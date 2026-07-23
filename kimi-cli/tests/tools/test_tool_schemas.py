from __future__ import annotations

# ruff: noqa

from inline_snapshot import snapshot

from kimi_cli.tools.agent import Agent as AgentTool
from kimi_cli.tools.background import TaskList, TaskOutput, TaskStop
from kimi_cli.tools.dmail import SendDMail
from kimi_cli.tools.file.glob import Glob
from kimi_cli.tools.file.grep_local import Grep
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.read_media import ReadMediaFile
from kimi_cli.tools.file.replace import EditFile
from kimi_cli.tools.file.write import WriteFile
from kimi_cli.tools.shell import Shell
from kimi_cli.tools.think import Think
from kimi_cli.tools.todo import TodoList
from kimi_cli.tools.web.fetch import FetchURL
from kimi_cli.tools.web.search import SearchWeb


def test_agent_params_schema(agent_tool: AgentTool):
    """Test the schema of Agent tool parameters."""
    assert agent_tool.base.parameters == snapshot(
        {
            "properties": {
                "description": {
                    "description": "Short task label (3–5 words).",
                    "type": "string",
                },
                "prompt": {
                    "description": "Task for the agent.",
                    "type": "string",
                },
                "subagent_type": {
                    "default": "coder",
                    "description": "Built-in agent type (default: coder).",
                    "type": "string",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Optional model override.",
                },
                "resume": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Agent ID to resume.",
                },
                "run_in_background": {
                    "default": False,
                    "description": "Run in background.",
                    "type": "boolean",
                },
                "timeout": {
                    "anyOf": [
                        {"maximum": 3600, "minimum": 30, "type": "integer"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Timeout in seconds (30–3600).",
                },
            },
            "required": ["description", "prompt"],
            "type": "object",
        }
    )


def test_send_dmail_params_schema(send_dmail_tool: SendDMail):
    """Test the schema of SendDMail tool parameters."""
    assert send_dmail_tool.base.parameters == snapshot(
        {
            "properties": {
                "message": {"description": "The message to send.", "type": "string"},
                "checkpoint_id": {
                    "description": "The checkpoint to send the message back to.",
                    "minimum": 0,
                    "type": "integer",
                },
            },
            "required": ["message", "checkpoint_id"],
            "type": "object",
        }
    )


def test_think_params_schema(think_tool: Think):
    """Test the schema of Think tool parameters."""
    assert think_tool.base.parameters == snapshot(
        {
            "properties": {
                "thought": {
                    "description": "Thought to log.",
                    "type": "string",
                }
            },
            "required": ["thought"],
            "type": "object",
        }
    )


def test_todo_list_params_schema(todo_list_tool: TodoList):
    """Test the schema of TodoList tool parameters."""
    schema = todo_list_tool.base.parameters
    # Verify top-level structure
    assert schema.get("additionalProperties") is False
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    # Verify required properties exist
    assert "todos" in props
    assert "mode" in props
    assert "match_mode" in props
    assert "auto_fix" in props
    # Verify mode enum values
    assert props["mode"]["enum"] == ["overwrite", "append", "force_overwrite"]
    assert props["mode"]["default"] == "append"
    # Verify match_mode enum values
    assert props["match_mode"]["enum"] == ["exact", "fuzzy"]
    assert props["match_mode"]["default"] == "exact"
    # Verify auto_fix type
    assert props["auto_fix"]["type"] == "boolean"
    assert props["auto_fix"]["default"] is False
    # Verify todos has Todo structure (may be $ref or inlined)
    todos_props = str(props["todos"])
    assert "title" in todos_props and "status" in todos_props
    # Verify sub-schema has notes field in the inline todos definition
    assert "notes" in str(props["todos"])


def test_shell_params_schema(shell_tool: Shell):
    """Test the schema of Shell tool parameters."""
    assert shell_tool.base.parameters == snapshot(
        {
            "properties": {
                "command": {
                    "description": "Command to execute.",
                    "type": "string",
                },
                "timeout": {
                    "default": 60,
                    "description": "Timeout in seconds.",
                    "maximum": 86400,
                    "minimum": 1,
                    "type": "integer",
                },
                "run_in_background": {
                    "default": False,
                    "description": "Run as background task.",
                    "type": "boolean",
                },
                "description": {
                    "default": "",
                    "description": "Background task description. Required for background tasks.",
                    "type": "string",
                },
            },
            "required": ["command"],
            "type": "object",
        }
    )


def test_task_output_params_schema(task_output_tool: TaskOutput):
    assert task_output_tool.base.parameters == snapshot(
        {
            "properties": {
                "task_id": {
                    "description": "Task ID.",
                    "type": "string",
                },
                "block": {
                    "default": False,
                    "description": "Wait for task completion.",
                    "type": "boolean",
                },
                "timeout": {
                    "default": 30,
                    "description": "Wait timeout in seconds.",
                    "maximum": 3600,
                    "minimum": 0,
                    "type": "integer",
                },
            },
            "required": ["task_id"],
            "type": "object",
        }
    )


def test_task_list_params_schema(task_list_tool: TaskList):
    assert task_list_tool.base.parameters == snapshot(
        {
            "properties": {
                "active_only": {
                    "default": True,
                    "description": "Only active tasks.",
                    "type": "boolean",
                },
                "limit": {
                    "default": 20,
                    "description": "Result limit.",
                    "maximum": 100,
                    "minimum": 1,
                    "type": "integer",
                },
            },
            "type": "object",
        }
    )


def test_task_stop_params_schema(task_stop_tool: TaskStop):
    assert task_stop_tool.base.parameters == snapshot(
        {
            "properties": {
                "task_id": {
                    "description": "Task ID.",
                    "type": "string",
                },
                "reason": {
                    "default": "Stopped by TaskStop",
                    "description": "Stop reason.",
                    "type": "string",
                },
            },
            "required": ["task_id"],
            "type": "object",
        }
    )


def test_read_file_params_schema(read_file_tool: ReadFile):
    """Test the schema of ReadFile tool parameters."""
    schema = read_file_tool.base.parameters
    props = schema.get("properties", {})
    # Verify top-level properties exist
    assert "path" in props
    assert "line_offset" in props
    assert "n_lines" in props
    assert "max_char" in props
    assert "char_offset" in props
    assert "glob" in props
    assert "show_line_numbers" in props
    # Verify line_offset is int (not list)
    assert props["line_offset"]["type"] == "integer"
    assert props["n_lines"]["type"] == "integer"
    assert props["max_char"]["type"] == "integer"
    # Verify glob is boolean
    assert props["glob"]["type"] == "boolean"
    # Verify show_line_numbers is boolean
    assert props["show_line_numbers"]["type"] == "boolean"


def test_read_media_file_params_schema(read_media_file_tool: ReadMediaFile):
    """Test the schema of ReadMediaFile tool parameters."""
    schema = read_media_file_tool.base.parameters
    props = schema.get("properties", {})
    # Verify required properties
    assert "path" in props
    assert props["path"]["type"] == "string"
    # Verify new params exist
    assert "info_only" in props
    assert "max_dimension" in props
    assert "quality" in props
    assert "auto_convert" in props
    assert "region_pct" in props
    assert "region" in props
    assert "full_resolution" in props
    # Verify quality bounds
    assert props["quality"]["default"] == 85
    assert props["quality"]["minimum"] == 1
    assert props["quality"]["maximum"] == 100
    # Verify auto_convert default
    assert props["auto_convert"]["default"] is True
    # Verify info_only default
    assert props["info_only"]["default"] is False


def test_glob_params_schema(glob_tool: Glob):
    """Test the schema of Glob tool parameters."""
    schema = glob_tool.base.parameters
    props = schema.get("properties", {})
    # Verify required properties
    assert "pattern" in props
    assert props["pattern"]["type"] == "string"
    # Verify new/modified params
    assert "directory" in props
    assert "include_dirs" in props
    assert props["include_dirs"]["default"] is False  # Changed from True to False
    assert "respect_gitignore" in props
    assert props["respect_gitignore"]["default"] is True
    assert props["respect_gitignore"]["type"] == "boolean"
    assert "include_ignored" in props  # Deprecated but still present
    assert "verbose" in props
    assert props["verbose"]["default"] is False
    assert props["verbose"]["type"] == "boolean"
    assert "timeout" in props


def test_grep_params_schema(grep_tool: Grep):
    """Test the schema of Grep tool parameters."""
    assert grep_tool.base.parameters == snapshot(
        {
            "properties": {
                "pattern": {
                    "description": "Regex pattern.",
                    "type": "string",
                },
                "path": {
                    "default": ".",
                    "description": "Search target directory or file.",
                    "type": "string",
                },
                "glob": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Glob filter.",
                },
                "output_mode": {
                    "default": "files_with_matches",
                    "description": "Output format: 'files_with_matches', 'count_matches', or 'content'.",
                    "enum": ["files_with_matches", "count_matches", "content"],
                    "type": "string",
                },
                "-B": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Lines before match (content mode only).",
                },
                "-A": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Lines after match (content mode only).",
                },
                "-C": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Lines around match (content mode only).",
                },
                "-n": {
                    "default": True,
                    "description": "Show line numbers (content mode only).",
                    "type": "boolean",
                },
                "-i": {
                    "default": False,
                    "description": "Case-insensitive search.",
                    "type": "boolean",
                },
                "type": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "File type filter.",
                },
                "head_limit": {
                    "anyOf": [{"minimum": 0, "type": "integer"}, {"type": "null"}],
                    "default": 500,
                    "description": "Max results (0 = unlimited).",
                },
                "offset": {
                    "default": 0,
                    "description": "Skip first N results.",
                    "minimum": 0,
                    "type": "integer",
                },
                "multiline": {
                    "default": False,
                    "description": "Multiline regex mode.",
                    "type": "boolean",
                },
                "include_ignored": {
                    "default": False,
                    "description": "Include .gitignore files.",
                    "type": "boolean",
                },
                "timeout": {
                    "default": 60,
                    "description": "Maximum time in seconds to wait for the search to complete.",
                    "minimum": 1,
                    "type": "integer",
                },
            },
            "required": ["pattern"],
            "type": "object",
        }
    )


def test_write_file_params_schema(write_file_tool: WriteFile):
    """Test the schema of WriteFile tool parameters."""
    schema = write_file_tool.base.parameters
    props = schema.get("properties", {})
    # Verify required properties
    assert "path" in props
    assert "content" in props
    assert "mode" in props
    assert "auto_fix_json" in props
    assert "mkdir" in props
    assert "show_diff" in props
    # Verify types
    assert props["path"]["type"] == "string"
    assert props["content"]["type"] == "string"
    assert props["mode"]["enum"] == ["overwrite", "append"]
    assert props["auto_fix_json"]["type"] == "boolean"
    assert props["auto_fix_json"]["default"] is True
    assert props["mkdir"]["type"] == "boolean"
    assert props["mkdir"]["default"] is True
    assert props["show_diff"]["type"] == "boolean"
    assert props["show_diff"]["default"] is False


def test_edit_file_params_schema(edit_file_tool: EditFile):
    """Test the schema of EditFile tool parameters."""
    schema = edit_file_tool.base.parameters
    props = schema.get("properties", {})
    # Verify required properties
    assert "path" in props
    assert "edit" in props
    assert props["path"]["type"] == "string"
    # edit accepts both a single Edit object and a list of Edit objects
    edit_schema = props["edit"]
    assert "anyOf" in edit_schema
    # Find the array option in anyOf
    array_schema = None
    for opt in edit_schema["anyOf"]:
        if opt.get("type") == "array":
            array_schema = opt
            break
    assert array_schema is not None, "Expected an array option in anyOf"
    # Verify edit item properties include new fields
    item_schema = array_schema.get("items", {})
    item_props = item_schema.get("properties", {})
    assert "old" in item_props
    assert "new" in item_props
    assert "replace_all" in item_props
    assert "max_replacements" in item_props
    assert "match_mode" in item_props
    # Verify match_mode enum
    assert item_props["match_mode"]["enum"] == ["exact", "fuzzy"]
    assert item_props["match_mode"]["default"] == "fuzzy"
    # Verify max_replacements is nullable integer (None = unlimited)
    assert item_props["max_replacements"]["anyOf"][0]["minimum"] == 1


def test_search_web_params_schema(search_web_tool: SearchWeb):
    """Test the schema of MoonshotSearch tool parameters."""
    assert search_web_tool.base.parameters == snapshot(
        {
            "properties": {
                "query": {
                    "description": "Search query.",
                    "type": "string",
                },
                "limit": {
                    "default": 5,
                    "description": "Number of results. Prefer a specific query over a high limit.",
                    "maximum": 20,
                    "minimum": 1,
                    "type": "integer",
                },
                "include_content": {
                    "default": False,
                    "description": "Include full page content. Increases token usage.",
                    "type": "boolean",
                },
            },
            "required": ["query"],
            "type": "object",
        }
    )


def test_fetch_url_params_schema(fetch_url_tool: FetchURL):
    """Test the schema of FetchURL tool parameters."""
    schema = fetch_url_tool.base.parameters
    props = schema.get("properties", {})
    # Verify required properties
    assert "url" in props
    assert "timeout" in props
    assert "method" in props
    assert "headers" in props
    assert "body" in props
    assert "follow_redirects" in props
    assert "max_redirects" in props
    # Verify types
    assert props["url"]["type"] == "string"
    assert props["timeout"]["default"] == 30.0
    assert props["timeout"]["minimum"] == 1.0
    assert props["timeout"]["maximum"] == 300.0
    assert props["method"]["enum"] == ["GET", "POST"]
    assert props["follow_redirects"]["default"] is True
    assert props["max_redirects"]["default"] == 5
    assert props["max_redirects"]["maximum"] == 20
