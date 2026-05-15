import asyncio
import tempfile
from pathlib import Path
from typing import Any

from kimi_agent_sdk import Session
from kimix.utils.session import _create_session_async, close_session_async
from kimix.utils.prompt import prompt_async
from kimi_cli.vfs.core import VFS, merge
from kimix.dag import DAG
from kimix.dag.executor import Executor
from kimix.utils import SystemPromptType

_ALL_VFS_PATH: dict[str, Path] = dict()
_MAX_AGENT_CONCURRENCY = 5

async def create_swarm_session(task_prompt: str) -> DAG | None:
    """Create a swarm session using agent_swarm.json and initialize the DAG."""
    agent_file = Path("agent_swarm.json")
    session = None
    try:
        session = await _create_session_async(agent_file=agent_file, agent_type=SystemPromptType.SwarmCoordinator)
        custom_data = session.get_custom_data()
        assert custom_data is not None
        dag = DAG()
        custom_data["swarm_dag"] = dag
        custom_data["swarm_node_counter"] = 0
        coordinator_prompt = f"Task: {task_prompt}"
        await prompt_async(coordinator_prompt, session, info_print=False)
        return dag
    finally:
        if session is not None:
            await close_session_async(session)

async def execute_swarm_dag(dag: DAG, finalize_prompt: str = "") -> Path | None:
    """Execute all tasks in a swarm DAG and merge resulting VFS paths.

    Args:
        dag: The planned swarm DAG (typically from create_swarm_session).
        finalize_prompt: Optional final prompt to run after merging.

    Returns:
        Path to the merged directory, or None if there are no VFS paths.
    """
    if len(dag) == 0:
        return None

    def _execute() -> dict[str, Any]:
        # Limit concurrent agents to prevent HTTP 429.
        executor = Executor(max_workers=_MAX_AGENT_CONCURRENCY)
        return executor.execute(dag)

    await asyncio.to_thread(_execute)
    return await merge_vfs_paths(finalize_prompt)

async def run_swarm_session(task_prompt: str, finalize_prompt: str = "") -> Path | None:
    """End-to-end swarm: coordinator plans the DAG, nodes execute, results merge.

    Args:
        task_prompt: High-level task description for the swarm coordinator.
        finalize_prompt: Optional final prompt to run after merging.

    Returns:
        Path to the merged directory, or None if the swarm produced no outputs.
    """
    _ALL_VFS_PATH.clear()
    dag = await create_swarm_session(task_prompt)
    if dag is None or len(dag) == 0:
        return None
    return await execute_swarm_dag(dag, finalize_prompt)

async def execute_swarm(node_id: str, prompt_str: str, vfs_path: Path | None) -> None:
    session = None
    try:
        session = await _create_session_async(
            vfs_path=vfs_path
        )
        custom_data = session.get_custom_data()
        assert custom_data is not None
        await prompt_async(prompt_str, session)
    finally:
        if session is not None:
            if vfs_path is not None:
                _ALL_VFS_PATH[node_id] = vfs_path
            await close_session_async(session)

async def merge_vfs_paths(finalize_prompt_str: str) -> Path | None:
    if not _ALL_VFS_PATH:
        return None

    merged_path = Path(tempfile.mkdtemp(prefix="swarm_merged_"))

    node_ids: list[str] = []
    vfs_instances: list[VFS] = []
    for node_id, vfs_path in _ALL_VFS_PATH.items():
        if vfs_path and vfs_path.exists():
            node_ids.append(node_id)
            vfs_instances.append(VFS(virtual_root=vfs_path, work_dir=merged_path))

    if not vfs_instances:
        _ALL_VFS_PATH.clear()
        return merged_path

    conflicts, _applied = merge(*vfs_instances, apply=True)

    session = None
    try:
        if conflicts:
            session = await _create_session_async()
            for rel_path, versions in conflicts.items():
                conflict_prompt = f"Merge conflict for file `{rel_path}`.\nMultiple versions exist from different swarm nodes:\n"
                for idx, content_bytes in versions:
                    node_id = node_ids[idx]
                    try:
                        content = content_bytes.decode('utf-8', errors='replace')
                    except Exception:
                        content = '<binary or unreadable>'
                    conflict_prompt += f"\n--- Version from {node_id} ---\n{content}\n"
                conflict_prompt += f"\nProduce the final merged content for `{rel_path}`. Output only the file content, no explanations."

                lines = []
                def capture(text: str, is_thinking: bool) -> None:
                    if not is_thinking:
                        lines.append(text)

                await prompt_async(conflict_prompt, session, info_print=False, output_function=capture)
                merged_content = '\n'.join(lines) if lines else ''
                dst = merged_path / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(merged_content, encoding='utf-8', errors='replace')

        if finalize_prompt_str:
            if session is None:
                session = await _create_session_async()
            await prompt_async(finalize_prompt_str, session)
    finally:
        if session is not None:
            await close_session_async(session)

    _ALL_VFS_PATH.clear()
    return merged_path