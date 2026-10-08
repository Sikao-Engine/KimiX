
import fnmatch
import os

import anyio
from kimi_cli.native_loader import (
    get_module as _native_get_module,
)
from kimi_cli.native_loader import (
    use_native as _native_use_native,
)
from pydantic import BaseModel, Field

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kimix.tools.common import _maybe_export_output

# Resolved once at import time (stable runtime: result never changes).
_NATIVE_TOOLS = _native_get_module("tools")


def _split_glob_pattern(target_path: str) -> tuple[str, str]:
    """Split ``'dir/**/*.ext'`` into ``('dir', '**/*.ext')``.

    The split point is the first path component that contains a glob character;
    everything before it is the base directory (``'.'`` when empty).  Extracted
    from ``FindStr`` so the tool method stays under the G1 complexity threshold.
    """
    parts = target_path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if "*" in part or "?" in part:
            return "/".join(parts[:i]) or ".", "/".join(parts[i:])
    return os.path.dirname(target_path) or ".", os.path.basename(target_path)


def _list_dir_files(directory: str, pattern: str) -> list[str]:
    """Return the files directly inside *directory* whose name matches *pattern*."""
    if not os.path.isdir(directory):
        return []
    out: list[str] = []
    for item in os.listdir(directory):
        full_path = os.path.join(directory, item)
        if pattern == "*" or fnmatch.fnmatch(item, pattern):
            if os.path.isfile(full_path):
                out.append(full_path)
    return out


def _walk_dir_files(directory: str, pattern: str) -> list[str]:
    """Recursively walk *directory*, matching basenames against *pattern*."""
    out: list[str] = []
    for root, _dirnames, filenames in os.walk(directory):
        for filename in filenames:
            if fnmatch.fnmatch(filename, pattern):
                out.append(os.path.join(root, filename))
    return out


def _native_find_in_file(
    lines: list[str], search_content: str, case_sensitive: bool, file_path: str
) -> list[dict] | None:
    """Return the native fast-path result, or ``None`` when it cannot be used.

    Native acceleration (``kimix_native.tools.find_in_file``) is only safe when
    the needle contains no line endings - the native kernel splits on ``\n`` and
    searches within the line body, so a needle containing ``\n``/``\r`` would
    behave differently from the Python per-line scan - and when both the content
    and the needle are pure ASCII.
    """
    if not (_native_use_native("TOOLS") and _NATIVE_TOOLS is not None):
        return None
    if "\n" in search_content or "\r" in search_content:
        return None
    content = "".join(lines)
    if not (content.isascii() and search_content.isascii()):
        return None
    return _NATIVE_TOOLS.find_in_file(content, search_content, case_sensitive, file_path)


def _match_columns(haystack: str, needle: str) -> list[int]:
    """0-based start columns of every occurrence of *needle* in *haystack*.

    Faithfully reproduces the original in-line scan, including its behaviour for
    an empty needle (a match at every position).
    """
    out: list[int] = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return out
        out.append(idx)
        start = idx + 1


def _find_files(target_path: str) -> list[str]:
    """Find files matching the path pattern."""
    # If path is a file, return it directly
    if os.path.isfile(target_path):
        return [target_path]

    if "*" not in target_path and "?" not in target_path:
        # No glob: treat the path as a directory listing of everything.
        return _list_dir_files(target_path, "*")

    base_dir, pattern_part = _split_glob_pattern(target_path)
    if "**" in pattern_part:
        return _walk_dir_files(base_dir, pattern_part.replace("**", "*"))
    return _list_dir_files(base_dir, pattern_part)


def _find_in_file(file_path: str, search_content: str, case_sensitive: bool) -> list[dict]:
    """Find all occurrences of *search_content* in *file_path*."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []

    native = _native_find_in_file(lines, search_content, case_sensitive, file_path)
    if native is not None:
        return native

    needle = search_content if case_sensitive else search_content.lower()
    results: list[dict] = []
    for line_num, line in enumerate(lines, start=1):
        haystack = line if case_sensitive else line.lower()
        for column in _match_columns(haystack, needle):
            results.append(
                {
                    "file": file_path,
                    "line": line_num,
                    "column": column + 1,  # 1-based column
                    "content": line.rstrip("\n\r"),
                }
            )
    return results


class FindStrParams(BaseModel):
    content: str = Field(
        description="Text to search for."
    )
    path: str = Field(
        description="Target path. Supports glob patterns: '*.ext', '**.ext'."
    )
    case_sensitive: bool = Field(
        default=False,
        description="Enable case-sensitive search."
    )


class FindStr(CallableTool2):
    name: str = "FindStr"
    description: str = "Search text in files."
    params: type[FindStrParams] = FindStrParams

    async def __call__(self, params: FindStrParams) -> ToolReturnValue:
        try:
            async def _offload(fn, *args):
                # os.walk/os.listdir/open are blocking: keep them off the event
                # loop so concurrent tools keep streaming (FP-12).
                return await anyio.to_thread.run_sync(fn, *args)

            files = await _offload(_find_files, params.path)

            if not files:
                return ToolOk(output=_maybe_export_output(f"No files found matching path: {params.path}"))

            all_matches = []
            for file_path in files:
                matches = await _offload(
                    _find_in_file, file_path, params.content, params.case_sensitive
                )
                all_matches.extend(matches)

            if not all_matches:
                return ToolOk(output=_maybe_export_output(f"No matches found for '{params.content}' in {params.path}"))

            # Format results
            result_lines = [
                f"Found {len(all_matches)} match(es) for '{params.content}':",
                ""
            ]

            current_file = None
            for match in all_matches:
                if match['file'] != current_file:
                    current_file = match['file']
                    result_lines.append(f"File: {current_file}")
                result_lines.append(f"  Line {match['line']}, Col {match['column']}: {match['content']}")

            return ToolOk(output=_maybe_export_output("\n".join(result_lines)))

        except Exception as exc:
            return ToolError(
                output="",
                message=str(exc),
                brief="Failed to find string",
            )
