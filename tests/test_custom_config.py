from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kaos.path import KaosPath

from kimi_agent_sdk._session import Session
from kimi_cli.tools.file.read import ReadFile, Params as ReadFileParams
from kimi_cli.tools.file.replace import EditFile, Params as EditFileParams
from kimi_cli.tools.file.utils import check_path_protected
from kimi_cli.tools.file.write import WriteFile, Params as WriteFileParams
from kimix.tools.file.run import Run, RunParams
from kosong.tooling import ToolError, ToolOk


@pytest.fixture(autouse=True)
def patch_run_flags() -> None:
    with patch("kimix.tools.file.run.USE_SYSTEM_SHELL", False):
        with patch("kimix.tools.file.run.USE_SYSTEM_PWSH_ON_WINDOWS", False):
            with patch("kimix.tools.file.run.find_bash", return_value=None):
                yield


# ---------------------------------------------------------------------------
# check_path_protected helper
# ---------------------------------------------------------------------------


class TestCheckPathProtected:
    def test_exact_file_match(self) -> None:
        work_dir = KaosPath("/home/project")
        path = KaosPath("/tmp/secret.txt")
        result = check_path_protected(path, ["/tmp/secret.txt"], work_dir)
        assert result == "/tmp/secret.txt"

    def test_directory_containment(self) -> None:
        work_dir = KaosPath("/home/project")
        path = KaosPath("/tmp/secrets/file.txt")
        result = check_path_protected(path, ["/tmp/secrets"], work_dir)
        assert result == "/tmp/secrets"

    def test_relative_path_resolution(self) -> None:
        work_dir = KaosPath("/home/project")
        path = KaosPath("/home/project/.env")
        result = check_path_protected(path, [".env"], work_dir)
        assert result == ".env"

    def test_tilde_expansion(self) -> None:
        work_dir = KaosPath("/home/project")
        home = KaosPath.home()
        path = home / ".ssh" / "id_rsa"
        result = check_path_protected(path, ["~/.ssh/id_rsa"], work_dir)
        assert result == "~/.ssh/id_rsa"

    def test_no_match(self) -> None:
        work_dir = KaosPath("/home/project")
        path = KaosPath("/tmp/b")
        result = check_path_protected(path, ["/tmp/a"], work_dir)
        assert result is None

    def test_empty_protected_paths(self) -> None:
        work_dir = KaosPath("/home/project")
        result = check_path_protected(KaosPath("/tmp/a"), [], work_dir)
        assert result is None

    def test_none_protected_paths(self) -> None:
        work_dir = KaosPath("/home/project")
        result = check_path_protected(KaosPath("/tmp/a"), None, work_dir)
        assert result is None

    def test_non_string_entries_skipped(self) -> None:
        work_dir = KaosPath("/home/project")
        result = check_path_protected(
            KaosPath("/tmp/a"), ["/tmp/a", 123, None], work_dir
        )
        assert result == "/tmp/a"

        result = check_path_protected(
            KaosPath("/tmp/other"), ["/tmp/a", 123, None], work_dir
        )
        assert result is None


# ---------------------------------------------------------------------------
# WriteFile protected path behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def write_tool(tmp_path: Path) -> WriteFile:
    runtime = MagicMock()
    runtime.builtin_args.KIMI_WORK_DIR = KaosPath(str(tmp_path))
    runtime.additional_dirs = []
    approval = MagicMock()
    approval.request = AsyncMock(return_value=MagicMock(approved=True))
    session = MagicMock()
    session.id = "test"
    session.custom_data = {}
    session.custom_config = {
        "config_json": {
            "protected_write_paths": ["secrets", str(tmp_path / "protected.txt")]
        }
    }
    return WriteFile(runtime, approval, session)


class TestWriteFileProtected:
    async def test_write_protected_file(
        self, write_tool: WriteFile, tmp_path: Path
    ) -> None:
        params = WriteFileParams(path=str(tmp_path / "protected.txt"), content="secret")
        result = await write_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief.startswith("Protected path")

    async def test_write_inside_protected_dir(
        self, write_tool: WriteFile, tmp_path: Path
    ) -> None:
        params = WriteFileParams(
            path=str(tmp_path / "secrets" / "nested.txt"), content="secret"
        )
        result = await write_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief.startswith("Protected path")

    async def test_write_allowed_file(
        self, write_tool: WriteFile, tmp_path: Path
    ) -> None:
        params = WriteFileParams(path=str(tmp_path / "allowed.txt"), content="hello")
        result = await write_tool(params)
        assert not isinstance(result, ToolError)


# ---------------------------------------------------------------------------
# EditFile protected path behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def edit_tool(tmp_path: Path) -> EditFile:
    runtime = MagicMock()
    runtime.builtin_args.KIMI_WORK_DIR = KaosPath(str(tmp_path))
    runtime.additional_dirs = []
    approval = MagicMock()
    approval.request = AsyncMock(return_value=MagicMock(approved=True))
    session = MagicMock()
    session.id = "test"
    session.custom_data = {}
    session.custom_config = {
        "config_json": {
            "protected_write_paths": ["secrets", str(tmp_path / "protected.txt")]
        }
    }
    return EditFile(runtime, approval, session)


class TestEditFileProtected:
    async def test_edit_protected_file(
        self, edit_tool: EditFile, tmp_path: Path
    ) -> None:
        params = EditFileParams(
            path=str(tmp_path / "protected.txt"),
            edit={"old": "a", "new": "b"},
        )
        result = await edit_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief == "Protected path"

    async def test_edit_inside_protected_dir(
        self, edit_tool: EditFile, tmp_path: Path
    ) -> None:
        params = EditFileParams(
            path=str(tmp_path / "secrets" / "nested.txt"),
            edit={"old": "a", "new": "b"},
        )
        result = await edit_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief == "Protected path"

    async def test_edit_allowed_file(
        self, edit_tool: EditFile, tmp_path: Path
    ) -> None:
        f = tmp_path / "allowed.txt"
        f.write_text("hello world", encoding="utf-8")
        params = EditFileParams(
            path=str(f),
            edit={"old": "hello", "new": "hi"},
        )
        result = await edit_tool(params)
        assert not isinstance(result, ToolError)


# ---------------------------------------------------------------------------
# ReadFile protected path behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def read_tool(tmp_path: Path) -> ReadFile:
    runtime = MagicMock()
    runtime.builtin_args.KIMI_WORK_DIR = KaosPath(str(tmp_path))
    runtime.additional_dirs = []
    session = MagicMock()
    session.id = "test"
    session.custom_data = {}
    session.custom_config = {
        "config_json": {
            "protected_read_paths": ["secrets", str(tmp_path / "protected.txt")]
        }
    }
    return ReadFile(runtime, session)


class TestReadFileProtected:
    async def test_read_protected_file(
        self, read_tool: ReadFile, tmp_path: Path
    ) -> None:
        params = ReadFileParams(path=str(tmp_path / "protected.txt"))
        result = await read_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief.startswith("Protected path")

    async def test_read_inside_protected_dir(
        self, read_tool: ReadFile, tmp_path: Path
    ) -> None:
        params = ReadFileParams(path=str(tmp_path / "secrets" / "nested.txt"))
        result = await read_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief.startswith("Protected path")

    async def test_read_allowed_file(
        self, read_tool: ReadFile, tmp_path: Path
    ) -> None:
        f = tmp_path / "allowed.txt"
        f.write_text("hello\n", encoding="utf-8")
        params = ReadFileParams(path=str(f))
        result = await read_tool(params)
        assert isinstance(result, ToolOk)


# ---------------------------------------------------------------------------
# Run forbidden command behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def run_tool() -> Run:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {
        "config_json": {
            "forbidden_commands": ["git commit", "rm -rf /", "sudo"]
        }
    }
    return Run(session=session)


@pytest.fixture
def mock_process_task() -> MagicMock:
    with patch("kimix.tools.file.run.ProcessTask") as MockTask:
        mock_task = MagicMock()
        mock_task.stream = MagicMock()
        mock_task.stream.success = AsyncMock(return_value=True)
        mock_task.stream.exit_code = 0
        mock_task.stream.process_elapsed = None
        mock_task.stream.pop_output = AsyncMock(return_value="output")
        mock_task.thread_is_alive = AsyncMock(return_value=False)
        mock_task.start = AsyncMock(return_value="task-1")
        mock_task.wait = AsyncMock()
        MockTask.return_value = mock_task
        yield mock_task


class TestRunForbiddenCommands:
    async def test_forbidden_command_exact(self, run_tool: Run) -> None:
        params = RunParams(command="git commit -m msg")
        result = await run_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief == "Forbidden command"

    async def test_forbidden_command_with_path_in_path(self, run_tool: Run) -> None:
        params = RunParams(command="git commit -m msg")
        result = await run_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief == "Forbidden command"

    async def test_forbidden_command_rm_rf(self, run_tool: Run) -> None:
        params = RunParams(command="rm -rf /")
        result = await run_tool(params)
        assert isinstance(result, ToolError)
        # The unconditional hardline floor now intercepts `rm -rf /` before
        # the config keyword check, so the brief changed from "Forbidden
        # command" to "Blocked (hardline)" — both block the same command.
        assert result.brief == "Blocked (hardline)"

    async def test_allowed_command(self, run_tool: Run, mock_process_task: MagicMock) -> None:
        params = RunParams(command="git status")
        result = await run_tool(params)
        assert isinstance(result, ToolOk)

    async def test_forbidden_command_prefix_only(
        self, run_tool: Run, mock_process_task: MagicMock
    ) -> None:
        params = RunParams(command="git commitment something")
        result = await run_tool(params)
        assert isinstance(result, ToolError)
        assert result.brief == "Forbidden command"

    async def test_empty_forbidden_commands(
        self, run_tool: Run, mock_process_task: MagicMock
    ) -> None:
        run_tool._session.custom_config = {"config_json": {}}
        params = RunParams(command="git status")
        result = await run_tool(params)
        assert isinstance(result, ToolOk)

    async def test_non_string_forbidden_ignored(
        self, run_tool: Run, mock_process_task: MagicMock
    ) -> None:
        run_tool._session.custom_config = {"config_json": {"forbidden_commands": [123, None, ""]}}
        params = RunParams(command="git status")
        result = await run_tool(params)
        assert isinstance(result, ToolOk)


# ---------------------------------------------------------------------------
# Config loading: kimix.utils.config.load_config_json + explicit passing
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cli_setup() -> MagicMock:
    mock_cli_session = MagicMock()
    mock_cli_session.custom_config = {}
    mock_cli = MagicMock()
    mock_cli.session = mock_cli_session
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "kimi_agent_sdk._session.CliSession.create",
                new_callable=AsyncMock,
                return_value=mock_cli_session,
            )
        )
        stack.enter_context(
            patch(
                "kimi_agent_sdk._session.CliSession.continue_",
                new_callable=AsyncMock,
                return_value=mock_cli_session,
            )
        )
        stack.enter_context(
            patch(
                "kimi_agent_sdk._session.CliSession.find",
                new_callable=AsyncMock,
                return_value=mock_cli_session,
            )
        )
        stack.enter_context(
            patch(
                "kimi_agent_sdk._session.KimiCLI.create",
                new_callable=AsyncMock,
                return_value=mock_cli,
            )
        )
        yield mock_cli_session


@pytest.fixture(autouse=True)
def _clear_config_cache():
    from kimix.utils.config import clear_config_json_cache

    clear_config_json_cache()
    yield
    clear_config_json_cache()


def _write_config(tmp_path: Path, content: str) -> Path:
    config_dir = tmp_path / ".kimix"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text(content, encoding="utf-8")
    return config_file


class TestLoadConfigJson:
    """``.kimix/config.json`` is parsed once with orjson, then cached."""

    def test_load_config(self, tmp_path: Path) -> None:
        from kimix.utils.config import load_config_json

        _write_config(tmp_path, '{"protected_write_paths": ["secret"]}')
        assert load_config_json(tmp_path) == {"protected_write_paths": ["secret"]}

    def test_missing_config(self, tmp_path: Path) -> None:
        from kimix.utils.config import load_config_json

        assert load_config_json(tmp_path) == {}

    def test_malformed_config(self, tmp_path: Path) -> None:
        from kimix.utils.config import load_config_json

        _write_config(tmp_path, "not json")
        assert load_config_json(tmp_path) == {}

    def test_non_dict_config(self, tmp_path: Path) -> None:
        from kimix.utils.config import load_config_json

        _write_config(tmp_path, "[1, 2, 3]")
        assert load_config_json(tmp_path) == {}

    def test_parsed_only_once(self, tmp_path: Path) -> None:
        """After the first parse the file is never read again (cached dict)."""
        from kimix.utils import config as config_mod

        _write_config(tmp_path, '{"a": 1}')
        first = config_mod.load_config_json(tmp_path)
        # Overwrite the file; the cached value must still be returned and the
        # file must not be re-read.
        _write_config(tmp_path, '{"a": 2}')

        read_calls = 0
        original_read_bytes = Path.read_bytes

        def counting_read_bytes(self):  # type: ignore[no-untyped-def]
            nonlocal read_calls
            read_calls += 1
            return original_read_bytes(self)

        with patch.object(Path, "read_bytes", counting_read_bytes):
            second = config_mod.load_config_json(tmp_path)

        assert second is first
        assert second == {"a": 1}
        assert read_calls == 0

        config_mod.clear_config_json_cache()
        assert config_mod.load_config_json(tmp_path) == {"a": 2}


class TestSessionConfigLoading:
    """Session.create/resume receive the config dict explicitly."""

    async def test_create_uses_explicit_config_json(
        self, tmp_path: Path, mock_cli_setup: MagicMock
    ) -> None:
        from kimix.utils.config import load_config_json

        _write_config(tmp_path, '{"protected_write_paths": ["secret"]}')
        config_json = load_config_json(tmp_path)

        sdk_session = await Session.create(
            work_dir=KaosPath(str(tmp_path)), config_json=config_json
        )
        assert sdk_session._cli.session.custom_config == {
            "config_json": {"protected_write_paths": ["secret"]}
        }

    async def test_create_without_config_json(
        self, tmp_path: Path, mock_cli_setup: MagicMock
    ) -> None:
        sdk_session = await Session.create(work_dir=KaosPath(str(tmp_path)))
        assert sdk_session._cli.session.custom_config == {"config_json": {}}

    async def test_create_does_not_read_config_file(
        self, tmp_path: Path, mock_cli_setup: MagicMock
    ) -> None:
        """Even when .kimix/config.json exists, create() must not parse it."""
        _write_config(tmp_path, '{"forbidden_commands": ["rm"]}')

        read_calls = 0
        original_read_bytes = Path.read_bytes

        def counting_read_bytes(self):  # type: ignore[no-untyped-def]
            nonlocal read_calls
            read_calls += 1
            return original_read_bytes(self)

        with patch.object(Path, "read_bytes", counting_read_bytes):
            sdk_session = await Session.create(work_dir=KaosPath(str(tmp_path)))

        assert sdk_session._cli.session.custom_config == {"config_json": {}}
        assert read_calls == 0

    async def test_resume_uses_explicit_config_json(
        self, tmp_path: Path, mock_cli_setup: MagicMock
    ) -> None:
        from kimix.utils.config import load_config_json

        _write_config(tmp_path, '{"forbidden_commands": ["rm"]}')
        config_json = load_config_json(tmp_path)

        sdk_session = await Session.resume(
            work_dir=KaosPath(str(tmp_path)),
            session_id="test-id",
            config_json=config_json,
        )
        assert sdk_session._cli.session.custom_config == {
            "config_json": {"forbidden_commands": ["rm"]}
        }

    async def test_create_rejects_non_dict_config_json(
        self, tmp_path: Path, mock_cli_setup: MagicMock
    ) -> None:
        sdk_session = await Session.create(
            work_dir=KaosPath(str(tmp_path)), config_json="not a dict"  # type: ignore[arg-type]
        )
        assert sdk_session._cli.session.custom_config == {"config_json": {}}
