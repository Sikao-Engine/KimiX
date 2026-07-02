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
    assert todo_list_tool.base.parameters == snapshot(
        {
            "additionalProperties": False,
            "properties": {
                "todos": {
                    "anyOf": [
                        {
                            "items": {
                                "properties": {
                                    "title": {
                                        "description": "Title",
                                        "maxLength": 65536,
                                        "minLength": 1,
                                        "type": "string",
                                    },
                                    "status": {
                                        "description": "Status",
                                        "enum": ["pending", "in_progress", "done"],
                                        "type": "string",
                                    },
                                    "priority": {
                                        "anyOf": [
                                            {"enum": ["low", "medium", "high"], "type": "string"},
                                            {"type": "null"},
                                        ],
                                        "default": None,
                                        "description": "Optional priority: low, medium, or high.",
                                    },
                                    "tags": {
                                        "anyOf": [
                                            {"items": {"type": "string"}, "type": "array"},
                                            {"type": "null"},
                                        ],
                                        "default": None,
                                        "description": "Optional list of tags.",
                                    },
                                    "notes": {
                                        "anyOf": [
                                            {"maxLength": 65536, "type": "string"},
                                            {"type": "null"},
                                        ],
                                        "default": None,
                                        "description": "Optional notes.",
                                    },
                                    "created_at": {
                                        "anyOf": [{"type": "number"}, {"type": "null"}],
                                        "default": None,
                                        "description": "Optional creation timestamp (Unix epoch).",
                                    },
                                    "updated_at": {
                                        "anyOf": [{"type": "number"}, {"type": "null"}],
                                        "default": None,
                                        "description": "Optional last-update timestamp (Unix epoch).",
                                    },
                                },
                                "required": ["title", "status"],
                                "type": "object",
                            },
                            "type": "array",
                        },
                        {
                            "properties": {
                                "title": {
                                    "description": "Title",
                                    "maxLength": 65536,
                                    "minLength": 1,
                                    "type": "string",
                                },
                                "status": {
                                    "description": "Status",
                                    "enum": ["pending", "in_progress", "done"],
                                    "type": "string",
                                },
                                "priority": {
                                    "anyOf": [
                                        {"enum": ["low", "medium", "high"], "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                    "description": "Optional priority: low, medium, or high.",
                                },
                                "tags": {
                                    "anyOf": [
                                        {"items": {"type": "string"}, "type": "array"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                    "description": "Optional list of tags.",
                                },
                                "notes": {
                                    "anyOf": [
                                        {"maxLength": 65536, "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                    "description": "Optional notes.",
                                },
                                "created_at": {
                                    "anyOf": [{"type": "number"}, {"type": "null"}],
                                    "default": None,
                                    "description": "Optional creation timestamp (Unix epoch).",
                                },
                                "updated_at": {
                                    "anyOf": [{"type": "number"}, {"type": "null"}],
                                    "default": None,
                                    "description": "Optional last-update timestamp (Unix epoch).",
                                },
                            },
                            "required": ["title", "status"],
                            "type": "object",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Updated list, a single Todo item, or omit to return current list unchanged.",
                },
                "mode": {
                    "default": "append",
                    "description": "Write mode: 'overwrite' safely replaces the existing todo list only when all old todos are done; 'append' merges the provided todos into the existing list (existing titles are updated, new titles are appended); 'force_overwrite' replaces the existing todo list unconditionally.",
                    "enum": ["overwrite", "append", "force_overwrite"],
                    "type": "string",
                },
                "delete": {
                    "anyOf": [
                        {"items": {"type": "string"}, "type": "array"},
                        {"type": "string"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Remove todos by exact title. Accepts a single title or a list of titles.",
                },
                "reorder": {
                    "anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}],
                    "default": None,
                    "description": "Reorder existing todos by listing every title in the desired order.",
                },
                "status_filter": {
                    "anyOf": [
                        {
                            "items": {"enum": ["pending", "in_progress", "done"], "type": "string"},
                            "type": "array",
                        },
                        {"enum": ["pending", "in_progress", "done"], "type": "string"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Filter read output by status(es).",
                },
                "search": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Fuzzy search todo titles when reading.",
                },
                "limit": {
                    "anyOf": [{"minimum": 0, "type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Limit the number of todos returned when reading.",
                },
                "offset": {
                    "anyOf": [{"minimum": 0, "type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Offset into the read results.",
                },
                "mark_all": {
                    "anyOf": [
                        {"enum": ["pending", "in_progress", "done"], "type": "string"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Transition all existing todos to the given status.",
                },
                "mark_matching": {
                    "anyOf": [
                        {
                            "additionalProperties": {
                                "enum": ["pending", "in_progress", "done"],
                                "type": "string",
                            },
                            "type": "object",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Transition todos whose titles contain each key (case-insensitive) to the corresponding status.",
                },
                "archive_done": {
                    "default": False,
                    "description": "Move completed todos to a separate archive list.",
                    "type": "boolean",
                },
            },
            "type": "object",
        }
    )


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
    assert read_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "anyOf": [
                        {"type": "string"},
                        {"items": {"type": "string"}, "type": "array"},
                    ],
                    "description": "File path, or a list of file paths. Each path may also be a glob pattern such as `*.py`; only the final path component may contain wildcards (`*`, `?`, `[...]`), and recursive patterns starting with `**` are not allowed. Absolute for files outside working directory.",
                },
                "line_offset": {
                    "anyOf": [
                        {"type": "integer"},
                        {"items": {"type": "integer"}, "type": "array"},
                    ],
                    "default": 1,
                    "description": "Start line, 1-based. Negative reads from end. Max abs 5000. May be a single integer applied to all files, or a list with one integer per file path.",
                },
                "n_lines": {
                    "anyOf": [
                        {"type": "integer"},
                        {"items": {"type": "integer"}, "type": "array"},
                    ],
                    "default": 5000,
                    "description": "Lines to read, max 5000. May be a single integer applied to all files, or a list with one integer per file path.",
                },
                "max_char": {
                    "anyOf": [
                        {"type": "integer"},
                        {"items": {"type": "integer"}, "type": "array"},
                    ],
                    "default": 65536,
                    "description": "Maximum number of characters to return. May be a single integer applied to all files, or a list with one integer per file path.",
                },
                "char_offset": {
                    "anyOf": [
                        {"type": "integer"},
                        {"items": {"type": "integer"}, "type": "array"},
                    ],
                    "default": 0,
                    "description": "Character offset to start returning from. May be a single integer applied to all files, or a list with one integer per file path.",
                },
            },
            "required": ["path"],
            "type": "object",
        }
    )


def test_read_media_file_params_schema(read_media_file_tool: ReadMediaFile):
    """Test the schema of ReadMediaFile tool parameters."""
    assert read_media_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "Media path. Absolute required outside work dir.",
                    "type": "string",
                }
            },
            "required": ["path"],
            "type": "object",
        }
    )


def test_glob_params_schema(glob_tool: Glob):
    """Test the schema of Glob tool parameters."""
    assert glob_tool.base.parameters == snapshot(
        {
            "properties": {
                "pattern": {
                    "description": "Glob pattern. Never start with `**`.",
                    "type": "string",
                },
                "directory": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Absolute search path. Defaults to working directory.",
                },
                "include_dirs": {
                    "default": True,
                    "description": "Include directories in results.",
                    "type": "boolean",
                },
                "include_ignored": {
                    "default": False,
                    "description": "Include .gitignore files.",
                    "type": "boolean",
                },
                "timeout": {
                    "default": 10,
                    "description": "Maximum time in seconds to wait for the search to complete.",
                    "minimum": 1,
                    "type": "integer",
                },
            },
            "required": ["pattern"],
            "type": "object",
        }
    )


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
                    "default": 250,
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
    assert write_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "File path. Absolute paths required outside the working directory.",
                    "type": "string",
                },
                "content": {
                    "description": "Content to write.",
                    "type": "string",
                },
                "mode": {
                    "default": "overwrite",
                    "description": "Write mode: overwrite or append.",
                    "enum": ["overwrite", "append"],
                    "type": "string",
                },
            },
            "required": ["path", "content"],
            "type": "object",
        }
    )


def test_edit_file_params_schema(edit_file_tool: EditFile):
    """Test the schema of EditFile tool parameters."""
    assert edit_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "File path. Absolute path required outside working directory.",
                    "type": "string",
                },
                "edit": {
                    "anyOf": [
                        {
                            "properties": {
                                "old": {
                                    "description": "String to replace.",
                                    "type": "string",
                                },
                                "new": {
                                    "description": "Replacement string.",
                                    "type": "string",
                                },
                                "replace_all": {
                                    "default": False,
                                    "description": "Replace all occurrences.",
                                    "type": "boolean",
                                },
                            },
                            "required": ["old", "new"],
                            "type": "object",
                        },
                        {
                            "items": {
                                "properties": {
                                    "old": {
                                        "description": "String to replace.",
                                        "type": "string",
                                    },
                                    "new": {
                                        "description": "Replacement string.",
                                        "type": "string",
                                    },
                                    "replace_all": {
                                        "default": False,
                                        "description": "Replace all occurrences.",
                                        "type": "boolean",
                                    },
                                },
                                "required": ["old", "new"],
                                "type": "object",
                            },
                            "type": "array",
                        },
                    ],
                    "description": "One or more edits.",
                },
            },
            "required": ["path", "edit"],
            "type": "object",
        }
    )


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
    assert fetch_url_tool.base.parameters == snapshot(
        {
            "properties": {
                "url": {
                    "description": "URL to fetch.",
                    "type": "string",
                }
            },
            "required": ["url"],
            "type": "object",
        }
    )
