from pathlib import Path
from typing import override

from kaos.path import KaosPath
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field, model_validator

from kimi_cli.session import Session
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.file.utils import MEDIA_SNIFF_BYTES, detect_file_type
from kimi_cli.vfs import VFS
from .utils import resolve_vfs
from kimi_cli.tools.utils import load_desc, truncate_line
from kimi_cli.utils.logging import logger
from kimi_cli.utils.path import is_within_workspace, kaos_path_from_user_input
from kimi_cli.utils.sensitive import is_sensitive_file

MAX_LINES = 1000
MAX_LINE_LENGTH = 2000
MAX_BYTES = 100 << 10  # 100KB


class Params(BaseModel):
    path: str = Field(
        description="File path. Absolute for files outside working directory."
    )
    line_offset: int = Field(
        description=(
            "Start line, 1-based. Negative reads from end. "
            f"Max abs {MAX_LINES}."
        ),
        default=1,
    )
    n_lines: int = Field(
        description=f"Lines to read, max {MAX_LINES}.",
        default=MAX_LINES,
        ge=1,
    )
    max_char: int = Field(
        description="Maximum number of characters to return.",
        default=65536,
        ge=0,
    )
    char_offset: int = Field(
        description="Character offset to start returning from.",
        default=0,
        ge=0,
    )

    @model_validator(mode="after")
    def _validate_line_offset(self) -> "Params":
        if self.line_offset == 0:
            raise ValueError(
                "line_offset cannot be 0; use 1 for the first line or -1 for the last line"
            )
        if self.line_offset < -MAX_LINES:
            raise ValueError(
                f"line_offset cannot be less than -{MAX_LINES}. "
                "Use a positive line_offset with the total line count "
                "to read from a specific position."
            )
        return self


class ReadFile(CallableTool2[Params]):
    name: str = "ReadFile"
    params: type[Params] = Params
    def __del__(self):
        # TODO
        pass
        

    def __init__(self, runtime: Runtime, session: Session, vfs: VFS | None = None) -> None:
        self.session_id = session.id
        self._session = session
        description = load_desc(
            Path(__file__).parent / "read.md",
            {
                "MAX_LINES": MAX_LINES,
                "MAX_LINE_LENGTH": MAX_LINE_LENGTH,
                "MAX_BYTES": MAX_BYTES,
            },
        )
        super().__init__(description=description)
        self._runtime = runtime
        self._work_dir = runtime.builtin_args.KIMI_WORK_DIR
        self._additional_dirs = runtime.additional_dirs
        self._vfs = vfs

    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        """Validate that the path is safe to read."""
        resolved_path = path.canonical()

        if (
            not is_within_workspace(resolved_path, self._work_dir, self._additional_dirs)
            and not path.is_absolute()
        ):
            # Outside files can only be read with absolute paths
            return ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to read a file "
                    "outside the working directory."
                ),
                brief=f"Invalid path: {path}",
            )

        protected_paths = self._session.custom_config.get("config_json", {}).get("protected_read_paths")
        if protected_paths:
            from .utils import check_path_protected
            if matched := check_path_protected(resolved_path, protected_paths, self._work_dir):
                return ToolError(
                    message=f"Reading `{path}` is blocked by protected path rule: `{matched}`.",
                    brief=f"Protected path: {path}",
                )
        return None

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if not params.path:
            return ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        display_path = params.path.replace("\\", "/")

        try:
            p = kaos_path_from_user_input(params.path)
            logical_path = p
            if err := await self._validate_path(p):
                return err

            p = await resolve_vfs(params.path, self._vfs, for_write=False)

            if is_sensitive_file(str(logical_path)):
                return ToolError(
                    message=(
                        f"`{display_path}` appears to contain secrets "
                        "(matched sensitive file pattern). "
                        "Reading this file is blocked to protect credentials."
                    ),
                    brief=f"Sensitive file: {display_path}",
                )

            if not await p.exists():
                return ToolError(
                    message=f"`{display_path}` does not exist.",
                    brief=f"File not found: {display_path}",
                )
            if not await p.is_file():
                return ToolError(
                    message=f"`{display_path}` is not a file.",
                    brief=f"Invalid path: {display_path}"
                )

            header = await p.read_bytes(MEDIA_SNIFF_BYTES)
            file_type = detect_file_type(str(logical_path), header=header)
            if file_type.kind in ("image", "video"):
                return ToolError(
                    message=(
                        f"`{display_path}` is a {file_type.kind} file. "
                        "Use other appropriate tools to read image or video files."
                    ),
                    brief=f"Unsupported file type: {display_path}",
                )

            if file_type.kind == "unknown":
                return ToolError(
                    message=(
                        f"`{display_path}` seems not readable. "
                        "You may need to read it with proper shell commands, Python tools "
                        "or MCP tools if available. "
                        "If you read/operate it with Python, you MUST ensure that any "
                        "third-party packages are installed in a virtual environment (venv)."
                    ),
                    brief=f"File not readable: {display_path}",
                )

            assert params.n_lines >= 1
            assert params.line_offset != 0

            if params.line_offset < 0:
                result = await self._read_tail(p, params)
            else:
                result = await self._read_forward(p, params)

            if isinstance(result, ToolOk):
                if isinstance(result.output, str):
                    result.output = result.output[params.char_offset:params.max_char]
                self._session.file_mtime.clean_file(params.path)
            return result
        except Exception as e:
            logger.warning("ReadFile failed: {path}: {error}", path=params.path, error=e)
            return ToolError(
                message=f"Failed to read {display_path}. Error: {e}",
                brief=f"Failed to read file: {display_path}",
            )

    async def _read_forward(self, p: KaosPath, params: Params) -> ToolReturnValue:
        """Read file from a positive line_offset."""
        display_path = params.path.replace("\\", "/")
        lines_with_no: list[str] = []
        n_bytes = 0
        truncated_line_numbers: list[int] = []
        max_lines_reached = False
        max_bytes_reached = False
        current_line_no = 0
        target_lines = min(params.n_lines, MAX_LINES)
        eof_reached = True

        async for line in p.read_lines(errors="replace"):
            current_line_no += 1
            if current_line_no < params.line_offset:
                continue
            truncated = truncate_line(line, MAX_LINE_LENGTH)
            if truncated != line:
                truncated_line_numbers.append(current_line_no)
            b_len = len(truncated.encode("utf-8"))
            lines_with_no.append(f"{current_line_no:6d}\t{truncated}")
            n_bytes += b_len
            if len(lines_with_no) >= target_lines:
                max_lines_reached = target_lines >= MAX_LINES
                eof_reached = False
                break
            if n_bytes >= MAX_BYTES:
                max_bytes_reached = True
                eof_reached = False
                break

        start_line = params.line_offset

        message = (
            f"{len(lines_with_no)} lines read from file starting from line {start_line}."
            if len(lines_with_no) > 0
            else "No lines read from file."
        )
        if eof_reached:
            message += f" Total lines in file: {current_line_no}."
        if max_lines_reached:
            message += f" Max {MAX_LINES} lines reached."
        elif max_bytes_reached:
            message += f" Max {MAX_BYTES} bytes reached."
        elif len(lines_with_no) < params.n_lines:
            message += " End of file reached."
        if truncated_line_numbers:
            message += f" Lines {truncated_line_numbers} were truncated."
        return ToolOk(
            output="".join(lines_with_no),
            message=message,
            brief=f"Read {display_path}",
        )

    async def _read_tail(self, p: KaosPath, params: Params) -> ToolReturnValue:
        """Read file from a negative line_offset (tail mode)."""
        display_path = params.path.replace("\\", "/")
        tail_count = abs(params.line_offset)
        line_limit = min(params.n_lines, MAX_LINES)

        # Bounded list keeping the last `tail_count` lines.
        # Each entry: (line_no, truncated_line, was_truncated, byte_len)
        tail_buf: list[tuple[int, str, bool, int]] = []
        current_line_no = 0
        async for line in p.read_lines(errors="replace"):
            current_line_no += 1
            truncated = truncate_line(line, MAX_LINE_LENGTH)
            b_len = len(truncated.encode("utf-8"))
            tail_buf.append((current_line_no, truncated, truncated != line, b_len))
            if len(tail_buf) > tail_count:
                tail_buf.pop(0)

        total_lines = current_line_no

        # Apply n_lines / MAX_LINES from head of tail_buf.
        candidates = tail_buf[:line_limit]
        max_lines_reached = len(tail_buf) > MAX_LINES and len(candidates) == MAX_LINES

        # Apply MAX_BYTES — reverse-scan to keep the newest lines that fit.
        if candidates:
            total_candidate_bytes = sum(entry[3] for entry in candidates)
            if total_candidate_bytes > MAX_BYTES:
                max_bytes_reached = True
                kept = 0
                n_bytes = 0
                for entry in reversed(candidates):
                    n_bytes += entry[3]
                    if n_bytes > MAX_BYTES:
                        break
                    kept += 1
                candidates = candidates[len(candidates) - kept :]
            else:
                max_bytes_reached = False
        else:
            max_bytes_reached = False

        # Build output directly.
        lines_with_no: list[str] = []
        truncated_line_numbers: list[int] = []
        for line_no, truncated, was_truncated, _ in candidates:
            if was_truncated:
                truncated_line_numbers.append(line_no)
            lines_with_no.append(f"{line_no:6d}\t{truncated}")

        start_line = candidates[0][0] if candidates else total_lines + 1
        message = (
            f"{len(lines_with_no)} lines read from file starting from line {start_line}."
            if len(lines_with_no) > 0
            else "No lines read from file."
        )
        message += f" Total lines in file: {total_lines}."
        if max_lines_reached:
            message += f" Max {MAX_LINES} lines reached."
        elif max_bytes_reached:
            message += f" Max {MAX_BYTES} bytes reached."
        elif len(lines_with_no) < params.n_lines:
            message += " End of file reached."
        if truncated_line_numbers:
            message += f" Lines {truncated_line_numbers} were truncated."
        return ToolOk(
            output="".join(lines_with_no),
            message=message,
            brief=f"Read {display_path}",
        )
