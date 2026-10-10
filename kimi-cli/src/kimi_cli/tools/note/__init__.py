"""Plan file tools: write_plan, read_plan, edit_plan (alias: WritePlan, ReadPlan, EditPlan)."""

from pathlib import Path
from typing import Literal

import anyio
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.utils import truncate_line
from pydantic import BaseModel, Field, model_validator
from rapidfuzz import fuzz, process

from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kimi_cli.tools.prompt_common import accepts_alias_text

MAX_LINES = 1000
MAX_LINE_LENGTH = 2000
MAX_BYTES = 100 << 10  # 100KB

_enable_plan: bool = False  # module-level flag (thread-safe via GIL)


def _set_enable_plan(value: bool) -> None:
    """Set the module-level _enable_plan flag for cross-thread access."""
    global _enable_plan
    _enable_plan = value


# --- write_plan ---

class WritePlanParams(BaseModel):
    model_config = {"populate_by_name": True}

    content: str = Field(
        alias="text",  # common LLM variant (mirrors WriteFile)
        description="Content to write. " + accepts_alias_text("content", "text", word=False),
    )
    mode: Literal["overwrite", "append"] = Field(
        description="Overwrite or append.",
        default="overwrite",
    )


class write_plan(CallableTool2):  # noqa: N801
    name: str = "write_plan"
    description: str = "Write the plan file."
    params: type[WritePlanParams] = WritePlanParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        if not _enable_plan:
            raise SkipThisTool()

    async def __call__(self, params: WritePlanParams) -> ToolReturnValue:
        path: Path | None = self._session.custom_data.get('plan_writing_path')
        if path is None:
            return ToolError(
                output="",
                message="Invalid: no plan_writing_path set.",
                brief="invalid tool.",
            )
        try:
            await anyio.to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))
            if params.mode == "overwrite":
                async with await anyio.open_file(path, 'w', encoding='utf-8') as f:
                    await f.write(params.content)
            else:
                async with await anyio.open_file(path, 'a', encoding='utf-8') as f:
                    await f.write(params.content)
            self._session.custom_data['plan_called'] = True
            action = "written to" if params.mode == "overwrite" else "appended to"
            return ToolOk(output=f"Plan {action} {path}")
        except Exception as exc:
            return ToolError(
                output="",
                message=str(exc),
                brief="Failed to write plan",
            )


# --- read_plan ---

class ReadPlanParams(BaseModel):
    line_offset: int = Field(
        description=f"1-based start line; negative reads from end, max abs {MAX_LINES}.",
        default=1,
    )
    n_lines: int = Field(
        description=f"Lines to read, max {MAX_LINES}.",
        default=MAX_LINES,
        ge=1,
    )
    max_char: int = Field(
        description="Max characters to return.",
        default=65536,
        ge=0,
    )
    char_offset: int = Field(
        description="Character offset to start from.",
        default=0,
        ge=0,
    )

    @model_validator(mode="after")
    def _validate_line_offset(self) -> "ReadPlanParams":
        if self.line_offset == 0:
            raise ValueError(
                "line_offset cannot be 0; use 1 (first line) or -1 (last line)"
            )
        if self.line_offset < -MAX_LINES:
            raise ValueError(
                f"line_offset cannot be less than -{MAX_LINES}. "
                "Use a positive line_offset to read from a position."
            )
        return self


class read_plan(CallableTool2):  # noqa: N801
    name: str = "read_plan"
    description: str = "Read the plan file."
    params: type[ReadPlanParams] = ReadPlanParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        if not _enable_plan:
            raise SkipThisTool()

    async def __call__(self, params: ReadPlanParams) -> ToolReturnValue:
        path: Path | None = self._session.custom_data.get('plan_writing_path')
        if path is None:
            return ToolError(
                output="",
                message="Invalid: no plan_writing_path set.",
                brief="invalid tool.",
            )
        try:
            if not await anyio.to_thread.run_sync(lambda: path.exists()):
                return ToolError(
                    message=f"Plan file `{path}` does not exist.",
                    brief="File not found",
                )
            if not await anyio.to_thread.run_sync(lambda: path.is_file()):
                return ToolError(
                    message=f"`{path}` is not a file.",
                    brief="Invalid path",
                )

            if params.line_offset < 0:
                result = await self._read_tail(path, params)
            else:
                result = await self._read_forward(path, params)

            if isinstance(result, ToolOk) and isinstance(result.output, str):
                result.output = result.output[params.char_offset:params.max_char]
            return result
        except Exception as exc:
            return ToolError(
                message=f"Failed to read plan: {exc}",
                brief="Failed to read plan",
            )

    async def _read_forward(self, path: Path, params: ReadPlanParams) -> ToolReturnValue:
        """Read file from a positive line_offset."""
        lines_with_no: list[str] = []
        n_bytes = 0
        truncated_line_numbers: list[int] = []
        max_lines_reached = False
        max_bytes_reached = False
        current_line_no = 0
        target_lines = min(params.n_lines, MAX_LINES)

        async with await anyio.open_file(path, 'r', encoding='utf-8', errors='replace') as f:
            async for line in f:
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
                    break
                if n_bytes >= MAX_BYTES:
                    max_bytes_reached = True
                    break

        start_line = params.line_offset

        message_parts: list[str] = []
        if len(lines_with_no) > 0:
            message_parts.append(f"{len(lines_with_no)} lines read from line {start_line}.")
        else:
            message_parts.append("No lines read.")
        if len(lines_with_no) < target_lines and not max_bytes_reached:
            message_parts.append(f"Total lines in file: {current_line_no}.")
        if max_lines_reached:
            message_parts.append(f"Max {MAX_LINES} lines reached.")
        elif max_bytes_reached:
            message_parts.append(f"Max {MAX_BYTES} bytes reached.")
        if truncated_line_numbers:
            message_parts.append(f"Lines {truncated_line_numbers} were truncated.")

        return ToolOk(
            output="".join(lines_with_no),
            message=" ".join(message_parts),
            brief="Read plan",
        )

    async def _read_tail(self, path: Path, params: ReadPlanParams) -> ToolReturnValue:
        """Read file from a negative line_offset (tail mode)."""
        tail_count = abs(params.line_offset)
        line_limit = min(params.n_lines, MAX_LINES)

        # Bounded list keeping the last `tail_count` lines.
        tail_buf: list[tuple[int, str, bool, int]] = []
        current_line_no = 0

        async with await anyio.open_file(path, 'r', encoding='utf-8', errors='replace') as f:
            async for line in f:
                current_line_no += 1
                truncated = truncate_line(line, MAX_LINE_LENGTH)
                b_len = len(truncated.encode("utf-8"))
                tail_buf.append((current_line_no, truncated, truncated != line, b_len))
                if len(tail_buf) > tail_count:
                    tail_buf.pop(0)

        total_lines = current_line_no

        candidates = tail_buf[:line_limit]
        max_lines_reached = len(tail_buf) > MAX_LINES and len(candidates) == MAX_LINES
        candidates, max_bytes_reached = _trim_candidates_to_byte_budget(candidates, MAX_BYTES)

        lines_with_no: list[str] = []
        truncated_line_numbers: list[int] = []
        for line_no, truncated, was_truncated, _ in candidates:
            if was_truncated:
                truncated_line_numbers.append(line_no)
            lines_with_no.append(f"{line_no:6d}\t{truncated}")

        start_line = candidates[0][0] if candidates else total_lines + 1
        return ToolOk(
            output="".join(lines_with_no),
            message=_build_plan_read_message(
                lines_with_no,
                start_line,
                total_lines,
                max_lines_reached=max_lines_reached,
                max_bytes_reached=max_bytes_reached,
                truncated_line_numbers=truncated_line_numbers,
            ),
            brief="Read plan",
        )


def _trim_candidates_to_byte_budget(
    candidates: list[tuple[int, str, bool, int]], max_bytes: int
) -> tuple[list[tuple[int, str, bool, int]], bool]:
    """Keep the newest candidates whose combined UTF-8 size fits *max_bytes*.

    Returns ``(kept_candidates, byte_budget_was_hit)``.  Extracted from
    ``_read_tail`` so that method stays under the G1 complexity threshold.
    """
    if not candidates:
        return candidates, False
    if sum(entry[3] for entry in candidates) <= max_bytes:
        return candidates, False
    kept = 0
    n_bytes = 0
    for entry in reversed(candidates):
        n_bytes += entry[3]
        if n_bytes > max_bytes:
            break
        kept += 1
    return candidates[len(candidates) - kept :], True


def _build_plan_read_message(
    lines_with_no: list[str],
    start_line: int,
    total_lines: int,
    *,
    max_lines_reached: bool,
    max_bytes_reached: bool,
    truncated_line_numbers: list[int],
) -> str:
    """Assemble the human-facing summary for a plan read (extracted for G1)."""
    if lines_with_no:
        parts = [f"{len(lines_with_no)} lines read from line {start_line}."]
    else:
        parts = ["No lines read."]
    parts.append(f"Total lines in file: {total_lines}.")
    if max_lines_reached:
        parts.append(f"Max {MAX_LINES} lines reached.")
    elif max_bytes_reached:
        parts.append(f"Max {MAX_BYTES} bytes reached.")
    if truncated_line_numbers:
        parts.append(f"Lines {truncated_line_numbers} were truncated.")
    return " ".join(parts)


# --- edit_plan ---

class Edit(BaseModel):
    model_config = {"populate_by_name": True}

    old: str = Field(
        alias="old_string",  # common LLM variant (mirrors EditFile)
        description="String to replace. " + accepts_alias_text("old", "old_string", word=False),
    )
    new: str = Field(
        alias="new_string",  # common LLM variant (mirrors EditFile)
        description="Replacement string. " + accepts_alias_text("new", "new_string", word=False),
    )
    replace_all: bool = Field(description="Replace all occurrences.", default=False)


class EditPlanParams(BaseModel):
    model_config = {"populate_by_name": True}

    edit: Edit | list[Edit] = Field(
        alias="edits",  # common LLM variant (plural, mirrors EditFile)
        description="One or more edits. " + accepts_alias_text("edit", "edits", word=False),
    )


class edit_plan(CallableTool2):  # noqa: N801
    name: str = "edit_plan"
    description: str = "Replace strings in the plan file."
    params: type[EditPlanParams] = EditPlanParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        if not _enable_plan:
            raise SkipThisTool()

    def _normalize_line_endings(self, text: str) -> str:
        """Normalize \r\n to \n for comparison."""
        return text.replace("\r\n", "\n")

    def _find_similar(self, target: str, content: str, cutoff: float = 75.0) -> str | None:
        """Find the most similar line or chunk in content to target.

        The single-line case is a plain best-match over the file's lines.  When
        the target spans several lines, the same comparison is repeated over every
        sliding window of that width.

        (The former trailing `if target_line_count == 1 and lines:` block was a
        byte-for-byte repeat of the first lookup, so it could never return anything
        new - removed as dead code, finding F-43.)
        """
        norm_target = self._normalize_line_endings(target)
        lines = self._normalize_line_endings(content).splitlines()

        match = self._best_fuzzy_match(norm_target, lines, cutoff)
        if match is not None:
            return match

        target_line_count = len(norm_target.splitlines())
        if target_line_count <= 1 or len(lines) < target_line_count:
            return None
        windows = [
            "\n".join(lines[i : i + target_line_count])
            for i in range(len(lines) - target_line_count + 1)
        ]
        return self._best_fuzzy_match(norm_target, windows, cutoff)

    @staticmethod
    def _best_fuzzy_match(target: str, candidates: list[str], cutoff: float) -> str | None:
        """Return the best `rapidfuzz` candidate scoring at or above *cutoff*."""
        if not candidates:
            return None
        best = process.extractOne(target, candidates, scorer=fuzz.ratio)
        if best and best[1] >= cutoff:
            return best[0]
        return None

    def _try_strip_match(
        self, content: str, old: str, new: str
    ) -> str | None:
        """Try to find *old* inside any line of *content* ignoring leading/trailing whitespace."""
        old_stripped = old.strip()
        if not old_stripped:
            return None

        for line in content.splitlines(keepends=True):
            line_core = line.rstrip("\n").rstrip("\r")
            idx = line_core.find(old_stripped)
            if idx != -1:
                prefix = line_core[:idx]
                suffix = line_core[idx + len(old_stripped):]
                ending = ""
                if line.endswith("\r\n"):
                    ending = "\r\n"
                elif line.endswith("\n"):
                    ending = "\n"
                elif line.endswith("\r"):
                    ending = "\r"
                new_line = prefix + new + suffix + ending
                return content.replace(line, new_line, 1)
        return None

    def _find_best_fuzzy_match(
        self, target: str, content: str, cutoff: float = 75.0
    ) -> tuple[str, float] | None:
        """Find the best fuzzy match of target in content."""
        norm_target = self._normalize_line_endings(target)
        norm_content = self._normalize_line_endings(content)

        best_score = 0.0
        best_original = None

        target_lines = norm_target.splitlines()
        target_line_count = len(target_lines)

        original_lines = content.splitlines()
        norm_lines = norm_content.splitlines()

        if target_line_count == 1:
            for orig_line, norm_line in zip(original_lines, norm_lines, strict=False):
                score = fuzz.ratio(norm_target, norm_line)
                if score > best_score:
                    best_score = score
                    best_original = orig_line
        else:
            for i in range(len(norm_lines) - target_line_count + 1):
                window = "\n".join(norm_lines[i: i + target_line_count])
                score = fuzz.ratio(norm_target, window)
                if score > best_score:
                    best_score = score
                    best_original = "\n".join(
                        original_lines[i: i + target_line_count]
                    )

        if best_score >= cutoff:
            return best_original, best_score

        return None

    def _apply_edit(self, content: str, edit: Edit) -> tuple[str, int, str | None]:
        """Apply a single edit to the content.

        Returns (new_content, replacements_made, suggestion_or_None).
        """
        if not edit.old or edit.old == edit.new:
            return content, 0, None

        norm_content = self._normalize_line_endings(content)
        norm_old = self._normalize_line_endings(edit.old)
        norm_new = self._normalize_line_endings(edit.new)

        if edit.replace_all:
            count = norm_content.count(norm_old)
            if count == 0:
                suggestion = self._find_similar(edit.old, content)
                return content, 0, suggestion
            return norm_content.replace(norm_old, norm_new), count, None

        # Single replacement
        idx = norm_content.find(norm_old)
        if idx != -1:
            return norm_content.replace(norm_old, norm_new, 1), 1, None

        # Try strip match
        stripped = self._try_strip_match(content, edit.old, edit.new)
        if stripped is not None:
            return stripped, 1, None

        # Try fuzzy match
        fuzzy = self._find_best_fuzzy_match(edit.old, content)
        if fuzzy is not None:
            matched_text, score = fuzzy
            new_content = norm_content.replace(
                self._normalize_line_endings(matched_text), norm_new, 1
            )
            return new_content, 1, None

        # No match
        suggestion = self._find_similar(edit.old, content)
        return content, 0, suggestion

    async def __call__(self, params: EditPlanParams) -> ToolReturnValue:
        path: Path | None = self._session.custom_data.get('plan_writing_path')
        if path is None:
            return ToolError(
                output="",
                message="Invalid: no plan_writing_path set.",
                brief="invalid tool.",
            )

        try:
            if not await anyio.to_thread.run_sync(lambda: path.exists()):
                return ToolError(
                    message=f"Plan file `{path}` does not exist.",
                    brief="File not found",
                )

            content = await anyio.to_thread.run_sync(
                lambda: path.read_text(encoding='utf-8', errors='replace')
            )

            original_content = content
            edits = [params.edit] if isinstance(params.edit, Edit) else params.edit

            text = content
            total = 0
            last_suggestion = None
            for edit in edits:
                text, n, suggestion = self._apply_edit(text, edit)
                total += n
                if suggestion:
                    last_suggestion = suggestion

            new_content = text

            if new_content == original_content:
                msg = "No replacements were made. The old string was not found in the plan file."
                if last_suggestion:
                    msg += f"\n\nDid you mean:\n  {last_suggestion}"
                return ToolError(
                    message=msg,
                    brief="No replacements made",
                )

            await anyio.to_thread.run_sync(
                lambda: path.write_text(new_content, encoding='utf-8')
            )

            return ToolOk(
                output="",
                message=f"Edited plan: {len(edits)} edit(s), {total} replacement(s).",
            )
        except Exception as exc:
            return ToolError(
                message=f"Failed to edit plan: {exc}",
                brief="Failed to edit plan",
            )


# --- Legacy aliases ---
# Old CamelCase tool names (used by agent manifests, prompt text and sessions
# recorded before the rename) resolve to the snake_case classes so nothing
# breaks while callers migrate.  The toolset redirect map
# (``kimi_cli.soul.toolset._build_platform_redirects``) maps the old *tool*
# names to the new ones at call time.
WritePlan = write_plan
ReadPlan = read_plan
EditPlan = edit_plan



