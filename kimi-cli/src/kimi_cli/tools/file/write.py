import json_repair
from collections.abc import Callable
from pathlib import Path
from typing import Literal, override

from kaos.path import KaosPath
from kosong.tooling import CallableTool2, DisplayBlock, ToolError, ToolReturnValue
from pydantic import BaseModel, Field

from kimi_cli.session import Session
from kimi_cli.soul.agent import Runtime
from kimi_cli.soul.approval import Approval
from kimi_cli.tools.display import DiffDisplayBlock
from kimi_cli.tools.file import FileActions
from kimi_cli.tools.file.check_fmt import check_json_text, check_toml_text, check_xml_text, check_yaml_text
from kimi_cli.tools.file.plan_mode import inspect_plan_edit_target
from kimi_cli.utils.diff import build_diff_blocks
from kimi_cli import logger
from kimi_cli.utils.path import is_within_directory, is_within_workspace, kaos_path_from_user_input
from kimi_cli.vfs import VFS
from .utils import resolve_vfs

_BASE_DESCRIPTION = "Write content to a file."


class Params(BaseModel):
    path: str = Field(
        description="File path. Absolute paths required outside the working directory."
    )
    content: str = Field(description="Content to write.")
    mode: Literal["overwrite", "append"] = Field(
        description="Write mode: overwrite or append.",
        default="overwrite",
    )


class WriteFile(CallableTool2[Params]):
    name: str = "WriteFile"
    description: str = _BASE_DESCRIPTION
    params: type[Params] = Params

    def __init__(self, runtime: Runtime, approval: Approval, session: Session, vfs: VFS | None = None):
        super().__init__()
        self._work_dir = runtime.builtin_args.KIMI_WORK_DIR
        self._additional_dirs = runtime.additional_dirs
        self._approval = approval
        self._session = session
        self._vfs = vfs
        self._plan_mode_checker: Callable[[], bool] | None = None
        self._plan_file_path_getter: Callable[[], Path | None] | None = None

    def bind_plan_mode(
        self, checker: Callable[[], bool], path_getter: Callable[[], Path | None]
    ) -> None:
        """Bind plan mode state checker and plan file path getter."""
        self._plan_mode_checker = checker
        self._plan_file_path_getter = path_getter

    async def _validate_path(self, path: KaosPath) -> tuple[ToolError | None, bool]:
        """Validate that the path is safe to write.

        Returns:
            A tuple of (error_or_none, is_inside_workspace).
        """
        resolved_path = path.canonical()

        inside = is_within_workspace(
            resolved_path, self._work_dir, self._additional_dirs
        )
        if not inside and not path.is_absolute():
            return (
                ToolError(
                    message=(
                        f"`{path}` is not an absolute path. "
                        "You must provide an absolute path to write a file "
                        "outside the working directory."
                    ),
                    brief="Invalid path",
                ),
                False,
            )

        protected_paths = self._session.custom_config.get("config_json", {}).get("protected_write_paths")
        if protected_paths:
            from .utils import check_path_protected
            if matched := check_path_protected(resolved_path, protected_paths, self._work_dir):
                return (
                    ToolError(
                        message=f"Writing to `{path}` is blocked by protected path rule: `{matched}`.",
                        brief="Protected path",
                    ),
                    False,
                )

        return None, inside

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        display_path = params.path.replace("\\", "/")
        # TODO: checks:
        # - check if the path may contain secrets
        if not params.path:
            return ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        if not self._session.file_mtime.mark_dirty(params.path):
            return ToolError(
                message="File modified, read file first.",
                brief="File modified",
            )

        try:
            p = kaos_path_from_user_input(params.path)
            logical_path = p
            display_logical_path = str(logical_path).replace("\\", "/")
            _outside = not is_within_directory(logical_path.canonical(), self._work_dir)
            err, path_is_inside = await self._validate_path(logical_path)
            if err:
                err.message = f"[out of work-dir] {err.message}" if _outside else err.message
                return err

            p = await resolve_vfs(params.path, self._vfs, for_write=True)
            display_p = str(p).replace("\\", "/")

            if await p.is_dir():
                return ToolError(
                    message=f"{'[out of work-dir] ' if _outside else ''}`{display_p}` is a directory, not a file.",
                    brief="Path is a directory",
                )

            plan_target = inspect_plan_edit_target(
                logical_path,
                plan_mode_checker=self._plan_mode_checker,
                plan_file_path_getter=self._plan_file_path_getter,
            )
            if isinstance(plan_target, ToolError):
                if _outside:
                    plan_target.message = f"[out of work-dir] {plan_target.message}"
                return plan_target

            is_plan_file_write = plan_target.is_plan_target
            if is_plan_file_write and plan_target.plan_path is not None:
                plan_target.plan_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                await p.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return ToolError(
                    message=f"{'[out of work-dir] ' if _outside else ''}Failed to create parent directory for {display_p}: {e}",
                    brief="Parent directory not found",
                )

            # Validate mode parameter
            if params.mode not in ["overwrite", "append"]:
                return ToolError(
                    message=(
                        f"{'[out of work-dir] ' if _outside else ''}Invalid write mode: `{params.mode}`. "
                        "Mode must be either `overwrite` or `append`."
                    ),
                    brief="Invalid write mode",
                )

            old_text = ""
            file_existed = False
            try:
                old_text = await p.read_text(encoding="utf-8", errors="strict")
                file_existed = True
            except FileNotFoundError:
                pass

            if params.mode == "overwrite":
                new_text = params.content
            else:
                new_text = old_text + params.content

            # In-memory format validation & fix (before any write)
            fmt_error = None
            file_path_str = str(logical_path)
            is_json = file_path_str.lower().endswith(".json")
            if is_json:
                fmt_error = check_json_text(new_text)
            elif file_path_str.lower().endswith((".yaml", ".yml")):
                fmt_error = check_yaml_text(new_text)
            elif file_path_str.lower().endswith(".toml"):
                fmt_error = check_toml_text(new_text)
            elif file_path_str.lower().endswith(".xml"):
                fmt_error = check_xml_text(new_text)

            # Try to repair broken JSON before building diff
            if is_json and fmt_error:
                try:
                    repaired_text = json_repair.repair_json(new_text, return_objects=False)
                    if repaired_text:
                        new_text = repaired_text
                        fmt_error = None
                except Exception:
                    pass

            # Build diff blocks
            diff_blocks: list[DisplayBlock]
            if params.mode == "append" and file_existed:
                # Fast path: synthetic diff for append
                old_lines = old_text.splitlines()
                old_start = max(1, len(old_lines) - 2)
                old_context = "\n".join(old_lines[old_start - 1 :]) if old_lines else ""
                new_context = (
                    (old_context + "\n" if old_context else "") + params.content
                ).rstrip("\n")
                diff_blocks = [
                    DiffDisplayBlock(
                        path=file_path_str,
                        old_text=old_context,
                        new_text=new_context,
                        old_start=old_start,
                        new_start=old_start,
                    )
                ]
            else:
                diff_blocks = await build_diff_blocks(
                    file_path_str,
                    old_text,
                    new_text,
                )

            # Plan file writes are auto-approved; other writes need approval
            if not is_plan_file_write:
                action = (
                    FileActions.EDIT
                    if path_is_inside
                    else FileActions.EDIT_OUTSIDE
                )

                # Request approval
                result = await self._approval.request(
                    self.name,
                    action,
                    f"Write file `{display_logical_path}`",
                    display=diff_blocks,
                )
                if not result:
                    return result.rejection_error()

            # Write content to file
            if params.mode == "append" and file_existed:
                await p.append_text(params.content, encoding="utf-8", errors="strict")
            else:
                await p.write_text(new_text, encoding="utf-8", errors="strict")

            # Compute file size in-memory
            file_size = len(new_text.encode("utf-8"))
            action_desc = "overwritten" if params.mode == "overwrite" else "appended to"

            if fmt_error:
                return ToolError(
                    message=f"{'[out of work-dir] ' if _outside else ''}File successfully {action_desc}, but {fmt_error}",
                    brief="Format validation failed",
                )
            return ToolReturnValue(
                is_error=False,
                output="",
                message=(
                    f"{'[out of work-dir] ' if _outside else ''}File successfully {action_desc}. Current size: {file_size} bytes."
                ),
                display=diff_blocks,
            )

        except Exception as e:
            logger.warning(
                "WriteFile failed: {path}: {error}", path=params.path, error=e
            )
            _outside_ex = False
            try:
                _outside_ex = not is_within_directory(kaos_path_from_user_input(params.path).canonical(), self._work_dir)
            except Exception:
                pass
            return ToolError(
                message=f"{'[out of work-dir] ' if _outside_ex else ''}Failed to write to {display_path}. Error: {e}",
                brief="Failed to write file",
            )
