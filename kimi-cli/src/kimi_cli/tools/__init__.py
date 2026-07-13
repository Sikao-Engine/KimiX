import json

import orjson
from typing import cast

import streamingjson  # type: ignore[reportMissingTypeStubs]
from kaos.path import KaosPath
from kosong.utils.typing import JsonType

from kimi_cli.utils.string import shorten_middle


class SkipThisTool(Exception):
    """Raised when a tool decides to skip itself from the loading process."""

    pass


def extract_key_argument(
    json_content: str | streamingjson.Lexer,
    tool_name: str,
    work_dir: KaosPath | None = None,
) -> str | None:
    if isinstance(json_content, streamingjson.Lexer):
        json_str = json_content.complete_json()
    else:
        json_str = json_content
    from kosong.utils.jsonx import loads_relaxed

    try:
        curr_args: JsonType = loads_relaxed(json_str)
    except (orjson.JSONDecodeError, json.JSONDecodeError):
        return None
    if not curr_args:
        return None
    key_argument: str = ""
    match tool_name:
        case "Agent":
            if not isinstance(curr_args, dict) or not curr_args.get("description"):
                return None
            key_argument = str(curr_args["description"])
        case "SendDMail":
            return None
        case "Think":
            if not isinstance(curr_args, dict) or not curr_args.get("thought"):
                return None
            key_argument = str(curr_args["thought"])
        case "TodoList":
            return None
        case "Shell":
            if not isinstance(curr_args, dict) or not curr_args.get("command"):
                return None
            key_argument = str(curr_args["command"])
        case "TaskOutput":
            if not isinstance(curr_args, dict) or not curr_args.get("task_id"):
                return None
            key_argument = str(curr_args["task_id"])
        case "TaskList":
            if not isinstance(curr_args, dict):
                return None
            key_argument = "active" if curr_args.get("active_only", True) else "all"
        case "TaskStop":
            if not isinstance(curr_args, dict) or not curr_args.get("task_id"):
                return None
            key_argument = str(curr_args["task_id"])
        case "ReadFile":
            if not isinstance(curr_args, dict) or not curr_args.get("path"):
                return None
            if work_dir is None:
                return None
            key_argument = _normalize_path(str(curr_args["path"]), work_dir)
        case "ReadMediaFile":
            if not isinstance(curr_args, dict) or not curr_args.get("path"):
                return None
            if work_dir is None:
                return None
            key_argument = _normalize_path(str(curr_args["path"]), work_dir)
        case "Glob":
            if not isinstance(curr_args, dict) or not curr_args.get("pattern"):
                return None
            key_argument = str(curr_args["pattern"])
        case "Grep":
            if not isinstance(curr_args, dict) or not curr_args.get("pattern"):
                return None
            key_argument = str(curr_args["pattern"])
        case "WriteFile":
            if not isinstance(curr_args, dict) or not curr_args.get("path"):
                return None
            if work_dir is None:
                return None
            key_argument = _normalize_path(str(curr_args["path"]), work_dir)
        case "EditFile":
            if not isinstance(curr_args, dict) or not curr_args.get("path"):
                return None
            if work_dir is None:
                return None
            key_argument = _normalize_path(str(curr_args["path"]), work_dir)
        case "SearchWeb":
            if not isinstance(curr_args, dict) or not curr_args.get("query"):
                return None
            key_argument = str(curr_args["query"])
        case "FetchURL":
            if not isinstance(curr_args, dict) or not curr_args.get("url"):
                return None
            key_argument = str(curr_args["url"])
        case _:
            if isinstance(json_content, streamingjson.Lexer):
                # lexer.json_content is list[str] based on streamingjson source code
                content: list[str] = cast(list[str], json_content.json_content)  # type: ignore[reportUnknownMemberType]
                key_argument = "".join(content)
            else:
                key_argument = json_content
    key_argument = shorten_middle(key_argument, width=50)
    return key_argument


def _normalize_path(path: str, work_dir: KaosPath) -> str:
    cwd = str(work_dir.canonical())
    if path.startswith(cwd):
        path = path[len(cwd) :].lstrip("/\\")
    return path
