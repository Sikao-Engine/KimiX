"""Example: swarm agents write functions to calculator.py, then merge and test.

This example demonstrates:
1. Creating a swarm coordinator session with create_swarm_session.
2. Starting multiple threads, each calling execute_swarm to write a different
   function to the same Python script (calculator.py).
3. After all threads finish, calling merge_vfs_paths with a finalize_prompt_str
   to let the final agent test the merged result script.

To avoid requiring real LLM API keys, this example monkey-patches the session
and prompt functions so the "agents" write files deterministically.
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from kimix.dag.agent_swarm import execute_swarm, merge_vfs_paths, _ALL_VFS_PATH
from kimix.dag import DAG
from kimix.utils import SystemPromptType

_thread_local = threading.local()

NODE_FUNCTIONS: dict[str, str] = {
    "node_add": "def add(a, b):\n    return a + b\n",
    "node_sub": "def sub(a, b):\n    return a - b\n",
    "node_mul": "def mul(a, b):\n    return a * b\n",
}


class MockSession:
    id: str = "mock_session"

    def __init__(self) -> None:
        self._custom_data: dict[str, Any] = {}

    def get_custom_data(self) -> dict[str, Any]:
        return self._custom_data

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def prompt(self, prompt_str: str, **kwargs: Any) -> AsyncGenerator[MagicMock, None]:
        m = MagicMock()
        m.model_dump_json.return_value = "{}"
        yield m

    @property
    def status(self) -> object:
        class _Status:
            context_usage: float = 0.0
        return _Status()


async def mock_create_session(
    *,
    agent_file: Path | None = None,
    agent_type: SystemPromptType = SystemPromptType.Worker,
    vfs_path: Path | None = None,
    **kwargs: Any,
) -> MockSession:
    return MockSession()


async def mock_prompt_async(
    prompt_str: str,
    session: MockSession | None = None,
    **kwargs: Any,
) -> None:
    from typing import Callable, cast
    output_function: Callable[[str, bool], None] | None = cast(
        Callable[[str, bool], None] | None, kwargs.get("output_function")
    )
    info_print = kwargs.get("info_print", True)

    node_id = getattr(_thread_local, "node_id", None)
    vfs_path = getattr(_thread_local, "vfs_path", None)

    # Swarm node execution: write the function file
    if node_id and vfs_path and prompt_str and not output_function:
        content = NODE_FUNCTIONS.get(node_id, "")
        if content:
            calc = vfs_path / "calculator.py"
            calc.parent.mkdir(parents=True, exist_ok=True)
            calc.write_text(content, encoding="utf-8")
            if info_print:
                print(f"[{node_id}] wrote calculator.py")
        return

    # Conflict resolution or finalize prompt
    if output_function and prompt_str:
        if "calculator.py" in prompt_str:
            merged = (
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "def sub(a, b):\n"
                "    return a - b\n"
                "\n"
                "def mul(a, b):\n"
                "    return a * b\n"
            )
            output_function(merged, False)
        return

    if prompt_str and info_print:
        print(f"[finalize] {prompt_str[:80]}...")


async def mock_close_session(session: MockSession) -> None:
    pass


def setup_mocks() -> None:
    import kimix.dag.agent_swarm as swarm_mod

    swarm_mod._create_session_async = mock_create_session
    swarm_mod.prompt_async = mock_prompt_async
    swarm_mod.close_session_async = mock_close_session


async def create_swarm_session(task_prompt: str) -> DAG | None:
    """Create a swarm session and initialize the DAG."""
    agent_file = Path("agent_swarm.yaml")
    session = None
    try:
        session = await mock_create_session(agent_file=agent_file, agent_type=SystemPromptType.SwarmCoordinator)
        custom_data = session.get_custom_data()
        assert custom_data is not None
        dag = DAG()
        custom_data["swarm_dag"] = dag
        custom_data["swarm_node_counter"] = 0
        coordinator_prompt = f"Task: {task_prompt}"
        await mock_prompt_async(coordinator_prompt, session, info_print=False)
        return dag
    finally:
        if session is not None:
            await mock_close_session(session)


def run_node(node_id: str, prompt: str, vfs_path: Path) -> None:
    _thread_local.node_id = node_id
    _thread_local.vfs_path = vfs_path
    asyncio.run(execute_swarm(node_id, prompt, vfs_path))


async def main() -> None:
    _ALL_VFS_PATH.clear()
    setup_mocks()

    base_dir = Path(tempfile.mkdtemp(prefix="swarm_example_"))

    # Demonstrate create_swarm_session
    print("Creating swarm coordinator session...")
    dag = await create_swarm_session(
        "Build a calculator module with add, sub, and mul functions."
    )
    print(f"Coordinator session created (dag={dag})")

    nodes = [
        ("node_add", "Write function `add(a, b)` to calculator.py"),
        ("node_sub", "Write function `sub(a, b)` to calculator.py"),
        ("node_mul", "Write function `mul(a, b)` to calculator.py"),
    ]

    print("Starting swarm nodes...")
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        for node_id, prompt in nodes:
            vfs_path = base_dir / node_id
            vfs_path.mkdir(parents=True, exist_ok=True)
            future = executor.submit(run_node, node_id, prompt, vfs_path)
            futures.append(future)

        for future in futures:
            future.result()

    print(f"All nodes finished. Registered paths: {list(_ALL_VFS_PATH.keys())}")

    finalize_prompt = (
        "The merged calculator.py should contain add(), sub(), and mul() functions. "
        "Verify the file is syntactically valid Python."
    )

    merged_path = await merge_vfs_paths(finalize_prompt)
    print(f"Merged path: {merged_path}")

    merged_file = merged_path / "calculator.py"
    if merged_file.exists():
        content = merged_file.read_text(encoding="utf-8")
        print(f"Merged calculator.py:\n{content}")

        try:
            compile(content, str(merged_file), "exec")
            print("Syntax check: PASSED")
        except SyntaxError as exc:
            print(f"Syntax check: FAILED - {exc}")
    else:
        print("ERROR: calculator.py not found in merged path!")
        for f in merged_path.rglob("*"):
            if f.is_file():
                print(f"  {f.relative_to(merged_path)}: {f.read_text()[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
