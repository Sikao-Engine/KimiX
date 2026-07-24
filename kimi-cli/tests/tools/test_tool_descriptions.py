from __future__ import annotations

# ruff: noqa

from dataclasses import replace
import platform
import pytest
from inline_snapshot import snapshot

from kimi_cli.tools.agent import Agent as AgentTool
from kimi_cli.tools.file.glob import Glob
from kimi_cli.tools.file.grep_local import Grep
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.read_media import ReadMediaFile
from kimi_cli.tools.file.replace import EditFile
from kimi_cli.tools.file.write import WriteFile
from kimi_cli.tools.todo import TodoList
from kimi_cli.tools.web.fetch import FetchURL
from kimi_cli.tools.web.search import SearchWeb


def test_agent_description(agent_tool: AgentTool):
    """Test the description of Agent tool."""
    assert agent_tool.base.description == snapshot(
        """\
Start a subagent for focused tasks. Create new or resume by `agent_id`.

**Usage**
- Keep `description` short (3-5 words).
- Use `subagent_type` (default: `coder`), `model` to override.
- Use `resume` to continue existing instances with context.
- Run in foreground by default; `run_in_background=true` only for independent tasks.
- Be explicit: code or research only.
- Subagent results are private—summarize for user if needed.

**Explore Agent** — Preferred for codebase research (read-only). Use when you need >3 searches, module understanding, or concurrent investigations. Specify thoroughness: "quick" (find file), "medium" (understand module), "thorough" (architecture analysis).

**When Not To Use**
Reading known paths, small file searches, tasks completable in 1-2 tool calls.
"""
    )


def test_todo_list_description(todo_list_tool: TodoList):
    """Test the description of TodoList tool."""
    desc = todo_list_tool.base.description
    # Verify key parts of the description without doing exact snapshot
    assert "Track progress with a todo list" in desc
    assert "mode='append'" in desc
    assert "mode='overwrite'" in desc
    assert "mode='force_overwrite'" in desc
    assert "one item in_progress" in desc


def test_read_file_description(read_file_tool: ReadFile):
    """Test the description of ReadFile tool."""
    assert read_file_tool.base.description == snapshot(
        """\
Read one or more text files. `path` may be a single file path or a list of paths. `line_offset`, `n_lines`, `max_char`, and `char_offset` may each be a single value applied to all files, or a list with one value per file path. Lines over 4000 chars truncated. Max 5000 lines per file. Bytes per file scale with the model's context window (at least 102400 bytes, up to 1MiB). Negative offset = tail mode.

Each `path` may also be a glob pattern such as `./*.md` to read all matching files in a directory. Glob patterns support `*`, `?`, and `[...]`. Patterns that start with `**` are not allowed. The total number of files read in one call cannot exceed 32.
"""
    )


def test_read_media_file_description(read_media_file_tool: ReadMediaFile):
    """Test the description of ReadMediaFile tool."""
    assert read_media_file_tool.base.description == snapshot(
        """\
Read media content from a file.

**Tips:**
- Make sure you follow the description of each tool parameter.
- A `<system>` tag accompanies the media content; it summarizes the mime type, byte size and, for images, the original pixel dimensions, and states how the image was delivered (untouched, downsampled, cropped, or native resolution). When outputting coordinates, give relative coordinates first and compute absolute coordinates from the original image size. After generating or editing media via commands or scripts, read the result back before continuing.
- Large images are downsampled by default when automatic compression can safely fit them within model limits, which can blur fine detail (small text, dense UI). Compute absolute coordinates from the original dimensions reported in the `<system>` block, never by measuring the displayed copy. When the `<system>` tag reports downsampling and you need that detail, call this tool again with the `region` parameter (original-image pixel coordinates) to view a crop at full fidelity, or set `full_resolution` to true when the whole file fits the per-image byte limit. Re-reading the same file without these parameters just reproduces the same downsampled image.
- If automatic compression cannot safely produce an image within model limits, the tool returns an error and does not send the original image. Follow the error: use Shell or an available image-processing tool to create a smaller copy, then read that copy. Do not retry the unchanged file.
- The system will notify you when there is anything wrong when reading the file.
- This tool is a tool that you typically want to use in parallel. Always read multiple files in one response when possible.
- This tool can only read image or video files. To read text files, use the ReadFile tool. To list directories, use `ls` via Shell for a known directory, or Glob for pattern search.
- If the file doesn't exist or path is invalid, an error will be returned.
- The maximum size that can be read is 100MB. An error will be returned if the file is larger than this limit.
- The media content will be returned in a form that you can directly view and understand.

**Capabilities**
- This tool supports image and video files for the current model.
"""
    )


def test_glob_description(runtime):
    """Test the description of Glob tool."""
    runtime.environment = replace(runtime.environment, os_kind="Linux")
    glob_tool = Glob(runtime)
    windows_path_hint = "On Windows, the `directory` parameter accepts both Windows native paths"

    assert windows_path_hint not in glob_tool.base.description
    assert glob_tool.base.description == snapshot(
        "Find files by glob pattern.\n"
    )


def test_glob_description_on_windows(runtime):
    """Test the Windows-specific description of Glob tool."""
    runtime.environment = replace(runtime.environment, os_kind="Windows")
    glob_tool = Glob(runtime)
    windows_path_hint = "Windows: `directory` accepts native (`C:\\Users\\foo`) and POSIX-style (`/c/Users/foo`) paths. Results use backslashes — convert to forward slashes for shell commands."

    assert windows_path_hint in glob_tool.base.description


def test_grep_description(grep_tool: Grep):
    """Test the description of Grep tool."""
    assert grep_tool.base.description == snapshot(
        "Search files using ripgrep. Multiline patterns match across line boundaries. Example: pattern='def foo\\(.*?\\):' with multiline=True matches function signatures spanning multiple lines."
    )


def test_write_file_description(write_file_tool: WriteFile):
    """Test the description of WriteFile tool."""
    assert write_file_tool.base.description == snapshot(
        "Write content to a file."
    )


def test_edit_file_description(edit_file_tool: EditFile):
    """Test the description of EditFile tool."""
    assert edit_file_tool.base.description == snapshot(
        "Replace strings in text files."
    )


def test_search_web_description(search_web_tool: SearchWeb):
    """Test the description of MoonshotSearch tool."""
    assert search_web_tool.base.description == snapshot(
        "Search the web."
    )


def test_fetch_url_description(fetch_url_tool: FetchURL):
    """Test the description of FetchURL tool."""
    assert fetch_url_tool.base.description == snapshot(
        "Fetch a URL and extract main text."
    )
