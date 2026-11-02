"""Forced ``job_output`` kills for every ProcessTask-backed tool."""

from __future__ import annotations

import asyncio

import pytest
import regex as re

from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.background.utils import get_all_tasks, get_finished_task
from kimi_cli.tools.file.bash.bash_tool import Bash, BashParams
from kimi_cli.tools.file.bash.pwsh_tool import Powershell, PowershellParams
from kimi_cli.tools.py import Params as PythonParams
from kimi_cli.tools.py import python as PythonTool


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bash", "pwsh", "python"])
async def test_force_kill_terminates_process_task(kind: str, session: Session) -> None:
    try:
        if kind == "bash":
            tool = Bash(session)
            started = await tool(BashParams(cmd="printf 'ready\\n'; sleep 30", mode="send"))
        elif kind == "pwsh":
            tool = Powershell(session)
            started = await tool(
                PowershellParams(
                    command='Write-Output "ready"; Start-Sleep -Seconds 30',
                    run_in_background=True,
                )
            )
        else:
            tool = PythonTool(session)
            started = await tool(
                PythonParams(
                    code='print("ready", flush=True)\nimport time\ntime.sleep(30)',
                    run_in_background=True,
                )
            )
    except SkipThisTool:
        pytest.skip(f"{kind} tool is unavailable on this platform")

    assert not started.is_error
    task_id = next(iter(get_all_tasks(session)))
    stream = get_all_tasks(session)[task_id]
    output, matched, _elapsed = await stream.wait_for_output(
        timeout=10,
        pattern=re.compile("ready"),
        inactivity_timeout=None,
    )
    assert matched, output

    reader = TaskOutput(session)
    killed = await asyncio.wait_for(
        reader(TaskOutputParams(job_id=task_id, action="kill")),
        timeout=15,
    )

    assert not killed.is_error
    assert "killed" in killed.brief
    assert not await stream.thread_is_alive()
    assert task_id not in get_all_tasks(session)
    record = get_finished_task(session, task_id)
    assert record is not None
    assert not record.success
