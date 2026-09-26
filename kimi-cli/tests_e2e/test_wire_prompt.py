from __future__ import annotations

from typing import Any

from inline_snapshot import snapshot

from tests_e2e.wire_helpers import (
    build_ask_user_tool_call,
    build_question_response,
    build_todo_call,
    collect_until_request,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_response,
    read_response,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _find_event(messages: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    for msg in messages:
        if msg.get("method") != "event":
            continue
        params = msg.get("params")
        if isinstance(params, dict) and params.get("type") == event_type:
            return params
    raise AssertionError(f"Missing event {event_type}")


def test_basic_prompt_events(tmp_path) -> None:
    script = "\n".join(
        [
            "id: scripted-1",
            'usage: {"input_other": 5, "output": 2}',
            "text: Hello wire",
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "hi"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "Hello wire"},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": 5e-05,
                        "context_tokens": 5,
                        "max_context_tokens": 100000,
                        "token_usage": {
                            "input_other": 5,
                            "output": 2,
                            "input_cache_read": 0,
                            "input_cache_creation": 0,
                        },
                        "message_id": "scripted-1",
                        "mcp_status": None,
                    },
                }, {
    "method": "event",
    "type": "LLMToolsSnapshot",
    "payload": {
        "hash": "0192290bb02ad3977a519ac42212525a6192b4615f784971f5d243f9c819ff96",
        "tools": [
            {
                "name": "subagent",
                "description": """\
Start a subagent for focused tasks; create new or resume by agent_id.

Usage
- Keep description short (3-5 words).
- subagent_type (default: coder), model to override; resume continues existing instances.
- Foreground by default; run_in_background=true only for independent tasks.
- Be explicit: code or research only.

Explore Agent — preferred for read-only codebase research. Use when you need >3 searches, module understanding, or concurrent investigations. Thoroughness: "quick" (find file), "medium" (understand module), "thorough" (architecture analysis).\
""",
                "parameters": {
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
                },
            },
            {   'name': 'todo_list',
                'description': 'Read or write the todo plan — one tool, one item shape, every operation.\n'
                               '\n'
                               'Item shape (all modes): `{title, status?, notes?, children?}` plus the edit '
                               'keys `parent`, `rename_to`, `complete`. `title` is the one short imperative '
                               'line that identifies the item and the only required key; `notes` is optional '
                               'detail (an item of only `notes` has no identity).\n'
                               '\n'
                               'Dispatch:\n'
                               '- `todos` omitted → read the current tree.\n'
                               "- mode='merge' (default) → upsert each item: an existing title patches it in "
                               'place (omitted fields keep their value), an unknown title creates it. `parent` '
                               "/ top-level `scope` pick the sub-tree ('' = root).\n"
                               "- mode='replace' → the list IS the tree (children included); needs all "
                               'existing todos done unless force=True.\n'
                               "- mode='clear' → empty the tree (all-done guard unless force=True).\n"
                               '\n'
                               'Near-duplicate titles are treated as the same task, not a new one: an incoming '
                               'title that differs from an existing one only in numbering, punctuation, case '
                               "or word order hits the conflict policy — on_conflict='error' (default) refuses "
                               'the call and names the existing title plus the exact payload to send instead; '
                               "'reuse' patches that item; 'append' really adds a second one.\n"
                               '\n'
                               'Invariants: exactly one item in_progress (auto_fix=True demotes earlier ones, '
                               'keeping the last listed); done items never move back to pending/in_progress '
                               'unless force=True; done items dropped by replace/clear are archived.',
                'parameters': {   'additionalProperties': False,
                                  'properties': {   'todos': {   'description': 'The items to write; omit to '
                                                                                'READ the tree. In the default '
                                                                                "mode='merge' this is an "
                                                                                'upsert batch — send only the '
                                                                                'items you mean to touch, each '
                                                                                'with `title` plus any of '
                                                                                'status / notes / children / '
                                                                                'rename_to / parent / '
                                                                                'complete: an existing title '
                                                                                'is patched, an unknown one is '
                                                                                "created. In mode='replace' "
                                                                                'send the COMPLETE tree '
                                                                                'instead. A single object, a '
                                                                                'bare title string or a JSON '
                                                                                'string of those forms also '
                                                                                'work. Accepts `todos` or '
                                                                                '`items`.',
                                                                 'items': {   'additionalProperties': False,
                                                                              'description': 'One todo item.\n'
                                                                                             '\n'
                                                                                             'The single item '
                                                                                             'shape for the '
                                                                                             'one todo tool: '
                                                                                             'the same object '
                                                                                             'expresses a\n'
                                                                                             'whole-tree write '
                                                                                             "(``mode='replace'``), "
                                                                                             'an upsert '
                                                                                             "(``mode='merge'``) "
                                                                                             'and a\n'
                                                                                             'targeted edit '
                                                                                             '(``parent`` / '
                                                                                             '``rename_to`` / '
                                                                                             '``complete``). '
                                                                                             'Fields that '
                                                                                             'only\n'
                                                                                             'make sense for '
                                                                                             'an edit are '
                                                                                             'ignored by a '
                                                                                             'whole-tree '
                                                                                             'write.',
                                                                              'properties': {   'title': {   'description': 'Required. '
                                                                                                                            'The '
                                                                                                                            'task '
                                                                                                                            'title: '
                                                                                                                            'one '
                                                                                                                            'short '
                                                                                                                            'imperative '
                                                                                                                            'line, '
                                                                                                                            'and '
                                                                                                                            'the '
                                                                                                                            "item's "
                                                                                                                            'identity '
                                                                                                                            '— '
                                                                                                                            "mode='merge' "
                                                                                                                            'matches '
                                                                                                                            'items '
                                                                                                                            'by '
                                                                                                                            'it '
                                                                                                                            'and '
                                                                                                                            '`parent`/`scope` '
                                                                                                                            'look '
                                                                                                                            'items '
                                                                                                                            'up '
                                                                                                                            'by '
                                                                                                                            'it, '
                                                                                                                            'so '
                                                                                                                            'it '
                                                                                                                            'must '
                                                                                                                            'always '
                                                                                                                            'be '
                                                                                                                            'sent. '
                                                                                                                            'Accepts '
                                                                                                                            '`title`, '
                                                                                                                            '`content`, '
                                                                                                                            '`task`, '
                                                                                                                            '`todo`, '
                                                                                                                            '`item` '
                                                                                                                            'or '
                                                                                                                            '`name`.',
                                                                                                             'maxLength': 65536,
                                                                                                             'minLength': 1,
                                                                                                             'type': 'string'},
                                                                                                'status': {   'default': 'pending',
                                                                                                              'description': 'One '
                                                                                                                             'of: '
                                                                                                                             'pending, '
                                                                                                                             'in_progress, '
                                                                                                                             'done '
                                                                                                                             '(or '
                                                                                                                             'completed). '
                                                                                                                             'Omit '
                                                                                                                             'to '
                                                                                                                             'keep '
                                                                                                                             'an '
                                                                                                                             'existing '
                                                                                                                             "item's "
                                                                                                                             'status; '
                                                                                                                             'created '
                                                                                                                             'items '
                                                                                                                             'default '
                                                                                                                             'to '
                                                                                                                             'pending.',
                                                                                                              'enum': [   'pending',
                                                                                                                          'in_progress',
                                                                                                                          'done'],
                                                                                                              'type': 'string'},
                                                                                                'notes': {   'anyOf': [   {   'maxLength': 65536,
                                                                                                                              'type': 'string'},
                                                                                                                          {   'type': 'null'}],
                                                                                                             'default': None,
                                                                                                             'description': 'Optional '
                                                                                                                            'supporting '
                                                                                                                            'detail '
                                                                                                                            '(evidence, '
                                                                                                                            'file '
                                                                                                                            'paths, '
                                                                                                                            'findings). '
                                                                                                                            'Not '
                                                                                                                            'the '
                                                                                                                            'title '
                                                                                                                            '— '
                                                                                                                            'an '
                                                                                                                            'item '
                                                                                                                            'with '
                                                                                                                            'only '
                                                                                                                            '`notes` '
                                                                                                                            'has '
                                                                                                                            'no '
                                                                                                                            'identity, '
                                                                                                                            'so '
                                                                                                                            'always '
                                                                                                                            'send '
                                                                                                                            '`title` '
                                                                                                                            'too. '
                                                                                                                            'Omit '
                                                                                                                            'it '
                                                                                                                            '(or '
                                                                                                                            'send '
                                                                                                                            '"") '
                                                                                                                            'to '
                                                                                                                            'keep '
                                                                                                                            'the '
                                                                                                                            'current '
                                                                                                                            'notes.'},
                                                                                                'children': {   'description': 'Sub-todos '
                                                                                                                               'of '
                                                                                                                               'this '
                                                                                                                               'item; '
                                                                                                                               'same '
                                                                                                                               'shape, '
                                                                                                                               'any '
                                                                                                                               'depth. '
                                                                                                                               'Leave '
                                                                                                                               'empty '
                                                                                                                               'for '
                                                                                                                               'a '
                                                                                                                               'leaf. '
                                                                                                                               'A '
                                                                                                                               'bare '
                                                                                                                               'title '
                                                                                                                               'string '
                                                                                                                               'is '
                                                                                                                               'accepted '
                                                                                                                               'and '
                                                                                                                               'means '
                                                                                                                               'a '
                                                                                                                               'pending '
                                                                                                                               'item.',
                                                                                                                'items': {   'additionalProperties': False,
                                                                                                                             'description': 'One '
                                                                                                                                            'todo '
                                                                                                                                            'item.\n'
                                                                                                                                            '\n'
                                                                                                                                            'The '
                                                                                                                                            'single '
                                                                                                                                            'item '
                                                                                                                                            'shape '
                                                                                                                                            'for '
                                                                                                                                            'the '
                                                                                                                                            'one '
                                                                                                                                            'todo '
                                                                                                                                            'tool: '
                                                                                                                                            'the '
                                                                                                                                            'same '
                                                                                                                                            'object '
                                                                                                                                            'expresses '
                                                                                                                                            'a\n'
                                                                                                                                            'whole-tree '
                                                                                                                                            'write '
                                                                                                                                            "(``mode='replace'``), "
                                                                                                                                            'an '
                                                                                                                                            'upsert '
                                                                                                                                            "(``mode='merge'``) "
                                                                                                                                            'and '
                                                                                                                                            'a\n'
                                                                                                                                            'targeted '
                                                                                                                                            'edit '
                                                                                                                                            '(``parent`` '
                                                                                                                                            '/ '
                                                                                                                                            '``rename_to`` '
                                                                                                                                            '/ '
                                                                                                                                            '``complete``). '
                                                                                                                                            'Fields '
                                                                                                                                            'that '
                                                                                                                                            'only\n'
                                                                                                                                            'make '
                                                                                                                                            'sense '
                                                                                                                                            'for '
                                                                                                                                            'an '
                                                                                                                                            'edit '
                                                                                                                                            'are '
                                                                                                                                            'ignored '
                                                                                                                                            'by '
                                                                                                                                            'a '
                                                                                                                                            'whole-tree '
                                                                                                                                            'write.',
                                                                                                                             'properties': {   'title': {   'description': 'Sub-todo '
                                                                                                                                                                           'title: '
                                                                                                                                                                           'one '
                                                                                                                                                                           'short '
                                                                                                                                                                           'imperative '
                                                                                                                                                                           'line '
                                                                                                                                                                           '(required).',
                                                                                                                                                            'maxLength': 65536,
                                                                                                                                                            'minLength': 1,
                                                                                                                                                            'type': 'string'},
                                                                                                                                               'status': {   'default': 'pending',
                                                                                                                                                             'description': 'One '
                                                                                                                                                                            'of: '
                                                                                                                                                                            'pending, '
                                                                                                                                                                            'in_progress, '
                                                                                                                                                                            'done '
                                                                                                                                                                            '(or '
                                                                                                                                                                            'completed). '
                                                                                                                                                                            'Omit '
                                                                                                                                                                            'to '
                                                                                                                                                                            'keep '
                                                                                                                                                                            'an '
                                                                                                                                                                            'existing '
                                                                                                                                                                            "item's "
                                                                                                                                                                            'status; '
                                                                                                                                                                            'created '
                                                                                                                                                                            'items '
                                                                                                                                                                            'default '
                                                                                                                                                                            'to '
                                                                                                                                                                            'pending.',
                                                                                                                                                             'enum': [   'pending',
                                                                                                                                                                         'in_progress',
                                                                                                                                                                         'done'],
                                                                                                                                                             'type': 'string'},
                                                                                                                                               'notes': {   'anyOf': [   {   'maxLength': 65536,
                                                                                                                                                                             'type': 'string'},
                                                                                                                                                                         {   'type': 'null'}],
                                                                                                                                                            'default': None,
                                                                                                                                                            'description': 'Optional '
                                                                                                                                                                           'detail '
                                                                                                                                                                           'for '
                                                                                                                                                                           'this '
                                                                                                                                                                           'sub-todo; '
                                                                                                                                                                           'the '
                                                                                                                                                                           'title '
                                                                                                                                                                           'is '
                                                                                                                                                                           'the '
                                                                                                                                                                           'identity.'}},
                                                                                                                             'required': [   'title'],
                                                                                                                             'type': 'object'},
                                                                                                                'type': 'array'},
                                                                                                'parent': {   'anyOf': [   {   'type': 'string'},
                                                                                                                           {   'type': 'null'}],
                                                                                                              'default': None,
                                                                                                              'description': 'Scope '
                                                                                                                             'this '
                                                                                                                             'item '
                                                                                                                             'to '
                                                                                                                             'the '
                                                                                                                             'children '
                                                                                                                             'of '
                                                                                                                             'the '
                                                                                                                             'named '
                                                                                                                             'todo '
                                                                                                                             '(both '
                                                                                                                             'its '
                                                                                                                             'lookup '
                                                                                                                             'and '
                                                                                                                             'its '
                                                                                                                             'creation). '
                                                                                                                             '"" '
                                                                                                                             '= '
                                                                                                                             'root '
                                                                                                                             'scope. '
                                                                                                                             'Overrides '
                                                                                                                             'the '
                                                                                                                             'top-level '
                                                                                                                             '`scope`.'},
                                                                                                'rename_to': {   'anyOf': [   {   'type': 'string'},
                                                                                                                              {   'type': 'null'}],
                                                                                                                 'default': None,
                                                                                                                 'description': 'Rename '
                                                                                                                                'the '
                                                                                                                                'matched '
                                                                                                                                'item '
                                                                                                                                'to '
                                                                                                                                'this '
                                                                                                                                'title '
                                                                                                                                'instead '
                                                                                                                                'of '
                                                                                                                                'editing '
                                                                                                                                'a '
                                                                                                                                'field. '
                                                                                                                                'Renaming '
                                                                                                                                'onto '
                                                                                                                                'an '
                                                                                                                                'existing '
                                                                                                                                'title '
                                                                                                                                'is '
                                                                                                                                'rejected.'},
                                                                                                'complete': {   'default': False,
                                                                                                                'description': 'Mark '
                                                                                                                               'this '
                                                                                                                               'item '
                                                                                                                               'and '
                                                                                                                               'its '
                                                                                                                               'whole '
                                                                                                                               'sub-tree '
                                                                                                                               'done '
                                                                                                                               'in '
                                                                                                                               'one '
                                                                                                                               'call. '
                                                                                                                               'Not '
                                                                                                                               'combined '
                                                                                                                               'with '
                                                                                                                               'an '
                                                                                                                               'explicit '
                                                                                                                               'pending/in_progress '
                                                                                                                               'status '
                                                                                                                               'or '
                                                                                                                               'with '
                                                                                                                               '`children`.',
                                                                                                                'type': 'boolean'},
                                                                                                'fuzzy': {   'default': True,
                                                                                                             'description': 'Per-item '
                                                                                                                            'override '
                                                                                                                            'of '
                                                                                                                            'the '
                                                                                                                            'top-level '
                                                                                                                            '`fuzzy`.',
                                                                                                             'type': 'boolean'},
                                                                                                'force': {   'default': False,
                                                                                                             'description': 'Per-item '
                                                                                                                            'override '
                                                                                                                            'of '
                                                                                                                            'the '
                                                                                                                            'top-level '
                                                                                                                            '`force`.',
                                                                                                             'type': 'boolean'}},
                                                                              'required': ['title'],
                                                                              'type': 'object'},
                                                                 'type': 'array'},
                                                    'mode': {   'default': 'merge',
                                                                'description': "'merge' (default) upserts the "
                                                                               "given items; 'replace' makes "
                                                                               'the given list the whole tree '
                                                                               'and needs every existing todo '
                                                                               'done unless force=True; '
                                                                               '"clear" empties the tree (same '
                                                                               'guard).',
                                                                'enum': ['merge', 'replace', 'clear'],
                                                                'type': 'string'},
                                                    'scope': {   'anyOf': [   {'type': 'string'},
                                                                              {'type': 'null'}],
                                                                 'default': None,
                                                                 'description': 'Restrict this call to the '
                                                                                'children of the named todo; '
                                                                                '"" means the root. An item\'s '
                                                                                'own `parent` wins.'},
                                                    'on_conflict': {   'default': 'error',
                                                                       'description': 'What to do when an '
                                                                                      'incoming title is a '
                                                                                      'near-duplicate of an '
                                                                                      'existing one (same '
                                                                                      'words, different '
                                                                                      'numbering / punctuation '
                                                                                      "/ case): 'error' "
                                                                                      '(default) refuses the '
                                                                                      'call and names the '
                                                                                      'existing title plus the '
                                                                                      'exact payload to send '
                                                                                      "instead; 'reuse' "
                                                                                      'patches that item; '
                                                                                      "'append' really adds a "
                                                                                      'second one.',
                                                                       'enum': ['error', 'reuse', 'append'],
                                                                       'type': 'string'},
                                                    'fuzzy': {   'default': True,
                                                                 'description': 'When True (default) a title '
                                                                                'that misses exactly may still '
                                                                                'match the nearest existing '
                                                                                'todo; False requires exact '
                                                                                'titles.',
                                                                 'type': 'boolean'},
                                                    'force': {   'default': False,
                                                                 'description': 'Bypass the guards: the '
                                                                                'all-done requirement of '
                                                                                'replace/clear, reopening a '
                                                                                'done item, renaming onto one, '
                                                                                'and the single-in_progress '
                                                                                'and regression checks.',
                                                                 'type': 'boolean'},
                                                    'auto_fix': {   'default': True,
                                                                    'description': 'When True (default) and '
                                                                                   'several items are '
                                                                                   'in_progress, the last '
                                                                                   'listed one is kept and '
                                                                                   'earlier ones are marked '
                                                                                   'done; False errors '
                                                                                   'instead.',
                                                                    'type': 'boolean'}},
                                  'type': 'object'}},
            {
                "name": "retrieve",
                "description": "Retrieve past conversation history, including compacted/archived turns. Use `query` to search (natural language, relevance-ranked with a recency boost) or `id` to fetch a specific turn (e.g. a `prune_<n>` reference left by context pruning).",
                "parameters": {
                    "properties": {
                        "query": {
                            "default": "",
                            "description": "Search past conversation history (BM25 with recency boost) for this natural-language query.",
                            "type": "string",
                        },
                        "id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Fetch a specific history turn by id (e.g. '0' or 'prune_0').",
                        },
                        "k": {
                            "default": 3,
                            "description": "Maximum number of history turns to return.",
                            "maximum": 10,
                            "minimum": 1,
                            "type": "integer",
                        },
                    },
                    "type": "object",
                },
            },
            {
                "name": "read",
                "description": """\
Read a UTF-8 text file and return line-numbered content.
file_path: single path or list; offset/limit: scalar or one per file. Lines over 4000 chars truncated; max 5000 lines per file; bytes scale with context (≥102400, up to 1MiB). Negative offset = tail mode. A file_path glob (e.g. ./*.md) reads up to 32 files. Prefer glob/grep to find/search, then read.

Rich formats (one per call; scalar params apply to every file in a multi-file read):
- Archives (zip/jar/war/apk/whl/cbz, tar/tgz/tbz2/txz, bare gz/bz2/xz): read data.zip lists up to 500 root entries; archive_member="src/main.py" reads one member as text. Traversal (.., absolute, backslash) rejected; binary members get an explicit notice.
- SQLite (.sqlite/.sqlite3/.db/.db3): read app.db lists tables with counts; sql_table/sql_where/sql_order/sql_limit/sql_offset paginate rows; sql_query runs raw read-only SELECT (≤1000 rows; rejects ;, comments, LIMIT/UNION/ATTACH).
- PDF screenshots: read doc.pdf with pdf_page=3 renders page 3 as PNG (requires image_in); DPI falls back 150→96→72 on over-budget.
- Document markdown: render_markdown=True converts .docx to markdown (headings, code fences, pipe tables), .md/.html to clean text; False uses the legacy extractor.
- Profiles: read *.cpuprofile / *.sample.txt returns a compact bottleneck summary (hot paths, top-20 self time, idle excluded); profile_raw=True returns raw JSON/text.
- Conflict markers: reads of files containing unresolved git conflict blocks (<<<<<<< / ======= / >>>>>>>) append a warning footer with registered conflict ids. Inspect one block with read conflict://<N> (add /ours, /theirs or /base for a single side) and get a whole-file index with read <path>:conflicts. Resolve via write({ path: "conflict://<N>", content }).\
""",
                "parameters": {
                    "properties": {
                        "file_path": {
                            "anyOf": [
                                {"type": "string"},
                                {"items": {"type": "string"}, "type": "array"},
                            ],
                            "description": "Path to read, resolved by the filesystem backend. Accepts `file_path` or `path`. May be a single file path or a list of file paths. When `glob=True`, the final path component may contain wildcards (`*`, `?`, `[...]`); recursive patterns like `src/**/*.ts` are supported, only unsafe all-wildcard patterns (e.g. `**`, `**/*`) are rejected.",
                        },
                        "offset": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 1,
                            "description": "1-based first line to return. Defaults to 1. Accepts `offset` or `line_offset`. Negative reads from end. Max abs 5000. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "limit": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 2000,
                            "description": "Maximum number of lines to return. Defaults to 2000. Accepts `limit` or `n_lines`. Max 5000. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "max_char": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 16000,
                            "description": "Maximum number of characters to return (starting from char_offset). May be a scalar applied to all files, or a list with one value per file path. Default 16K balances completeness with context efficiency.",
                        },
                        "char_offset": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 0,
                            "description": "Character offset to start returning from. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "glob": {
                            "default": False,
                            "description": "When True, treat `path` as a glob pattern (e.g., '*.py', 'src/**/*.ts'). When False (default), treat `path` as a literal file path.",
                            "type": "boolean",
                        },
                        "show_line_numbers": {
                            "default": True,
                            "description": "When True (default), prefix each line with its line number (e.g., ' 42/tcontent'). When False, return raw content without line numbers.",
                            "type": "boolean",
                        },
                        "archive_member": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Archive member path to read inside an archive. When omitted, ``read`` lists the archive root entries. Applies to zip/tar/tar.gz/tgz/tar.bz2/tar.xz and bare gz/bz2/xz files.",
                        },
                        "sql_query": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Raw read-only SQL query for SQLite files. Only SELECT statements are allowed; capped at 1000 rows. Cannot be combined with sql_table/sql_where/sql_order/sql_limit/sql_offset.",
                        },
                        "sql_table": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Table name to browse in a SQLite file.",
                        },
                        "sql_where": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "WHERE fragment for sql_table (e.g. ``id > 10``). Rejects statement terminators, comments, and LIMIT/UNION/etc.",
                        },
                        "sql_order": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "ORDER BY column for sql_table, as 'col' or 'col:asc|desc'.",
                        },
                        "sql_limit": {
                            "anyOf": [
                                {"maximum": 500, "minimum": 1, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Maximum rows for sql_table queries (default 20, max 500).",
                        },
                        "sql_offset": {
                            "anyOf": [
                                {"minimum": 0, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Offset for sql_table queries.",
                        },
                        "pdf_page": {
                            "anyOf": [
                                {"minimum": 1, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Render this PDF page as an image. Requires a model with image_in capability; otherwise returns an error.",
                        },
                        "profile_raw": {
                            "default": False,
                            "description": "When True, return the raw bytes/text of .cpuprofile or .sample.txt files. When False (default), return a compact bottleneck summary.",
                            "type": "boolean",
                        },
                        "render_markdown": {
                            "default": True,
                            "description": "When True (default), extract supported documents as markdown-flavored text and convert .md/.html files to plain text. When False, use the legacy plain-text extractor.",
                            "type": "boolean",
                        },
                    },
                    "required": ["file_path"],
                    "type": "object",
                },
            },
            {
                "name": "glob",
                "description": """\
Find files by glob. Returns file paths — never directories — including hidden/ignored (VCS metadata excluded), in modification-time order: up to 100 paths (first 100 with a note; full list saved elsewhere). Does not enumerate directory entries.
Use `read` to open matches (up to 1000 collected; omitted count reported in `message`).
Windows: `path` accepts native (`C:/Users/foo`) and POSIX-style (`/c/Users/foo`) paths. Results use backslashes — convert to forward slashes for shell commands.
""",
                "parameters": {
                    "properties": {
                        "pattern": {
                            "description": 'Glob pattern to match file paths against (e.g. `**/*.ts`, `src/**/*.test.js`). A pattern with no "/" matches the basename at any depth, so `*` and `*.ts` both search the whole tree; include a separator to anchor the depth. Unsafe recursive patterns (``**``, ``**/*``, ``**/**``, etc.) are forbidden.',
                            "type": "string",
                        },
                        "path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it. Accepts `path` or `directory`.",
                        },
                        "include_dirs": {
                            "default": False,
                            "description": "Include directories in results.",
                            "type": "boolean",
                        },
                        "respect_gitignore": {
                            "default": True,
                            "description": "When True (default), skip files matched by .gitignore rules. When False, include all files regardless of .gitignore settings.",
                            "type": "boolean",
                        },
                        "include_ignored": {
                            "default": False,
                            "description": "[Deprecated] Use respect_gitignore=False instead.",
                            "type": "boolean",
                        },
                        "verbose": {
                            "default": False,
                            "description": "When True, include file size, modification time, and type for each match.",
                            "type": "boolean",
                        },
                        "timeout": {
                            "default": 10,
                            "description": "Maximum time in seconds to wait for the search to complete.",
                            "minimum": 1,
                            "type": "integer",
                        },
                        "fold": {
                            "default": 500,
                            "description": "Maximum number of result lines in the output. Longer results are head+tail folded with an omitted-count marker and the total is reported in `message`. 0 = unlimited (the MAX_MATCHES collection cap still applies).",
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["pattern"],
                    "type": "object",
                },
            },
            {
                "name": "grep",
                "description": "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. Returns the first 250 matches inline; a capped result reports where the complete match list was saved. Use read on a matched file for surrounding context. Multiline patterns match across line boundaries.",
                "parameters": {
                    "properties": {
                        "pattern": {
                            "description": "Regular expression to search for (ripgrep syntax).",
                            "type": "string",
                        },
                        "path": {
                            "anyOf": [
                                {"type": "string"},
                                {"items": {"type": "string"}, "type": "array"},
                            ],
                            "default": ".",
                            "description": 'File or directory to search. Defaults to the session workspace; a relative path resolves against it. Also accepts embedded line-range selectors (`file.py:50-100`, `file.py:50+10`, `file.py:301-`, `file.py:5-16,960-973`, `..` alias), archive members (`bundle.zip:src/foo.ts`, combined `bundle.zip:src/foo.ts:50-100`), and multi-entry strings (`"src; tests"`) or lists.',
                        },
                        "grouped": {
                            "anyOf": [{"type": "boolean"}, {"type": "null"}],
                            "default": None,
                            "description": "Group content-mode results by file with `# path` headers and `*N|`/` N|` match/context markers. None = auto: grouped only when a line-range selector or archive member is used; True force grouped; False force legacy `path:line:text` output.",
                        },
                        "record": {
                            "default": True,
                            "description": "Persist the deduplicated matched-file list (relative paths) in the session so a follow-up read/edit pass can operate on exactly the files this grep surfaced.",
                            "type": "boolean",
                        },
                        "include": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "One glob filter for which files to search (e.g. `*.ts`, `*.{js,jsx}`). Not a list; negation is not supported. Accepts `include` or `glob`.",
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
                            "anyOf": [
                                {"minimum": 0, "type": "integer"},
                                {"type": "null"},
                            ],
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
                            "description": "Multiline regex mode. Patterns containing a newline or a `/n` regex escape automatically enable multiline mode.",
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
                        "token_kill": {
                            "default": True,
                            "description": "Deduplicate repeated output lines via rtk (token killer). Set to False to see raw, unfiltered output.",
                            "type": "boolean",
                        },
                        "fold": {
                            "default": 500,
                            "description": "Maximum number of lines in the final tool output. Longer results are head+tail folded with an omitted-count marker and a summary in `message`. 0 = unlimited (the byte cap still applies). Applied after offset/head_limit pagination.",
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["pattern"],
                    "type": "object",
                },
            },
            {
                "name": "write",
                "description": "Create or fully replace a UTF-8 text file. Overwriting an existing auto-generated file (e.g. zz_generated.*, *.pb.go, *_pb2.py, *.gen.ts, or files with a '@generated' / 'Code generated by …' header) is refused by default; pass allow_auto_generated=True to override.",
                "parameters": {
                    "properties": {
                        "file_path": {
                            "description": "Path to write, resolved by the filesystem backend. Accepts `file_path` or `path`.",
                            "type": "string",
                        },
                        "content": {
                            "description": "Full UTF-8 text content to write. Accepts `content` or `text`.",
                            "type": "string",
                        },
                        "sandbox_permissions": {
                            "anyOf": [
                                {
                                    "enum": ["workspace-write", "danger-full-access"],
                                    "type": "string",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "The wider sandbox mode this file operation needs (`workspace-write` or `danger-full-access`). Only valid as a one-shot retry of an operation the sandbox just denied; requires justification and user approval.",
                        },
                        "justification": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Required with sandbox_permissions: one sentence for the user explaining why this exact file operation needs the wider access.",
                        },
                        "mode": {
                            "default": "overwrite",
                            "description": "Write mode: overwrite or append.",
                            "enum": ["overwrite", "append"],
                            "type": "string",
                        },
                        "auto_fix_json": {
                            "default": True,
                            "description": "When True (default), attempt to repair broken JSON before writing. When False, fail with a format error if JSON is invalid.",
                            "type": "boolean",
                        },
                        "mkdir": {
                            "default": True,
                            "description": "When True (default), automatically create parent directories. When False, fail if the parent directory does not exist.",
                            "type": "boolean",
                        },
                        "show_diff": {
                            "default": False,
                            "description": "When True, include a unified diff in the tool output.",
                            "type": "boolean",
                        },
                        "allow_conflicts": {
                            "default": False,
                            "description": "When True, allow writing content that still contains conflict markers (opt-out of the conflict-marker write guard). Default False refuses to leave unresolved markers in a file.",
                            "type": "boolean",
                        },
                        "allow_auto_generated": {
                            "default": False,
                            "description": "When True, allow overwriting files that appear to be auto-generated (opt-out of the auto-generated-file guard). Default False refuses to modify generated files.",
                            "type": "boolean",
                        },
                    },
                    "required": ["file_path", "content"],
                    "type": "object",
                },
            },
            {
                "name": "edit",
                "description": "Edit an existing UTF-8 text file by replacing literal text. Files that appear to be auto-generated (e.g. zz_generated.*, *.pb.go, *_pb2.py, or files with a '@generated' / 'Code generated by …' header) are refused by default; pass allow_auto_generated=True to override.",
                "parameters": {
                    "description": "Parameters for the multi-mode edit tool.",
                    "properties": {
                        "mode": {
                            "default": "auto",
                            "description": "Edit mode. 'auto' detects the mode from the payload shape.",
                            "enum": ["auto", "replace", "sloppy"],
                            "type": "string",
                        },
                        "file_path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Path to edit. Accepts `file_path` or `path`.",
                        },
                        "edits": {
                            "anyOf": [
                                {
                                    "description": "A single literal replace edit.",
                                    "properties": {
                                        "old_string": {
                                            "description": "String to replace. Accepts `old` or `old_string`.",
                                            "type": "string",
                                        },
                                        "new_string": {
                                            "description": "Replacement text. Accepts `new` or `new_string`.",
                                            "type": "string",
                                        },
                                        "replace_all": {
                                            "default": False,
                                            "description": "Replace all occurrences. When False, only the first occurrence is replaced.",
                                            "type": "boolean",
                                        },
                                        "max_replacements": {
                                            "anyOf": [
                                                {"minimum": 1, "type": "integer"},
                                                {"type": "null"},
                                            ],
                                            "default": None,
                                            "description": "Maximum number of occurrences to replace when replace_all=True. None means unlimited.",
                                        },
                                        "match_mode": {
                                            "default": "fuzzy",
                                            "description": "'fuzzy' (default): Use fuzzy matching when exact match fails (may match similar text). 'exact': Only replace literal matches of `old`.",
                                            "enum": ["exact", "fuzzy"],
                                            "type": "string",
                                        },
                                    },
                                    "required": ["old_string", "new_string"],
                                    "type": "object",
                                },
                                {
                                    "items": {
                                        "description": "A single literal replace edit.",
                                        "properties": {
                                            "old_string": {
                                                "description": "String to replace. Accepts `old` or `old_string`.",
                                                "type": "string",
                                            },
                                            "new_string": {
                                                "description": "Replacement text. Accepts `new` or `new_string`.",
                                                "type": "string",
                                            },
                                            "replace_all": {
                                                "default": False,
                                                "description": "Replace all occurrences. When False, only the first occurrence is replaced.",
                                                "type": "boolean",
                                            },
                                            "max_replacements": {
                                                "anyOf": [
                                                    {"minimum": 1, "type": "integer"},
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                                "description": "Maximum number of occurrences to replace when replace_all=True. None means unlimited.",
                                            },
                                            "match_mode": {
                                                "default": "fuzzy",
                                                "description": "'fuzzy' (default): Use fuzzy matching when exact match fails (may match similar text). 'exact': Only replace literal matches of `old`.",
                                                "enum": ["exact", "fuzzy"],
                                                "type": "string",
                                            },
                                        },
                                        "required": ["old_string", "new_string"],
                                        "type": "object",
                                    },
                                    "type": "array",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "One or more literal replace edits. Accepts `edit` or `edits`.",
                        },
                        "old_string": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Literal text to replace. Single-edit shorthand for `edit`.",
                        },
                        "new_string": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Literal replacement text. Single-edit shorthand for `edit`.",
                        },
                        "replace_all": {
                            "default": False,
                            "description": "Replace all matches. Only used with the single-edit shorthand.",
                            "type": "boolean",
                        },
                        "input": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Input text for sloppy mode.",
                        },
                        "sandbox_permissions": {
                            "anyOf": [
                                {
                                    "enum": ["workspace-write", "danger-full-access"],
                                    "type": "string",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "The wider sandbox mode this file operation needs.",
                        },
                        "justification": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Required with sandbox_permissions: explanation for the user.",
                        },
                        "allow_conflicts": {
                            "default": False,
                            "description": "When True, allow editing files that contain conflict markers.",
                            "type": "boolean",
                        },
                        "allow_auto_generated": {
                            "default": False,
                            "description": "When True, allow editing files that appear to be auto-generated (opt-out of the auto-generated-file guard). Default False refuses to modify generated files.",
                            "type": "boolean",
                        },
                        "resolved_mode": {
                            "anyOf": [
                                {"enum": ["replace", "sloppy"], "type": "string"},
                                {"type": "null"},
                            ],
                            "default": None,
                        },
                    },
                    "type": "object",
                },
            },
            {
                "name": "fetch_url",
                "description": "Fetch a URL and extract main text.",
                "parameters": {
                    "properties": {
                        "url": {
                            "description": "URL to fetch content from.",
                            "type": "string",
                        },
                        "timeout": {
                            "default": 30.0,
                            "description": "Request timeout in seconds (1-300).",
                            "maximum": 300.0,
                            "minimum": 1.0,
                            "type": "number",
                        },
                        "method": {
                            "default": "GET",
                            "description": "HTTP method to use.",
                            "enum": ["GET", "POST"],
                            "type": "string",
                        },
                        "headers": {
                            "anyOf": [
                                {
                                    "additionalProperties": {"type": "string"},
                                    "type": "object",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Custom HTTP headers (e.g., {'Authorization': 'Bearer token'}).",
                        },
                        "body": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Request body for POST requests.",
                        },
                        "follow_redirects": {
                            "default": True,
                            "description": "Automatically follow HTTP redirects.",
                            "type": "boolean",
                        },
                        "max_redirects": {
                            "default": 5,
                            "description": "Maximum number of redirects to follow (0-20).",
                            "maximum": 20,
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["url"],
                    "type": "object",
                },
            },
            {
                "name": "web_extract",
                "description": "Extract page content from URLs as markdown/text (no LLM). Within char budget pages return whole; larger pages head+tail truncate with the full text saved to disk (read_file the omitted middle). On failure/timeout use fetch_url.",
                "parameters": {
                    "properties": {
                        "urls": {
                            "description": "List of URLs (or search-result objects with a 'url'/'href' field) to extract content from (max 5)",
                            "items": {},
                            "maxItems": 5,
                            "type": "array",
                        },
                        "char_limit": {
                            "anyOf": [
                                {"maximum": 500000, "minimum": 2000, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Per-page character budget (default 15000). Larger pages are head+tail truncated with the full text saved to disk.",
                        },
                    },
                    "required": ["urls"],
                    "type": "object",
                },
            },
            {
                "name": "context_prune",
                "description": "Prune old session content (reasoning, tool results, stale messages) to save tokens. Recent turns and tool-call pairs are always preserved. Modes: 'prune' (smart elision), 'compact' (full compaction), 'strip_reasoning' (remove old thinking content). Use dry_run=True to preview changes.",
                "parameters": {
                    "properties": {
                        "mode": {
                            "default": "prune",
                            "description": "Strategy: prune stale content, compact old turns, or strip old reasoning.",
                            "enum": ["prune", "compact", "strip_reasoning"],
                            "type": "string",
                        },
                        "target_token_count": {
                            "anyOf": [
                                {"minimum": 1000, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Target max tokens after pruning.",
                        },
                        "remove_reasoning": {
                            "default": True,
                            "description": "Remove old reasoning/thinking content.",
                            "type": "boolean",
                        },
                        "remove_tool_results": {
                            "default": True,
                            "description": "Remove old tool-result messages.",
                            "type": "boolean",
                        },
                        "keep_recent_turns": {
                            "default": 6,
                            "description": "Recent user/assistant turns to keep.",
                            "maximum": 20,
                            "minimum": 1,
                            "type": "integer",
                        },
                        "dry_run": {
                            "default": False,
                            "description": "Report what would be removed without changing the session.",
                            "type": "boolean",
                        },
                    },
                    "type": "object",
                },
            },
        ],
    },
}, {
    "method": "event",
    "type": "LLMRequest",
    "payload": {
        "kind": "loop",
        "provider": "scripted_echo",
        "model": "scripted_echo",
        "thinking_effort": None,
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "system_prompt_hash": "<SYSTEM_PROMPT_HASH>",
        "system_prompt": "<SYSTEM_PROMPT>",
        "tools_hash": "cacb7258493396316ce30be759abe02bc984d1794e3f83be31933d4cf5f861a6",
        "message_count": 1,
        "turn_step": 1,
        "attempt": 1,
        "dropped_count": None,
    },
}, {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()


def test_multiline_prompt(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: ok"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire)
        user_input = "line1\nline2"
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": user_input},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        turn_begin = _find_event(messages, "TurnBegin")
        payload = turn_begin.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("user_input") == user_input
        assert turn_begin == snapshot(
            {
                "type": "TurnBegin",
                "payload": {
                    "user_input": """\
line1
line2\
"""
                },
            }
        )
    finally:
        wire.close()


def test_content_part_prompt(tmp_path) -> None:
    config_path = write_scripted_config(
        tmp_path,
        ["text: ok"],
        capabilities=["image_in", "video_in"],
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    content_parts = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "audio_url", "audio_url": {"url": "data:audio/aac;base64,AAA"}},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAA"}},
    ]
    expected_parts = [
        {"type": "text", "text": "hello"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAA", "id": None},
        },
        {
            "type": "audio_url",
            "audio_url": {"url": "data:audio/aac;base64,AAA", "id": None},
        },
        {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,AAA", "id": None},
        },
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": content_parts},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        turn_begin = _find_event(messages, "TurnBegin")
        payload = turn_begin.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("user_input") == expected_parts
        assert turn_begin == snapshot(
            {
                "type": "TurnBegin",
                "payload": {
                    "user_input": [
                        {"type": "text", "text": "hello"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAA", "id": None},
                        },
                        {
                            "type": "audio_url",
                            "audio_url": {"url": "data:audio/aac;base64,AAA", "id": None},
                        },
                        {
                            "type": "video_url",
                            "video_url": {"url": "data:video/mp4;base64,AAA", "id": None},
                        },
                    ]
                },
            }
        )
    finally:
        wire.close()


def test_max_steps_reached(tmp_path) -> None:
    todo_line = build_todo_call("tc-1", [{"title": "x", "status": "pending"}])
    script = "\n".join(
        [
            "text: start",
            todo_line,
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--max-steps-per-turn", "1"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "max_steps_reached"
        assert normalize_response(resp) == snapshot(
            {"result": {"status": "max_steps_reached", "steps": 1}}
        )
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "start"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {
                            "name": "TodoList",
                            "arguments": '{"todos": [{"title": "x", "status": "pending"}]}',
                        },
                        "extras": None,
                    },
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "mcp_status": None,
                    },
                }, {
    "method": "event",
    "type": "ToolResult",
    "payload": {
        "tool_call_id": "tc-1",
        "return_value": {
            "is_error": False,
            "output": """\
Todo list updated (1 total: 0 done, 0 in progress, 1 pending)
- [pending] x
Next: todo_list with no todos to read the tree; parent="<title>" to add a child under an item.

<system-warning>
Tool `TodoList` was not found. Auto-corrected to `todo_list`.
</system-warning>\
""",
            "message": "Todo list updated.",
            "display": [
                {
                    "type": "todo",
                    "items": [
                        {"title": "x", "status": "pending", "notes": None, "depth": 0}
                    ],
                }
            ],
            "extras": None,
        },
    },
}, {
    "method": "event",
    "type": "LLMToolsSnapshot",
    "payload": {
        "hash": "0192290bb02ad3977a519ac42212525a6192b4615f784971f5d243f9c819ff96",
        "tools": [
            {
                "name": "subagent",
                "description": """\
Start a subagent for focused tasks; create new or resume by agent_id.

Usage
- Keep description short (3-5 words).
- subagent_type (default: coder), model to override; resume continues existing instances.
- Foreground by default; run_in_background=true only for independent tasks.
- Be explicit: code or research only.

Explore Agent — preferred for read-only codebase research. Use when you need >3 searches, module understanding, or concurrent investigations. Thoroughness: "quick" (find file), "medium" (understand module), "thorough" (architecture analysis).\
""",
                "parameters": {
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
                },
            },
            {   'name': 'todo_list',
                'description': 'Read or write the todo plan — one tool, one item shape, every operation.\n'
                               '\n'
                               'Item shape (all modes): `{title, status?, notes?, children?}` plus the edit '
                               'keys `parent`, `rename_to`, `complete`. `title` is the one short imperative '
                               'line that identifies the item and the only required key; `notes` is optional '
                               'detail (an item of only `notes` has no identity).\n'
                               '\n'
                               'Dispatch:\n'
                               '- `todos` omitted → read the current tree.\n'
                               "- mode='merge' (default) → upsert each item: an existing title patches it in "
                               'place (omitted fields keep their value), an unknown title creates it. `parent` '
                               "/ top-level `scope` pick the sub-tree ('' = root).\n"
                               "- mode='replace' → the list IS the tree (children included); needs all "
                               'existing todos done unless force=True.\n'
                               "- mode='clear' → empty the tree (all-done guard unless force=True).\n"
                               '\n'
                               'Near-duplicate titles are treated as the same task, not a new one: an incoming '
                               'title that differs from an existing one only in numbering, punctuation, case '
                               "or word order hits the conflict policy — on_conflict='error' (default) refuses "
                               'the call and names the existing title plus the exact payload to send instead; '
                               "'reuse' patches that item; 'append' really adds a second one.\n"
                               '\n'
                               'Invariants: exactly one item in_progress (auto_fix=True demotes earlier ones, '
                               'keeping the last listed); done items never move back to pending/in_progress '
                               'unless force=True; done items dropped by replace/clear are archived.',
                'parameters': {   'additionalProperties': False,
                                  'properties': {   'todos': {   'description': 'The items to write; omit to '
                                                                                'READ the tree. In the default '
                                                                                "mode='merge' this is an "
                                                                                'upsert batch — send only the '
                                                                                'items you mean to touch, each '
                                                                                'with `title` plus any of '
                                                                                'status / notes / children / '
                                                                                'rename_to / parent / '
                                                                                'complete: an existing title '
                                                                                'is patched, an unknown one is '
                                                                                "created. In mode='replace' "
                                                                                'send the COMPLETE tree '
                                                                                'instead. A single object, a '
                                                                                'bare title string or a JSON '
                                                                                'string of those forms also '
                                                                                'work. Accepts `todos` or '
                                                                                '`items`.',
                                                                 'items': {   'additionalProperties': False,
                                                                              'description': 'One todo item.\n'
                                                                                             '\n'
                                                                                             'The single item '
                                                                                             'shape for the '
                                                                                             'one todo tool: '
                                                                                             'the same object '
                                                                                             'expresses a\n'
                                                                                             'whole-tree write '
                                                                                             "(``mode='replace'``), "
                                                                                             'an upsert '
                                                                                             "(``mode='merge'``) "
                                                                                             'and a\n'
                                                                                             'targeted edit '
                                                                                             '(``parent`` / '
                                                                                             '``rename_to`` / '
                                                                                             '``complete``). '
                                                                                             'Fields that '
                                                                                             'only\n'
                                                                                             'make sense for '
                                                                                             'an edit are '
                                                                                             'ignored by a '
                                                                                             'whole-tree '
                                                                                             'write.',
                                                                              'properties': {   'title': {   'description': 'Required. '
                                                                                                                            'The '
                                                                                                                            'task '
                                                                                                                            'title: '
                                                                                                                            'one '
                                                                                                                            'short '
                                                                                                                            'imperative '
                                                                                                                            'line, '
                                                                                                                            'and '
                                                                                                                            'the '
                                                                                                                            "item's "
                                                                                                                            'identity '
                                                                                                                            '— '
                                                                                                                            "mode='merge' "
                                                                                                                            'matches '
                                                                                                                            'items '
                                                                                                                            'by '
                                                                                                                            'it '
                                                                                                                            'and '
                                                                                                                            '`parent`/`scope` '
                                                                                                                            'look '
                                                                                                                            'items '
                                                                                                                            'up '
                                                                                                                            'by '
                                                                                                                            'it, '
                                                                                                                            'so '
                                                                                                                            'it '
                                                                                                                            'must '
                                                                                                                            'always '
                                                                                                                            'be '
                                                                                                                            'sent. '
                                                                                                                            'Accepts '
                                                                                                                            '`title`, '
                                                                                                                            '`content`, '
                                                                                                                            '`task`, '
                                                                                                                            '`todo`, '
                                                                                                                            '`item` '
                                                                                                                            'or '
                                                                                                                            '`name`.',
                                                                                                             'maxLength': 65536,
                                                                                                             'minLength': 1,
                                                                                                             'type': 'string'},
                                                                                                'status': {   'default': 'pending',
                                                                                                              'description': 'One '
                                                                                                                             'of: '
                                                                                                                             'pending, '
                                                                                                                             'in_progress, '
                                                                                                                             'done '
                                                                                                                             '(or '
                                                                                                                             'completed). '
                                                                                                                             'Omit '
                                                                                                                             'to '
                                                                                                                             'keep '
                                                                                                                             'an '
                                                                                                                             'existing '
                                                                                                                             "item's "
                                                                                                                             'status; '
                                                                                                                             'created '
                                                                                                                             'items '
                                                                                                                             'default '
                                                                                                                             'to '
                                                                                                                             'pending.',
                                                                                                              'enum': [   'pending',
                                                                                                                          'in_progress',
                                                                                                                          'done'],
                                                                                                              'type': 'string'},
                                                                                                'notes': {   'anyOf': [   {   'maxLength': 65536,
                                                                                                                              'type': 'string'},
                                                                                                                          {   'type': 'null'}],
                                                                                                             'default': None,
                                                                                                             'description': 'Optional '
                                                                                                                            'supporting '
                                                                                                                            'detail '
                                                                                                                            '(evidence, '
                                                                                                                            'file '
                                                                                                                            'paths, '
                                                                                                                            'findings). '
                                                                                                                            'Not '
                                                                                                                            'the '
                                                                                                                            'title '
                                                                                                                            '— '
                                                                                                                            'an '
                                                                                                                            'item '
                                                                                                                            'with '
                                                                                                                            'only '
                                                                                                                            '`notes` '
                                                                                                                            'has '
                                                                                                                            'no '
                                                                                                                            'identity, '
                                                                                                                            'so '
                                                                                                                            'always '
                                                                                                                            'send '
                                                                                                                            '`title` '
                                                                                                                            'too. '
                                                                                                                            'Omit '
                                                                                                                            'it '
                                                                                                                            '(or '
                                                                                                                            'send '
                                                                                                                            '"") '
                                                                                                                            'to '
                                                                                                                            'keep '
                                                                                                                            'the '
                                                                                                                            'current '
                                                                                                                            'notes.'},
                                                                                                'children': {   'description': 'Sub-todos '
                                                                                                                               'of '
                                                                                                                               'this '
                                                                                                                               'item; '
                                                                                                                               'same '
                                                                                                                               'shape, '
                                                                                                                               'any '
                                                                                                                               'depth. '
                                                                                                                               'Leave '
                                                                                                                               'empty '
                                                                                                                               'for '
                                                                                                                               'a '
                                                                                                                               'leaf. '
                                                                                                                               'A '
                                                                                                                               'bare '
                                                                                                                               'title '
                                                                                                                               'string '
                                                                                                                               'is '
                                                                                                                               'accepted '
                                                                                                                               'and '
                                                                                                                               'means '
                                                                                                                               'a '
                                                                                                                               'pending '
                                                                                                                               'item.',
                                                                                                                'items': {   'additionalProperties': False,
                                                                                                                             'description': 'One '
                                                                                                                                            'todo '
                                                                                                                                            'item.\n'
                                                                                                                                            '\n'
                                                                                                                                            'The '
                                                                                                                                            'single '
                                                                                                                                            'item '
                                                                                                                                            'shape '
                                                                                                                                            'for '
                                                                                                                                            'the '
                                                                                                                                            'one '
                                                                                                                                            'todo '
                                                                                                                                            'tool: '
                                                                                                                                            'the '
                                                                                                                                            'same '
                                                                                                                                            'object '
                                                                                                                                            'expresses '
                                                                                                                                            'a\n'
                                                                                                                                            'whole-tree '
                                                                                                                                            'write '
                                                                                                                                            "(``mode='replace'``), "
                                                                                                                                            'an '
                                                                                                                                            'upsert '
                                                                                                                                            "(``mode='merge'``) "
                                                                                                                                            'and '
                                                                                                                                            'a\n'
                                                                                                                                            'targeted '
                                                                                                                                            'edit '
                                                                                                                                            '(``parent`` '
                                                                                                                                            '/ '
                                                                                                                                            '``rename_to`` '
                                                                                                                                            '/ '
                                                                                                                                            '``complete``). '
                                                                                                                                            'Fields '
                                                                                                                                            'that '
                                                                                                                                            'only\n'
                                                                                                                                            'make '
                                                                                                                                            'sense '
                                                                                                                                            'for '
                                                                                                                                            'an '
                                                                                                                                            'edit '
                                                                                                                                            'are '
                                                                                                                                            'ignored '
                                                                                                                                            'by '
                                                                                                                                            'a '
                                                                                                                                            'whole-tree '
                                                                                                                                            'write.',
                                                                                                                             'properties': {   'title': {   'description': 'Sub-todo '
                                                                                                                                                                           'title: '
                                                                                                                                                                           'one '
                                                                                                                                                                           'short '
                                                                                                                                                                           'imperative '
                                                                                                                                                                           'line '
                                                                                                                                                                           '(required).',
                                                                                                                                                            'maxLength': 65536,
                                                                                                                                                            'minLength': 1,
                                                                                                                                                            'type': 'string'},
                                                                                                                                               'status': {   'default': 'pending',
                                                                                                                                                             'description': 'One '
                                                                                                                                                                            'of: '
                                                                                                                                                                            'pending, '
                                                                                                                                                                            'in_progress, '
                                                                                                                                                                            'done '
                                                                                                                                                                            '(or '
                                                                                                                                                                            'completed). '
                                                                                                                                                                            'Omit '
                                                                                                                                                                            'to '
                                                                                                                                                                            'keep '
                                                                                                                                                                            'an '
                                                                                                                                                                            'existing '
                                                                                                                                                                            "item's "
                                                                                                                                                                            'status; '
                                                                                                                                                                            'created '
                                                                                                                                                                            'items '
                                                                                                                                                                            'default '
                                                                                                                                                                            'to '
                                                                                                                                                                            'pending.',
                                                                                                                                                             'enum': [   'pending',
                                                                                                                                                                         'in_progress',
                                                                                                                                                                         'done'],
                                                                                                                                                             'type': 'string'},
                                                                                                                                               'notes': {   'anyOf': [   {   'maxLength': 65536,
                                                                                                                                                                             'type': 'string'},
                                                                                                                                                                         {   'type': 'null'}],
                                                                                                                                                            'default': None,
                                                                                                                                                            'description': 'Optional '
                                                                                                                                                                           'detail '
                                                                                                                                                                           'for '
                                                                                                                                                                           'this '
                                                                                                                                                                           'sub-todo; '
                                                                                                                                                                           'the '
                                                                                                                                                                           'title '
                                                                                                                                                                           'is '
                                                                                                                                                                           'the '
                                                                                                                                                                           'identity.'}},
                                                                                                                             'required': [   'title'],
                                                                                                                             'type': 'object'},
                                                                                                                'type': 'array'},
                                                                                                'parent': {   'anyOf': [   {   'type': 'string'},
                                                                                                                           {   'type': 'null'}],
                                                                                                              'default': None,
                                                                                                              'description': 'Scope '
                                                                                                                             'this '
                                                                                                                             'item '
                                                                                                                             'to '
                                                                                                                             'the '
                                                                                                                             'children '
                                                                                                                             'of '
                                                                                                                             'the '
                                                                                                                             'named '
                                                                                                                             'todo '
                                                                                                                             '(both '
                                                                                                                             'its '
                                                                                                                             'lookup '
                                                                                                                             'and '
                                                                                                                             'its '
                                                                                                                             'creation). '
                                                                                                                             '"" '
                                                                                                                             '= '
                                                                                                                             'root '
                                                                                                                             'scope. '
                                                                                                                             'Overrides '
                                                                                                                             'the '
                                                                                                                             'top-level '
                                                                                                                             '`scope`.'},
                                                                                                'rename_to': {   'anyOf': [   {   'type': 'string'},
                                                                                                                              {   'type': 'null'}],
                                                                                                                 'default': None,
                                                                                                                 'description': 'Rename '
                                                                                                                                'the '
                                                                                                                                'matched '
                                                                                                                                'item '
                                                                                                                                'to '
                                                                                                                                'this '
                                                                                                                                'title '
                                                                                                                                'instead '
                                                                                                                                'of '
                                                                                                                                'editing '
                                                                                                                                'a '
                                                                                                                                'field. '
                                                                                                                                'Renaming '
                                                                                                                                'onto '
                                                                                                                                'an '
                                                                                                                                'existing '
                                                                                                                                'title '
                                                                                                                                'is '
                                                                                                                                'rejected.'},
                                                                                                'complete': {   'default': False,
                                                                                                                'description': 'Mark '
                                                                                                                               'this '
                                                                                                                               'item '
                                                                                                                               'and '
                                                                                                                               'its '
                                                                                                                               'whole '
                                                                                                                               'sub-tree '
                                                                                                                               'done '
                                                                                                                               'in '
                                                                                                                               'one '
                                                                                                                               'call. '
                                                                                                                               'Not '
                                                                                                                               'combined '
                                                                                                                               'with '
                                                                                                                               'an '
                                                                                                                               'explicit '
                                                                                                                               'pending/in_progress '
                                                                                                                               'status '
                                                                                                                               'or '
                                                                                                                               'with '
                                                                                                                               '`children`.',
                                                                                                                'type': 'boolean'},
                                                                                                'fuzzy': {   'default': True,
                                                                                                             'description': 'Per-item '
                                                                                                                            'override '
                                                                                                                            'of '
                                                                                                                            'the '
                                                                                                                            'top-level '
                                                                                                                            '`fuzzy`.',
                                                                                                             'type': 'boolean'},
                                                                                                'force': {   'default': False,
                                                                                                             'description': 'Per-item '
                                                                                                                            'override '
                                                                                                                            'of '
                                                                                                                            'the '
                                                                                                                            'top-level '
                                                                                                                            '`force`.',
                                                                                                             'type': 'boolean'}},
                                                                              'required': ['title'],
                                                                              'type': 'object'},
                                                                 'type': 'array'},
                                                    'mode': {   'default': 'merge',
                                                                'description': "'merge' (default) upserts the "
                                                                               "given items; 'replace' makes "
                                                                               'the given list the whole tree '
                                                                               'and needs every existing todo '
                                                                               'done unless force=True; '
                                                                               '"clear" empties the tree (same '
                                                                               'guard).',
                                                                'enum': ['merge', 'replace', 'clear'],
                                                                'type': 'string'},
                                                    'scope': {   'anyOf': [   {'type': 'string'},
                                                                              {'type': 'null'}],
                                                                 'default': None,
                                                                 'description': 'Restrict this call to the '
                                                                                'children of the named todo; '
                                                                                '"" means the root. An item\'s '
                                                                                'own `parent` wins.'},
                                                    'on_conflict': {   'default': 'error',
                                                                       'description': 'What to do when an '
                                                                                      'incoming title is a '
                                                                                      'near-duplicate of an '
                                                                                      'existing one (same '
                                                                                      'words, different '
                                                                                      'numbering / punctuation '
                                                                                      "/ case): 'error' "
                                                                                      '(default) refuses the '
                                                                                      'call and names the '
                                                                                      'existing title plus the '
                                                                                      'exact payload to send '
                                                                                      "instead; 'reuse' "
                                                                                      'patches that item; '
                                                                                      "'append' really adds a "
                                                                                      'second one.',
                                                                       'enum': ['error', 'reuse', 'append'],
                                                                       'type': 'string'},
                                                    'fuzzy': {   'default': True,
                                                                 'description': 'When True (default) a title '
                                                                                'that misses exactly may still '
                                                                                'match the nearest existing '
                                                                                'todo; False requires exact '
                                                                                'titles.',
                                                                 'type': 'boolean'},
                                                    'force': {   'default': False,
                                                                 'description': 'Bypass the guards: the '
                                                                                'all-done requirement of '
                                                                                'replace/clear, reopening a '
                                                                                'done item, renaming onto one, '
                                                                                'and the single-in_progress '
                                                                                'and regression checks.',
                                                                 'type': 'boolean'},
                                                    'auto_fix': {   'default': True,
                                                                    'description': 'When True (default) and '
                                                                                   'several items are '
                                                                                   'in_progress, the last '
                                                                                   'listed one is kept and '
                                                                                   'earlier ones are marked '
                                                                                   'done; False errors '
                                                                                   'instead.',
                                                                    'type': 'boolean'}},
                                  'type': 'object'}},
            {
                "name": "retrieve",
                "description": "Retrieve past conversation history, including compacted/archived turns. Use `query` to search (natural language, relevance-ranked with a recency boost) or `id` to fetch a specific turn (e.g. a `prune_<n>` reference left by context pruning).",
                "parameters": {
                    "properties": {
                        "query": {
                            "default": "",
                            "description": "Search past conversation history (BM25 with recency boost) for this natural-language query.",
                            "type": "string",
                        },
                        "id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Fetch a specific history turn by id (e.g. '0' or 'prune_0').",
                        },
                        "k": {
                            "default": 3,
                            "description": "Maximum number of history turns to return.",
                            "maximum": 10,
                            "minimum": 1,
                            "type": "integer",
                        },
                    },
                    "type": "object",
                },
            },
            {
                "name": "read",
                "description": """\
Read a UTF-8 text file and return line-numbered content.
file_path: single path or list; offset/limit: scalar or one per file. Lines over 4000 chars truncated; max 5000 lines per file; bytes scale with context (≥102400, up to 1MiB). Negative offset = tail mode. A file_path glob (e.g. ./*.md) reads up to 32 files. Prefer glob/grep to find/search, then read.

Rich formats (one per call; scalar params apply to every file in a multi-file read):
- Archives (zip/jar/war/apk/whl/cbz, tar/tgz/tbz2/txz, bare gz/bz2/xz): read data.zip lists up to 500 root entries; archive_member="src/main.py" reads one member as text. Traversal (.., absolute, backslash) rejected; binary members get an explicit notice.
- SQLite (.sqlite/.sqlite3/.db/.db3): read app.db lists tables with counts; sql_table/sql_where/sql_order/sql_limit/sql_offset paginate rows; sql_query runs raw read-only SELECT (≤1000 rows; rejects ;, comments, LIMIT/UNION/ATTACH).
- PDF screenshots: read doc.pdf with pdf_page=3 renders page 3 as PNG (requires image_in); DPI falls back 150→96→72 on over-budget.
- Document markdown: render_markdown=True converts .docx to markdown (headings, code fences, pipe tables), .md/.html to clean text; False uses the legacy extractor.
- Profiles: read *.cpuprofile / *.sample.txt returns a compact bottleneck summary (hot paths, top-20 self time, idle excluded); profile_raw=True returns raw JSON/text.
- Conflict markers: reads of files containing unresolved git conflict blocks (<<<<<<< / ======= / >>>>>>>) append a warning footer with registered conflict ids. Inspect one block with read conflict://<N> (add /ours, /theirs or /base for a single side) and get a whole-file index with read <path>:conflicts. Resolve via write({ path: "conflict://<N>", content }).\
""",
                "parameters": {
                    "properties": {
                        "file_path": {
                            "anyOf": [
                                {"type": "string"},
                                {"items": {"type": "string"}, "type": "array"},
                            ],
                            "description": "Path to read, resolved by the filesystem backend. Accepts `file_path` or `path`. May be a single file path or a list of file paths. When `glob=True`, the final path component may contain wildcards (`*`, `?`, `[...]`); recursive patterns like `src/**/*.ts` are supported, only unsafe all-wildcard patterns (e.g. `**`, `**/*`) are rejected.",
                        },
                        "offset": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 1,
                            "description": "1-based first line to return. Defaults to 1. Accepts `offset` or `line_offset`. Negative reads from end. Max abs 5000. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "limit": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 2000,
                            "description": "Maximum number of lines to return. Defaults to 2000. Accepts `limit` or `n_lines`. Max 5000. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "max_char": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 16000,
                            "description": "Maximum number of characters to return (starting from char_offset). May be a scalar applied to all files, or a list with one value per file path. Default 16K balances completeness with context efficiency.",
                        },
                        "char_offset": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 0,
                            "description": "Character offset to start returning from. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "glob": {
                            "default": False,
                            "description": "When True, treat `path` as a glob pattern (e.g., '*.py', 'src/**/*.ts'). When False (default), treat `path` as a literal file path.",
                            "type": "boolean",
                        },
                        "show_line_numbers": {
                            "default": True,
                            "description": "When True (default), prefix each line with its line number (e.g., ' 42/tcontent'). When False, return raw content without line numbers.",
                            "type": "boolean",
                        },
                        "archive_member": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Archive member path to read inside an archive. When omitted, ``read`` lists the archive root entries. Applies to zip/tar/tar.gz/tgz/tar.bz2/tar.xz and bare gz/bz2/xz files.",
                        },
                        "sql_query": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Raw read-only SQL query for SQLite files. Only SELECT statements are allowed; capped at 1000 rows. Cannot be combined with sql_table/sql_where/sql_order/sql_limit/sql_offset.",
                        },
                        "sql_table": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Table name to browse in a SQLite file.",
                        },
                        "sql_where": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "WHERE fragment for sql_table (e.g. ``id > 10``). Rejects statement terminators, comments, and LIMIT/UNION/etc.",
                        },
                        "sql_order": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "ORDER BY column for sql_table, as 'col' or 'col:asc|desc'.",
                        },
                        "sql_limit": {
                            "anyOf": [
                                {"maximum": 500, "minimum": 1, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Maximum rows for sql_table queries (default 20, max 500).",
                        },
                        "sql_offset": {
                            "anyOf": [
                                {"minimum": 0, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Offset for sql_table queries.",
                        },
                        "pdf_page": {
                            "anyOf": [
                                {"minimum": 1, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Render this PDF page as an image. Requires a model with image_in capability; otherwise returns an error.",
                        },
                        "profile_raw": {
                            "default": False,
                            "description": "When True, return the raw bytes/text of .cpuprofile or .sample.txt files. When False (default), return a compact bottleneck summary.",
                            "type": "boolean",
                        },
                        "render_markdown": {
                            "default": True,
                            "description": "When True (default), extract supported documents as markdown-flavored text and convert .md/.html files to plain text. When False, use the legacy plain-text extractor.",
                            "type": "boolean",
                        },
                    },
                    "required": ["file_path"],
                    "type": "object",
                },
            },
            {
                "name": "glob",
                "description": """\
Find files by glob. Returns file paths — never directories — including hidden/ignored (VCS metadata excluded), in modification-time order: up to 100 paths (first 100 with a note; full list saved elsewhere). Does not enumerate directory entries.
Use `read` to open matches (up to 1000 collected; omitted count reported in `message`).
Windows: `path` accepts native (`C:/Users/foo`) and POSIX-style (`/c/Users/foo`) paths. Results use backslashes — convert to forward slashes for shell commands.
""",
                "parameters": {
                    "properties": {
                        "pattern": {
                            "description": 'Glob pattern to match file paths against (e.g. `**/*.ts`, `src/**/*.test.js`). A pattern with no "/" matches the basename at any depth, so `*` and `*.ts` both search the whole tree; include a separator to anchor the depth. Unsafe recursive patterns (``**``, ``**/*``, ``**/**``, etc.) are forbidden.',
                            "type": "string",
                        },
                        "path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it. Accepts `path` or `directory`.",
                        },
                        "include_dirs": {
                            "default": False,
                            "description": "Include directories in results.",
                            "type": "boolean",
                        },
                        "respect_gitignore": {
                            "default": True,
                            "description": "When True (default), skip files matched by .gitignore rules. When False, include all files regardless of .gitignore settings.",
                            "type": "boolean",
                        },
                        "include_ignored": {
                            "default": False,
                            "description": "[Deprecated] Use respect_gitignore=False instead.",
                            "type": "boolean",
                        },
                        "verbose": {
                            "default": False,
                            "description": "When True, include file size, modification time, and type for each match.",
                            "type": "boolean",
                        },
                        "timeout": {
                            "default": 10,
                            "description": "Maximum time in seconds to wait for the search to complete.",
                            "minimum": 1,
                            "type": "integer",
                        },
                        "fold": {
                            "default": 500,
                            "description": "Maximum number of result lines in the output. Longer results are head+tail folded with an omitted-count marker and the total is reported in `message`. 0 = unlimited (the MAX_MATCHES collection cap still applies).",
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["pattern"],
                    "type": "object",
                },
            },
            {
                "name": "grep",
                "description": "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. Returns the first 250 matches inline; a capped result reports where the complete match list was saved. Use read on a matched file for surrounding context. Multiline patterns match across line boundaries.",
                "parameters": {
                    "properties": {
                        "pattern": {
                            "description": "Regular expression to search for (ripgrep syntax).",
                            "type": "string",
                        },
                        "path": {
                            "anyOf": [
                                {"type": "string"},
                                {"items": {"type": "string"}, "type": "array"},
                            ],
                            "default": ".",
                            "description": 'File or directory to search. Defaults to the session workspace; a relative path resolves against it. Also accepts embedded line-range selectors (`file.py:50-100`, `file.py:50+10`, `file.py:301-`, `file.py:5-16,960-973`, `..` alias), archive members (`bundle.zip:src/foo.ts`, combined `bundle.zip:src/foo.ts:50-100`), and multi-entry strings (`"src; tests"`) or lists.',
                        },
                        "grouped": {
                            "anyOf": [{"type": "boolean"}, {"type": "null"}],
                            "default": None,
                            "description": "Group content-mode results by file with `# path` headers and `*N|`/` N|` match/context markers. None = auto: grouped only when a line-range selector or archive member is used; True force grouped; False force legacy `path:line:text` output.",
                        },
                        "record": {
                            "default": True,
                            "description": "Persist the deduplicated matched-file list (relative paths) in the session so a follow-up read/edit pass can operate on exactly the files this grep surfaced.",
                            "type": "boolean",
                        },
                        "include": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "One glob filter for which files to search (e.g. `*.ts`, `*.{js,jsx}`). Not a list; negation is not supported. Accepts `include` or `glob`.",
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
                            "anyOf": [
                                {"minimum": 0, "type": "integer"},
                                {"type": "null"},
                            ],
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
                            "description": "Multiline regex mode. Patterns containing a newline or a `/n` regex escape automatically enable multiline mode.",
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
                        "token_kill": {
                            "default": True,
                            "description": "Deduplicate repeated output lines via rtk (token killer). Set to False to see raw, unfiltered output.",
                            "type": "boolean",
                        },
                        "fold": {
                            "default": 500,
                            "description": "Maximum number of lines in the final tool output. Longer results are head+tail folded with an omitted-count marker and a summary in `message`. 0 = unlimited (the byte cap still applies). Applied after offset/head_limit pagination.",
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["pattern"],
                    "type": "object",
                },
            },
            {
                "name": "write",
                "description": "Create or fully replace a UTF-8 text file. Overwriting an existing auto-generated file (e.g. zz_generated.*, *.pb.go, *_pb2.py, *.gen.ts, or files with a '@generated' / 'Code generated by …' header) is refused by default; pass allow_auto_generated=True to override.",
                "parameters": {
                    "properties": {
                        "file_path": {
                            "description": "Path to write, resolved by the filesystem backend. Accepts `file_path` or `path`.",
                            "type": "string",
                        },
                        "content": {
                            "description": "Full UTF-8 text content to write. Accepts `content` or `text`.",
                            "type": "string",
                        },
                        "sandbox_permissions": {
                            "anyOf": [
                                {
                                    "enum": ["workspace-write", "danger-full-access"],
                                    "type": "string",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "The wider sandbox mode this file operation needs (`workspace-write` or `danger-full-access`). Only valid as a one-shot retry of an operation the sandbox just denied; requires justification and user approval.",
                        },
                        "justification": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Required with sandbox_permissions: one sentence for the user explaining why this exact file operation needs the wider access.",
                        },
                        "mode": {
                            "default": "overwrite",
                            "description": "Write mode: overwrite or append.",
                            "enum": ["overwrite", "append"],
                            "type": "string",
                        },
                        "auto_fix_json": {
                            "default": True,
                            "description": "When True (default), attempt to repair broken JSON before writing. When False, fail with a format error if JSON is invalid.",
                            "type": "boolean",
                        },
                        "mkdir": {
                            "default": True,
                            "description": "When True (default), automatically create parent directories. When False, fail if the parent directory does not exist.",
                            "type": "boolean",
                        },
                        "show_diff": {
                            "default": False,
                            "description": "When True, include a unified diff in the tool output.",
                            "type": "boolean",
                        },
                        "allow_conflicts": {
                            "default": False,
                            "description": "When True, allow writing content that still contains conflict markers (opt-out of the conflict-marker write guard). Default False refuses to leave unresolved markers in a file.",
                            "type": "boolean",
                        },
                        "allow_auto_generated": {
                            "default": False,
                            "description": "When True, allow overwriting files that appear to be auto-generated (opt-out of the auto-generated-file guard). Default False refuses to modify generated files.",
                            "type": "boolean",
                        },
                    },
                    "required": ["file_path", "content"],
                    "type": "object",
                },
            },
            {
                "name": "edit",
                "description": "Edit an existing UTF-8 text file by replacing literal text. Files that appear to be auto-generated (e.g. zz_generated.*, *.pb.go, *_pb2.py, or files with a '@generated' / 'Code generated by …' header) are refused by default; pass allow_auto_generated=True to override.",
                "parameters": {
                    "description": "Parameters for the multi-mode edit tool.",
                    "properties": {
                        "mode": {
                            "default": "auto",
                            "description": "Edit mode. 'auto' detects the mode from the payload shape.",
                            "enum": ["auto", "replace", "sloppy"],
                            "type": "string",
                        },
                        "file_path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Path to edit. Accepts `file_path` or `path`.",
                        },
                        "edits": {
                            "anyOf": [
                                {
                                    "description": "A single literal replace edit.",
                                    "properties": {
                                        "old_string": {
                                            "description": "String to replace. Accepts `old` or `old_string`.",
                                            "type": "string",
                                        },
                                        "new_string": {
                                            "description": "Replacement text. Accepts `new` or `new_string`.",
                                            "type": "string",
                                        },
                                        "replace_all": {
                                            "default": False,
                                            "description": "Replace all occurrences. When False, only the first occurrence is replaced.",
                                            "type": "boolean",
                                        },
                                        "max_replacements": {
                                            "anyOf": [
                                                {"minimum": 1, "type": "integer"},
                                                {"type": "null"},
                                            ],
                                            "default": None,
                                            "description": "Maximum number of occurrences to replace when replace_all=True. None means unlimited.",
                                        },
                                        "match_mode": {
                                            "default": "fuzzy",
                                            "description": "'fuzzy' (default): Use fuzzy matching when exact match fails (may match similar text). 'exact': Only replace literal matches of `old`.",
                                            "enum": ["exact", "fuzzy"],
                                            "type": "string",
                                        },
                                    },
                                    "required": ["old_string", "new_string"],
                                    "type": "object",
                                },
                                {
                                    "items": {
                                        "description": "A single literal replace edit.",
                                        "properties": {
                                            "old_string": {
                                                "description": "String to replace. Accepts `old` or `old_string`.",
                                                "type": "string",
                                            },
                                            "new_string": {
                                                "description": "Replacement text. Accepts `new` or `new_string`.",
                                                "type": "string",
                                            },
                                            "replace_all": {
                                                "default": False,
                                                "description": "Replace all occurrences. When False, only the first occurrence is replaced.",
                                                "type": "boolean",
                                            },
                                            "max_replacements": {
                                                "anyOf": [
                                                    {"minimum": 1, "type": "integer"},
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                                "description": "Maximum number of occurrences to replace when replace_all=True. None means unlimited.",
                                            },
                                            "match_mode": {
                                                "default": "fuzzy",
                                                "description": "'fuzzy' (default): Use fuzzy matching when exact match fails (may match similar text). 'exact': Only replace literal matches of `old`.",
                                                "enum": ["exact", "fuzzy"],
                                                "type": "string",
                                            },
                                        },
                                        "required": ["old_string", "new_string"],
                                        "type": "object",
                                    },
                                    "type": "array",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "One or more literal replace edits. Accepts `edit` or `edits`.",
                        },
                        "old_string": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Literal text to replace. Single-edit shorthand for `edit`.",
                        },
                        "new_string": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Literal replacement text. Single-edit shorthand for `edit`.",
                        },
                        "replace_all": {
                            "default": False,
                            "description": "Replace all matches. Only used with the single-edit shorthand.",
                            "type": "boolean",
                        },
                        "input": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Input text for sloppy mode.",
                        },
                        "sandbox_permissions": {
                            "anyOf": [
                                {
                                    "enum": ["workspace-write", "danger-full-access"],
                                    "type": "string",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "The wider sandbox mode this file operation needs.",
                        },
                        "justification": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Required with sandbox_permissions: explanation for the user.",
                        },
                        "allow_conflicts": {
                            "default": False,
                            "description": "When True, allow editing files that contain conflict markers.",
                            "type": "boolean",
                        },
                        "allow_auto_generated": {
                            "default": False,
                            "description": "When True, allow editing files that appear to be auto-generated (opt-out of the auto-generated-file guard). Default False refuses to modify generated files.",
                            "type": "boolean",
                        },
                        "resolved_mode": {
                            "anyOf": [
                                {"enum": ["replace", "sloppy"], "type": "string"},
                                {"type": "null"},
                            ],
                            "default": None,
                        },
                    },
                    "type": "object",
                },
            },
            {
                "name": "fetch_url",
                "description": "Fetch a URL and extract main text.",
                "parameters": {
                    "properties": {
                        "url": {
                            "description": "URL to fetch content from.",
                            "type": "string",
                        },
                        "timeout": {
                            "default": 30.0,
                            "description": "Request timeout in seconds (1-300).",
                            "maximum": 300.0,
                            "minimum": 1.0,
                            "type": "number",
                        },
                        "method": {
                            "default": "GET",
                            "description": "HTTP method to use.",
                            "enum": ["GET", "POST"],
                            "type": "string",
                        },
                        "headers": {
                            "anyOf": [
                                {
                                    "additionalProperties": {"type": "string"},
                                    "type": "object",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Custom HTTP headers (e.g., {'Authorization': 'Bearer token'}).",
                        },
                        "body": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Request body for POST requests.",
                        },
                        "follow_redirects": {
                            "default": True,
                            "description": "Automatically follow HTTP redirects.",
                            "type": "boolean",
                        },
                        "max_redirects": {
                            "default": 5,
                            "description": "Maximum number of redirects to follow (0-20).",
                            "maximum": 20,
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["url"],
                    "type": "object",
                },
            },
            {
                "name": "web_extract",
                "description": "Extract page content from URLs as markdown/text (no LLM). Within char budget pages return whole; larger pages head+tail truncate with the full text saved to disk (read_file the omitted middle). On failure/timeout use fetch_url.",
                "parameters": {
                    "properties": {
                        "urls": {
                            "description": "List of URLs (or search-result objects with a 'url'/'href' field) to extract content from (max 5)",
                            "items": {},
                            "maxItems": 5,
                            "type": "array",
                        },
                        "char_limit": {
                            "anyOf": [
                                {"maximum": 500000, "minimum": 2000, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Per-page character budget (default 15000). Larger pages are head+tail truncated with the full text saved to disk.",
                        },
                    },
                    "required": ["urls"],
                    "type": "object",
                },
            },
            {
                "name": "context_prune",
                "description": "Prune old session content (reasoning, tool results, stale messages) to save tokens. Recent turns and tool-call pairs are always preserved. Modes: 'prune' (smart elision), 'compact' (full compaction), 'strip_reasoning' (remove old thinking content). Use dry_run=True to preview changes.",
                "parameters": {
                    "properties": {
                        "mode": {
                            "default": "prune",
                            "description": "Strategy: prune stale content, compact old turns, or strip old reasoning.",
                            "enum": ["prune", "compact", "strip_reasoning"],
                            "type": "string",
                        },
                        "target_token_count": {
                            "anyOf": [
                                {"minimum": 1000, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Target max tokens after pruning.",
                        },
                        "remove_reasoning": {
                            "default": True,
                            "description": "Remove old reasoning/thinking content.",
                            "type": "boolean",
                        },
                        "remove_tool_results": {
                            "default": True,
                            "description": "Remove old tool-result messages.",
                            "type": "boolean",
                        },
                        "keep_recent_turns": {
                            "default": 6,
                            "description": "Recent user/assistant turns to keep.",
                            "maximum": 20,
                            "minimum": 1,
                            "type": "integer",
                        },
                        "dry_run": {
                            "default": False,
                            "description": "Report what would be removed without changing the session.",
                            "type": "boolean",
                        },
                    },
                    "type": "object",
                },
            },
        ],
    },
}, {
    "method": "event",
    "type": "LLMRequest",
    "payload": {
        "kind": "loop",
        "provider": "scripted_echo",
        "model": "scripted_echo",
        "thinking_effort": None,
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "system_prompt_hash": "<SYSTEM_PROMPT_HASH>",
        "system_prompt": "<SYSTEM_PROMPT>",
        "tools_hash": "cacb7258493396316ce30be759abe02bc984d1794e3f83be31933d4cf5f861a6",
        "message_count": 1,
        "turn_step": 1,
        "attempt": 1,
        "dropped_count": None,
    },
}, {
                    "method": "event",
                    "type": "TurnEnd",
                    "payload": {},
                },
            ]
        )
    finally:
        wire.close()


def test_status_update_fields(tmp_path) -> None:
    script = "\n".join(
        [
            "id: scripted-1",
            'usage: {"input_other": 5, "output": 2}',
            "text: hello",
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        _, messages = collect_until_response(wire, "prompt-1")
        status = _find_event(messages, "StatusUpdate")
        payload = status.get("payload")
        assert isinstance(payload, dict)
        assert isinstance(payload.get("token_usage"), dict)
        assert status == snapshot(
            {
                "type": "StatusUpdate",
                "payload": {
                    "context_usage": 5e-05,
                    "context_tokens": 5,
                    "max_context_tokens": 100000,
                    "token_usage": {
                        "input_other": 5,
                        "output": 2,
                        "input_cache_read": 0,
                        "input_cache_creation": 0,
                    },
                    "message_id": "scripted-1",
                    "mcp_status": None,
                },
            }
        )
    finally:
        wire.close()


def test_concurrent_prompt_error(tmp_path) -> None:
    """A second prompt while a turn is active returns INVALID_STATE (-32000)."""
    question = {
        "question": "Proceed?",
        "header": "Concurrent",
        "options": [
            {"label": "Yes", "description": "go on"},
            {"label": "No", "description": "stop"},
        ],
        "multi_select": False,
    }
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_ask_user_tool_call("tc-1", [question]),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire, capabilities={"supports_question": True})
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run"},
            }
        )
        # The question request keeps the first turn active.
        request_msg, messages = collect_until_request(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "second"},
            }
        )
        prompt2_resp = normalize_response(read_response(wire, "prompt-2"))
        assert prompt2_resp == snapshot(
            {
                "error": {
                    "code": -32000,
                    "message": "An agent turn is already in progress",
                    "data": None,
                }
            }
        )

        wire.send_json(build_question_response(request_msg, {"Proceed?": "Yes"}))
        prompt1_resp, messages_after = collect_until_response(wire, "prompt-1")
        assert prompt1_resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages + messages_after) == snapshot(
            [{"method": "event", "type": "TurnBegin", "payload": {"user_input": "run"}}, {"method": "event", "type": "StepBegin", "payload": {"n": 1}}, {"method": "event", "type": "ContentPart", "payload": {"type": "text", "text": "step1"}}, {
    "method": "event",
    "type": "ToolCall",
    "payload": {
        "type": "function",
        "id": "tc-1",
        "function": {
            "name": "AskUserQuestion",
            "arguments": '{"questions": [{"question": "Proceed?", "header": "Concurrent", "options": [{"label": "Yes", "description": "go on"}, {"label": "No", "description": "stop"}], "multi_select": false}]}',
        },
        "extras": None,
    },
}, {
    "method": "event",
    "type": "StatusUpdate",
    "payload": {
        "context_usage": None,
        "context_tokens": None,
        "max_context_tokens": None,
        "token_usage": None,
        "message_id": None,
        "mcp_status": None,
    },
}, {
    "method": "request",
    "type": "QuestionRequest",
    "payload": {
        "id": "<uuid>",
        "tool_call_id": "tc-1",
        "questions": [
            {
                "question": "Proceed?",
                "header": "Concurrent",
                "options": [
                    {"label": "Yes", "description": "go on"},
                    {"label": "No", "description": "stop"},
                ],
                "multi_select": False,
                "body": "",
                "other_label": "",
                "other_description": "",
            }
        ],
    },
}, {
    "method": "event",
    "type": "ToolResult",
    "payload": {
        "tool_call_id": "tc-1",
        "return_value": {
            "is_error": False,
            "output": '{"answers":{"Proceed?":"Yes"}}',
            "message": "User has answered.",
            "display": [{"type": "brief", "text": "User answered"}],
            "extras": None,
        },
    },
}, {
    "method": "event",
    "type": "LLMToolsSnapshot",
    "payload": {
        "hash": "82a777df8fd492d239b44a3d29c62bae1594e166a46dfc86aea623acc7eab598",
        "tools": [
            {
                "name": "subagent",
                "description": """\
Start a subagent for focused tasks; create new or resume by agent_id.

Usage
- Keep description short (3-5 words).
- subagent_type (default: coder), model to override; resume continues existing instances.
- Foreground by default; run_in_background=true only for independent tasks.
- Be explicit: code or research only.

Explore Agent — preferred for read-only codebase research. Use when you need >3 searches, module understanding, or concurrent investigations. Thoroughness: "quick" (find file), "medium" (understand module), "thorough" (architecture analysis).\
""",
                "parameters": {
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
                },
            },
            {
                "name": "AskUserQuestion",
                "description": 'Ask users structured questions for preferences, ambiguity resolution, or approach decisions. Don\'t use when answer is inferable or trivial—overuse disrupts workflow. Use multi_select for multiple answers; provide 2-4 concise options (1-5 words) per question, recommended first with "(Recommended)". Ask 1-4 related questions at a time. Never add an "Other" option—users always have one.',
                "parameters": {
                    "properties": {
                        "questions": {
                            "description": "Questions to ask (1-4).",
                            "items": {
                                "properties": {
                                    "question": {
                                        "description": "Actionable question ending with '?'.",
                                        "type": "string",
                                    },
                                    "header": {
                                        "default": "",
                                        "description": "Category tag (max 12 chars).",
                                        "type": "string",
                                    },
                                    "options": {
                                        "description": "2-4 options. 'Other' is auto-added.",
                                        "items": {
                                            "properties": {
                                                "label": {
                                                    "description": "Display text (1-5 words).",
                                                    "type": "string",
                                                },
                                                "description": {
                                                    "default": "",
                                                    "description": "Option meaning or trade-offs.",
                                                    "type": "string",
                                                },
                                            },
                                            "required": ["label"],
                                            "type": "object",
                                        },
                                        "maxItems": 4,
                                        "minItems": 2,
                                        "type": "array",
                                    },
                                    "multi_select": {
                                        "default": False,
                                        "description": "Allow multiple selections.",
                                        "type": "boolean",
                                    },
                                },
                                "required": ["question", "options"],
                                "type": "object",
                            },
                            "maxItems": 4,
                            "minItems": 1,
                            "type": "array",
                        }
                    },
                    "required": ["questions"],
                    "type": "object",
                },
            },
            {   'name': 'todo_list',
                'description': 'Read or write the todo plan — one tool, one item shape, every operation.\n'
                               '\n'
                               'Item shape (all modes): `{title, status?, notes?, children?}` plus the edit '
                               'keys `parent`, `rename_to`, `complete`. `title` is the one short imperative '
                               'line that identifies the item and the only required key; `notes` is optional '
                               'detail (an item of only `notes` has no identity).\n'
                               '\n'
                               'Dispatch:\n'
                               '- `todos` omitted → read the current tree.\n'
                               "- mode='merge' (default) → upsert each item: an existing title patches it in "
                               'place (omitted fields keep their value), an unknown title creates it. `parent` '
                               "/ top-level `scope` pick the sub-tree ('' = root).\n"
                               "- mode='replace' → the list IS the tree (children included); needs all "
                               'existing todos done unless force=True.\n'
                               "- mode='clear' → empty the tree (all-done guard unless force=True).\n"
                               '\n'
                               'Near-duplicate titles are treated as the same task, not a new one: an incoming '
                               'title that differs from an existing one only in numbering, punctuation, case '
                               "or word order hits the conflict policy — on_conflict='error' (default) refuses "
                               'the call and names the existing title plus the exact payload to send instead; '
                               "'reuse' patches that item; 'append' really adds a second one.\n"
                               '\n'
                               'Invariants: exactly one item in_progress (auto_fix=True demotes earlier ones, '
                               'keeping the last listed); done items never move back to pending/in_progress '
                               'unless force=True; done items dropped by replace/clear are archived.',
                'parameters': {   'additionalProperties': False,
                                  'properties': {   'todos': {   'description': 'The items to write; omit to '
                                                                                'READ the tree. In the default '
                                                                                "mode='merge' this is an "
                                                                                'upsert batch — send only the '
                                                                                'items you mean to touch, each '
                                                                                'with `title` plus any of '
                                                                                'status / notes / children / '
                                                                                'rename_to / parent / '
                                                                                'complete: an existing title '
                                                                                'is patched, an unknown one is '
                                                                                "created. In mode='replace' "
                                                                                'send the COMPLETE tree '
                                                                                'instead. A single object, a '
                                                                                'bare title string or a JSON '
                                                                                'string of those forms also '
                                                                                'work. Accepts `todos` or '
                                                                                '`items`.',
                                                                 'items': {   'additionalProperties': False,
                                                                              'description': 'One todo item.\n'
                                                                                             '\n'
                                                                                             'The single item '
                                                                                             'shape for the '
                                                                                             'one todo tool: '
                                                                                             'the same object '
                                                                                             'expresses a\n'
                                                                                             'whole-tree write '
                                                                                             "(``mode='replace'``), "
                                                                                             'an upsert '
                                                                                             "(``mode='merge'``) "
                                                                                             'and a\n'
                                                                                             'targeted edit '
                                                                                             '(``parent`` / '
                                                                                             '``rename_to`` / '
                                                                                             '``complete``). '
                                                                                             'Fields that '
                                                                                             'only\n'
                                                                                             'make sense for '
                                                                                             'an edit are '
                                                                                             'ignored by a '
                                                                                             'whole-tree '
                                                                                             'write.',
                                                                              'properties': {   'title': {   'description': 'Required. '
                                                                                                                            'The '
                                                                                                                            'task '
                                                                                                                            'title: '
                                                                                                                            'one '
                                                                                                                            'short '
                                                                                                                            'imperative '
                                                                                                                            'line, '
                                                                                                                            'and '
                                                                                                                            'the '
                                                                                                                            "item's "
                                                                                                                            'identity '
                                                                                                                            '— '
                                                                                                                            "mode='merge' "
                                                                                                                            'matches '
                                                                                                                            'items '
                                                                                                                            'by '
                                                                                                                            'it '
                                                                                                                            'and '
                                                                                                                            '`parent`/`scope` '
                                                                                                                            'look '
                                                                                                                            'items '
                                                                                                                            'up '
                                                                                                                            'by '
                                                                                                                            'it, '
                                                                                                                            'so '
                                                                                                                            'it '
                                                                                                                            'must '
                                                                                                                            'always '
                                                                                                                            'be '
                                                                                                                            'sent. '
                                                                                                                            'Accepts '
                                                                                                                            '`title`, '
                                                                                                                            '`content`, '
                                                                                                                            '`task`, '
                                                                                                                            '`todo`, '
                                                                                                                            '`item` '
                                                                                                                            'or '
                                                                                                                            '`name`.',
                                                                                                             'maxLength': 65536,
                                                                                                             'minLength': 1,
                                                                                                             'type': 'string'},
                                                                                                'status': {   'default': 'pending',
                                                                                                              'description': 'One '
                                                                                                                             'of: '
                                                                                                                             'pending, '
                                                                                                                             'in_progress, '
                                                                                                                             'done '
                                                                                                                             '(or '
                                                                                                                             'completed). '
                                                                                                                             'Omit '
                                                                                                                             'to '
                                                                                                                             'keep '
                                                                                                                             'an '
                                                                                                                             'existing '
                                                                                                                             "item's "
                                                                                                                             'status; '
                                                                                                                             'created '
                                                                                                                             'items '
                                                                                                                             'default '
                                                                                                                             'to '
                                                                                                                             'pending.',
                                                                                                              'enum': [   'pending',
                                                                                                                          'in_progress',
                                                                                                                          'done'],
                                                                                                              'type': 'string'},
                                                                                                'notes': {   'anyOf': [   {   'maxLength': 65536,
                                                                                                                              'type': 'string'},
                                                                                                                          {   'type': 'null'}],
                                                                                                             'default': None,
                                                                                                             'description': 'Optional '
                                                                                                                            'supporting '
                                                                                                                            'detail '
                                                                                                                            '(evidence, '
                                                                                                                            'file '
                                                                                                                            'paths, '
                                                                                                                            'findings). '
                                                                                                                            'Not '
                                                                                                                            'the '
                                                                                                                            'title '
                                                                                                                            '— '
                                                                                                                            'an '
                                                                                                                            'item '
                                                                                                                            'with '
                                                                                                                            'only '
                                                                                                                            '`notes` '
                                                                                                                            'has '
                                                                                                                            'no '
                                                                                                                            'identity, '
                                                                                                                            'so '
                                                                                                                            'always '
                                                                                                                            'send '
                                                                                                                            '`title` '
                                                                                                                            'too. '
                                                                                                                            'Omit '
                                                                                                                            'it '
                                                                                                                            '(or '
                                                                                                                            'send '
                                                                                                                            '"") '
                                                                                                                            'to '
                                                                                                                            'keep '
                                                                                                                            'the '
                                                                                                                            'current '
                                                                                                                            'notes.'},
                                                                                                'children': {   'description': 'Sub-todos '
                                                                                                                               'of '
                                                                                                                               'this '
                                                                                                                               'item; '
                                                                                                                               'same '
                                                                                                                               'shape, '
                                                                                                                               'any '
                                                                                                                               'depth. '
                                                                                                                               'Leave '
                                                                                                                               'empty '
                                                                                                                               'for '
                                                                                                                               'a '
                                                                                                                               'leaf. '
                                                                                                                               'A '
                                                                                                                               'bare '
                                                                                                                               'title '
                                                                                                                               'string '
                                                                                                                               'is '
                                                                                                                               'accepted '
                                                                                                                               'and '
                                                                                                                               'means '
                                                                                                                               'a '
                                                                                                                               'pending '
                                                                                                                               'item.',
                                                                                                                'items': {   'additionalProperties': False,
                                                                                                                             'description': 'One '
                                                                                                                                            'todo '
                                                                                                                                            'item.\n'
                                                                                                                                            '\n'
                                                                                                                                            'The '
                                                                                                                                            'single '
                                                                                                                                            'item '
                                                                                                                                            'shape '
                                                                                                                                            'for '
                                                                                                                                            'the '
                                                                                                                                            'one '
                                                                                                                                            'todo '
                                                                                                                                            'tool: '
                                                                                                                                            'the '
                                                                                                                                            'same '
                                                                                                                                            'object '
                                                                                                                                            'expresses '
                                                                                                                                            'a\n'
                                                                                                                                            'whole-tree '
                                                                                                                                            'write '
                                                                                                                                            "(``mode='replace'``), "
                                                                                                                                            'an '
                                                                                                                                            'upsert '
                                                                                                                                            "(``mode='merge'``) "
                                                                                                                                            'and '
                                                                                                                                            'a\n'
                                                                                                                                            'targeted '
                                                                                                                                            'edit '
                                                                                                                                            '(``parent`` '
                                                                                                                                            '/ '
                                                                                                                                            '``rename_to`` '
                                                                                                                                            '/ '
                                                                                                                                            '``complete``). '
                                                                                                                                            'Fields '
                                                                                                                                            'that '
                                                                                                                                            'only\n'
                                                                                                                                            'make '
                                                                                                                                            'sense '
                                                                                                                                            'for '
                                                                                                                                            'an '
                                                                                                                                            'edit '
                                                                                                                                            'are '
                                                                                                                                            'ignored '
                                                                                                                                            'by '
                                                                                                                                            'a '
                                                                                                                                            'whole-tree '
                                                                                                                                            'write.',
                                                                                                                             'properties': {   'title': {   'description': 'Sub-todo '
                                                                                                                                                                           'title: '
                                                                                                                                                                           'one '
                                                                                                                                                                           'short '
                                                                                                                                                                           'imperative '
                                                                                                                                                                           'line '
                                                                                                                                                                           '(required).',
                                                                                                                                                            'maxLength': 65536,
                                                                                                                                                            'minLength': 1,
                                                                                                                                                            'type': 'string'},
                                                                                                                                               'status': {   'default': 'pending',
                                                                                                                                                             'description': 'One '
                                                                                                                                                                            'of: '
                                                                                                                                                                            'pending, '
                                                                                                                                                                            'in_progress, '
                                                                                                                                                                            'done '
                                                                                                                                                                            '(or '
                                                                                                                                                                            'completed). '
                                                                                                                                                                            'Omit '
                                                                                                                                                                            'to '
                                                                                                                                                                            'keep '
                                                                                                                                                                            'an '
                                                                                                                                                                            'existing '
                                                                                                                                                                            "item's "
                                                                                                                                                                            'status; '
                                                                                                                                                                            'created '
                                                                                                                                                                            'items '
                                                                                                                                                                            'default '
                                                                                                                                                                            'to '
                                                                                                                                                                            'pending.',
                                                                                                                                                             'enum': [   'pending',
                                                                                                                                                                         'in_progress',
                                                                                                                                                                         'done'],
                                                                                                                                                             'type': 'string'},
                                                                                                                                               'notes': {   'anyOf': [   {   'maxLength': 65536,
                                                                                                                                                                             'type': 'string'},
                                                                                                                                                                         {   'type': 'null'}],
                                                                                                                                                            'default': None,
                                                                                                                                                            'description': 'Optional '
                                                                                                                                                                           'detail '
                                                                                                                                                                           'for '
                                                                                                                                                                           'this '
                                                                                                                                                                           'sub-todo; '
                                                                                                                                                                           'the '
                                                                                                                                                                           'title '
                                                                                                                                                                           'is '
                                                                                                                                                                           'the '
                                                                                                                                                                           'identity.'}},
                                                                                                                             'required': [   'title'],
                                                                                                                             'type': 'object'},
                                                                                                                'type': 'array'},
                                                                                                'parent': {   'anyOf': [   {   'type': 'string'},
                                                                                                                           {   'type': 'null'}],
                                                                                                              'default': None,
                                                                                                              'description': 'Scope '
                                                                                                                             'this '
                                                                                                                             'item '
                                                                                                                             'to '
                                                                                                                             'the '
                                                                                                                             'children '
                                                                                                                             'of '
                                                                                                                             'the '
                                                                                                                             'named '
                                                                                                                             'todo '
                                                                                                                             '(both '
                                                                                                                             'its '
                                                                                                                             'lookup '
                                                                                                                             'and '
                                                                                                                             'its '
                                                                                                                             'creation). '
                                                                                                                             '"" '
                                                                                                                             '= '
                                                                                                                             'root '
                                                                                                                             'scope. '
                                                                                                                             'Overrides '
                                                                                                                             'the '
                                                                                                                             'top-level '
                                                                                                                             '`scope`.'},
                                                                                                'rename_to': {   'anyOf': [   {   'type': 'string'},
                                                                                                                              {   'type': 'null'}],
                                                                                                                 'default': None,
                                                                                                                 'description': 'Rename '
                                                                                                                                'the '
                                                                                                                                'matched '
                                                                                                                                'item '
                                                                                                                                'to '
                                                                                                                                'this '
                                                                                                                                'title '
                                                                                                                                'instead '
                                                                                                                                'of '
                                                                                                                                'editing '
                                                                                                                                'a '
                                                                                                                                'field. '
                                                                                                                                'Renaming '
                                                                                                                                'onto '
                                                                                                                                'an '
                                                                                                                                'existing '
                                                                                                                                'title '
                                                                                                                                'is '
                                                                                                                                'rejected.'},
                                                                                                'complete': {   'default': False,
                                                                                                                'description': 'Mark '
                                                                                                                               'this '
                                                                                                                               'item '
                                                                                                                               'and '
                                                                                                                               'its '
                                                                                                                               'whole '
                                                                                                                               'sub-tree '
                                                                                                                               'done '
                                                                                                                               'in '
                                                                                                                               'one '
                                                                                                                               'call. '
                                                                                                                               'Not '
                                                                                                                               'combined '
                                                                                                                               'with '
                                                                                                                               'an '
                                                                                                                               'explicit '
                                                                                                                               'pending/in_progress '
                                                                                                                               'status '
                                                                                                                               'or '
                                                                                                                               'with '
                                                                                                                               '`children`.',
                                                                                                                'type': 'boolean'},
                                                                                                'fuzzy': {   'default': True,
                                                                                                             'description': 'Per-item '
                                                                                                                            'override '
                                                                                                                            'of '
                                                                                                                            'the '
                                                                                                                            'top-level '
                                                                                                                            '`fuzzy`.',
                                                                                                             'type': 'boolean'},
                                                                                                'force': {   'default': False,
                                                                                                             'description': 'Per-item '
                                                                                                                            'override '
                                                                                                                            'of '
                                                                                                                            'the '
                                                                                                                            'top-level '
                                                                                                                            '`force`.',
                                                                                                             'type': 'boolean'}},
                                                                              'required': ['title'],
                                                                              'type': 'object'},
                                                                 'type': 'array'},
                                                    'mode': {   'default': 'merge',
                                                                'description': "'merge' (default) upserts the "
                                                                               "given items; 'replace' makes "
                                                                               'the given list the whole tree '
                                                                               'and needs every existing todo '
                                                                               'done unless force=True; '
                                                                               '"clear" empties the tree (same '
                                                                               'guard).',
                                                                'enum': ['merge', 'replace', 'clear'],
                                                                'type': 'string'},
                                                    'scope': {   'anyOf': [   {'type': 'string'},
                                                                              {'type': 'null'}],
                                                                 'default': None,
                                                                 'description': 'Restrict this call to the '
                                                                                'children of the named todo; '
                                                                                '"" means the root. An item\'s '
                                                                                'own `parent` wins.'},
                                                    'on_conflict': {   'default': 'error',
                                                                       'description': 'What to do when an '
                                                                                      'incoming title is a '
                                                                                      'near-duplicate of an '
                                                                                      'existing one (same '
                                                                                      'words, different '
                                                                                      'numbering / punctuation '
                                                                                      "/ case): 'error' "
                                                                                      '(default) refuses the '
                                                                                      'call and names the '
                                                                                      'existing title plus the '
                                                                                      'exact payload to send '
                                                                                      "instead; 'reuse' "
                                                                                      'patches that item; '
                                                                                      "'append' really adds a "
                                                                                      'second one.',
                                                                       'enum': ['error', 'reuse', 'append'],
                                                                       'type': 'string'},
                                                    'fuzzy': {   'default': True,
                                                                 'description': 'When True (default) a title '
                                                                                'that misses exactly may still '
                                                                                'match the nearest existing '
                                                                                'todo; False requires exact '
                                                                                'titles.',
                                                                 'type': 'boolean'},
                                                    'force': {   'default': False,
                                                                 'description': 'Bypass the guards: the '
                                                                                'all-done requirement of '
                                                                                'replace/clear, reopening a '
                                                                                'done item, renaming onto one, '
                                                                                'and the single-in_progress '
                                                                                'and regression checks.',
                                                                 'type': 'boolean'},
                                                    'auto_fix': {   'default': True,
                                                                    'description': 'When True (default) and '
                                                                                   'several items are '
                                                                                   'in_progress, the last '
                                                                                   'listed one is kept and '
                                                                                   'earlier ones are marked '
                                                                                   'done; False errors '
                                                                                   'instead.',
                                                                    'type': 'boolean'}},
                                  'type': 'object'}},
            {
                "name": "retrieve",
                "description": "Retrieve past conversation history, including compacted/archived turns. Use `query` to search (natural language, relevance-ranked with a recency boost) or `id` to fetch a specific turn (e.g. a `prune_<n>` reference left by context pruning).",
                "parameters": {
                    "properties": {
                        "query": {
                            "default": "",
                            "description": "Search past conversation history (BM25 with recency boost) for this natural-language query.",
                            "type": "string",
                        },
                        "id": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Fetch a specific history turn by id (e.g. '0' or 'prune_0').",
                        },
                        "k": {
                            "default": 3,
                            "description": "Maximum number of history turns to return.",
                            "maximum": 10,
                            "minimum": 1,
                            "type": "integer",
                        },
                    },
                    "type": "object",
                },
            },
            {
                "name": "read",
                "description": """\
Read a UTF-8 text file and return line-numbered content.
file_path: single path or list; offset/limit: scalar or one per file. Lines over 4000 chars truncated; max 5000 lines per file; bytes scale with context (≥102400, up to 1MiB). Negative offset = tail mode. A file_path glob (e.g. ./*.md) reads up to 32 files. Prefer glob/grep to find/search, then read.

Rich formats (one per call; scalar params apply to every file in a multi-file read):
- Archives (zip/jar/war/apk/whl/cbz, tar/tgz/tbz2/txz, bare gz/bz2/xz): read data.zip lists up to 500 root entries; archive_member="src/main.py" reads one member as text. Traversal (.., absolute, backslash) rejected; binary members get an explicit notice.
- SQLite (.sqlite/.sqlite3/.db/.db3): read app.db lists tables with counts; sql_table/sql_where/sql_order/sql_limit/sql_offset paginate rows; sql_query runs raw read-only SELECT (≤1000 rows; rejects ;, comments, LIMIT/UNION/ATTACH).
- PDF screenshots: read doc.pdf with pdf_page=3 renders page 3 as PNG (requires image_in); DPI falls back 150→96→72 on over-budget.
- Document markdown: render_markdown=True converts .docx to markdown (headings, code fences, pipe tables), .md/.html to clean text; False uses the legacy extractor.
- Profiles: read *.cpuprofile / *.sample.txt returns a compact bottleneck summary (hot paths, top-20 self time, idle excluded); profile_raw=True returns raw JSON/text.
- Conflict markers: reads of files containing unresolved git conflict blocks (<<<<<<< / ======= / >>>>>>>) append a warning footer with registered conflict ids. Inspect one block with read conflict://<N> (add /ours, /theirs or /base for a single side) and get a whole-file index with read <path>:conflicts. Resolve via write({ path: "conflict://<N>", content }).\
""",
                "parameters": {
                    "properties": {
                        "file_path": {
                            "anyOf": [
                                {"type": "string"},
                                {"items": {"type": "string"}, "type": "array"},
                            ],
                            "description": "Path to read, resolved by the filesystem backend. Accepts `file_path` or `path`. May be a single file path or a list of file paths. When `glob=True`, the final path component may contain wildcards (`*`, `?`, `[...]`); recursive patterns like `src/**/*.ts` are supported, only unsafe all-wildcard patterns (e.g. `**`, `**/*`) are rejected.",
                        },
                        "offset": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 1,
                            "description": "1-based first line to return. Defaults to 1. Accepts `offset` or `line_offset`. Negative reads from end. Max abs 5000. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "limit": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 2000,
                            "description": "Maximum number of lines to return. Defaults to 2000. Accepts `limit` or `n_lines`. Max 5000. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "max_char": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 16000,
                            "description": "Maximum number of characters to return (starting from char_offset). May be a scalar applied to all files, or a list with one value per file path. Default 16K balances completeness with context efficiency.",
                        },
                        "char_offset": {
                            "anyOf": [
                                {"type": "integer"},
                                {"items": {"type": "integer"}, "type": "array"},
                            ],
                            "default": 0,
                            "description": "Character offset to start returning from. May be a scalar applied to all files, or a list with one value per file path.",
                        },
                        "glob": {
                            "default": False,
                            "description": "When True, treat `path` as a glob pattern (e.g., '*.py', 'src/**/*.ts'). When False (default), treat `path` as a literal file path.",
                            "type": "boolean",
                        },
                        "show_line_numbers": {
                            "default": True,
                            "description": "When True (default), prefix each line with its line number (e.g., ' 42/tcontent'). When False, return raw content without line numbers.",
                            "type": "boolean",
                        },
                        "archive_member": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Archive member path to read inside an archive. When omitted, ``read`` lists the archive root entries. Applies to zip/tar/tar.gz/tgz/tar.bz2/tar.xz and bare gz/bz2/xz files.",
                        },
                        "sql_query": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Raw read-only SQL query for SQLite files. Only SELECT statements are allowed; capped at 1000 rows. Cannot be combined with sql_table/sql_where/sql_order/sql_limit/sql_offset.",
                        },
                        "sql_table": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Table name to browse in a SQLite file.",
                        },
                        "sql_where": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "WHERE fragment for sql_table (e.g. ``id > 10``). Rejects statement terminators, comments, and LIMIT/UNION/etc.",
                        },
                        "sql_order": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "ORDER BY column for sql_table, as 'col' or 'col:asc|desc'.",
                        },
                        "sql_limit": {
                            "anyOf": [
                                {"maximum": 500, "minimum": 1, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Maximum rows for sql_table queries (default 20, max 500).",
                        },
                        "sql_offset": {
                            "anyOf": [
                                {"minimum": 0, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Offset for sql_table queries.",
                        },
                        "pdf_page": {
                            "anyOf": [
                                {"minimum": 1, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Render this PDF page as an image. Requires a model with image_in capability; otherwise returns an error.",
                        },
                        "profile_raw": {
                            "default": False,
                            "description": "When True, return the raw bytes/text of .cpuprofile or .sample.txt files. When False (default), return a compact bottleneck summary.",
                            "type": "boolean",
                        },
                        "render_markdown": {
                            "default": True,
                            "description": "When True (default), extract supported documents as markdown-flavored text and convert .md/.html files to plain text. When False, use the legacy plain-text extractor.",
                            "type": "boolean",
                        },
                    },
                    "required": ["file_path"],
                    "type": "object",
                },
            },
            {
                "name": "glob",
                "description": """\
Find files by glob. Returns file paths — never directories — including hidden/ignored (VCS metadata excluded), in modification-time order: up to 100 paths (first 100 with a note; full list saved elsewhere). Does not enumerate directory entries.
Use `read` to open matches (up to 1000 collected; omitted count reported in `message`).
Windows: `path` accepts native (`C:/Users/foo`) and POSIX-style (`/c/Users/foo`) paths. Results use backslashes — convert to forward slashes for shell commands.
""",
                "parameters": {
                    "properties": {
                        "pattern": {
                            "description": 'Glob pattern to match file paths against (e.g. `**/*.ts`, `src/**/*.test.js`). A pattern with no "/" matches the basename at any depth, so `*` and `*.ts` both search the whole tree; include a separator to anchor the depth. Unsafe recursive patterns (``**``, ``**/*``, ``**/**``, etc.) are forbidden.',
                            "type": "string",
                        },
                        "path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it. Accepts `path` or `directory`.",
                        },
                        "include_dirs": {
                            "default": False,
                            "description": "Include directories in results.",
                            "type": "boolean",
                        },
                        "respect_gitignore": {
                            "default": True,
                            "description": "When True (default), skip files matched by .gitignore rules. When False, include all files regardless of .gitignore settings.",
                            "type": "boolean",
                        },
                        "include_ignored": {
                            "default": False,
                            "description": "[Deprecated] Use respect_gitignore=False instead.",
                            "type": "boolean",
                        },
                        "verbose": {
                            "default": False,
                            "description": "When True, include file size, modification time, and type for each match.",
                            "type": "boolean",
                        },
                        "timeout": {
                            "default": 10,
                            "description": "Maximum time in seconds to wait for the search to complete.",
                            "minimum": 1,
                            "type": "integer",
                        },
                        "fold": {
                            "default": 500,
                            "description": "Maximum number of result lines in the output. Longer results are head+tail folded with an omitted-count marker and the total is reported in `message`. 0 = unlimited (the MAX_MATCHES collection cap still applies).",
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["pattern"],
                    "type": "object",
                },
            },
            {
                "name": "grep",
                "description": "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. Returns the first 250 matches inline; a capped result reports where the complete match list was saved. Use read on a matched file for surrounding context. Multiline patterns match across line boundaries.",
                "parameters": {
                    "properties": {
                        "pattern": {
                            "description": "Regular expression to search for (ripgrep syntax).",
                            "type": "string",
                        },
                        "path": {
                            "anyOf": [
                                {"type": "string"},
                                {"items": {"type": "string"}, "type": "array"},
                            ],
                            "default": ".",
                            "description": 'File or directory to search. Defaults to the session workspace; a relative path resolves against it. Also accepts embedded line-range selectors (`file.py:50-100`, `file.py:50+10`, `file.py:301-`, `file.py:5-16,960-973`, `..` alias), archive members (`bundle.zip:src/foo.ts`, combined `bundle.zip:src/foo.ts:50-100`), and multi-entry strings (`"src; tests"`) or lists.',
                        },
                        "grouped": {
                            "anyOf": [{"type": "boolean"}, {"type": "null"}],
                            "default": None,
                            "description": "Group content-mode results by file with `# path` headers and `*N|`/` N|` match/context markers. None = auto: grouped only when a line-range selector or archive member is used; True force grouped; False force legacy `path:line:text` output.",
                        },
                        "record": {
                            "default": True,
                            "description": "Persist the deduplicated matched-file list (relative paths) in the session so a follow-up read/edit pass can operate on exactly the files this grep surfaced.",
                            "type": "boolean",
                        },
                        "include": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "One glob filter for which files to search (e.g. `*.ts`, `*.{js,jsx}`). Not a list; negation is not supported. Accepts `include` or `glob`.",
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
                            "anyOf": [
                                {"minimum": 0, "type": "integer"},
                                {"type": "null"},
                            ],
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
                            "description": "Multiline regex mode. Patterns containing a newline or a `/n` regex escape automatically enable multiline mode.",
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
                        "token_kill": {
                            "default": True,
                            "description": "Deduplicate repeated output lines via rtk (token killer). Set to False to see raw, unfiltered output.",
                            "type": "boolean",
                        },
                        "fold": {
                            "default": 500,
                            "description": "Maximum number of lines in the final tool output. Longer results are head+tail folded with an omitted-count marker and a summary in `message`. 0 = unlimited (the byte cap still applies). Applied after offset/head_limit pagination.",
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["pattern"],
                    "type": "object",
                },
            },
            {
                "name": "write",
                "description": "Create or fully replace a UTF-8 text file. Overwriting an existing auto-generated file (e.g. zz_generated.*, *.pb.go, *_pb2.py, *.gen.ts, or files with a '@generated' / 'Code generated by …' header) is refused by default; pass allow_auto_generated=True to override.",
                "parameters": {
                    "properties": {
                        "file_path": {
                            "description": "Path to write, resolved by the filesystem backend. Accepts `file_path` or `path`.",
                            "type": "string",
                        },
                        "content": {
                            "description": "Full UTF-8 text content to write. Accepts `content` or `text`.",
                            "type": "string",
                        },
                        "sandbox_permissions": {
                            "anyOf": [
                                {
                                    "enum": ["workspace-write", "danger-full-access"],
                                    "type": "string",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "The wider sandbox mode this file operation needs (`workspace-write` or `danger-full-access`). Only valid as a one-shot retry of an operation the sandbox just denied; requires justification and user approval.",
                        },
                        "justification": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Required with sandbox_permissions: one sentence for the user explaining why this exact file operation needs the wider access.",
                        },
                        "mode": {
                            "default": "overwrite",
                            "description": "Write mode: overwrite or append.",
                            "enum": ["overwrite", "append"],
                            "type": "string",
                        },
                        "auto_fix_json": {
                            "default": True,
                            "description": "When True (default), attempt to repair broken JSON before writing. When False, fail with a format error if JSON is invalid.",
                            "type": "boolean",
                        },
                        "mkdir": {
                            "default": True,
                            "description": "When True (default), automatically create parent directories. When False, fail if the parent directory does not exist.",
                            "type": "boolean",
                        },
                        "show_diff": {
                            "default": False,
                            "description": "When True, include a unified diff in the tool output.",
                            "type": "boolean",
                        },
                        "allow_conflicts": {
                            "default": False,
                            "description": "When True, allow writing content that still contains conflict markers (opt-out of the conflict-marker write guard). Default False refuses to leave unresolved markers in a file.",
                            "type": "boolean",
                        },
                        "allow_auto_generated": {
                            "default": False,
                            "description": "When True, allow overwriting files that appear to be auto-generated (opt-out of the auto-generated-file guard). Default False refuses to modify generated files.",
                            "type": "boolean",
                        },
                    },
                    "required": ["file_path", "content"],
                    "type": "object",
                },
            },
            {
                "name": "edit",
                "description": "Edit an existing UTF-8 text file by replacing literal text. Files that appear to be auto-generated (e.g. zz_generated.*, *.pb.go, *_pb2.py, or files with a '@generated' / 'Code generated by …' header) are refused by default; pass allow_auto_generated=True to override.",
                "parameters": {
                    "description": "Parameters for the multi-mode edit tool.",
                    "properties": {
                        "mode": {
                            "default": "auto",
                            "description": "Edit mode. 'auto' detects the mode from the payload shape.",
                            "enum": ["auto", "replace", "sloppy"],
                            "type": "string",
                        },
                        "file_path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Path to edit. Accepts `file_path` or `path`.",
                        },
                        "edits": {
                            "anyOf": [
                                {
                                    "description": "A single literal replace edit.",
                                    "properties": {
                                        "old_string": {
                                            "description": "String to replace. Accepts `old` or `old_string`.",
                                            "type": "string",
                                        },
                                        "new_string": {
                                            "description": "Replacement text. Accepts `new` or `new_string`.",
                                            "type": "string",
                                        },
                                        "replace_all": {
                                            "default": False,
                                            "description": "Replace all occurrences. When False, only the first occurrence is replaced.",
                                            "type": "boolean",
                                        },
                                        "max_replacements": {
                                            "anyOf": [
                                                {"minimum": 1, "type": "integer"},
                                                {"type": "null"},
                                            ],
                                            "default": None,
                                            "description": "Maximum number of occurrences to replace when replace_all=True. None means unlimited.",
                                        },
                                        "match_mode": {
                                            "default": "fuzzy",
                                            "description": "'fuzzy' (default): Use fuzzy matching when exact match fails (may match similar text). 'exact': Only replace literal matches of `old`.",
                                            "enum": ["exact", "fuzzy"],
                                            "type": "string",
                                        },
                                    },
                                    "required": ["old_string", "new_string"],
                                    "type": "object",
                                },
                                {
                                    "items": {
                                        "description": "A single literal replace edit.",
                                        "properties": {
                                            "old_string": {
                                                "description": "String to replace. Accepts `old` or `old_string`.",
                                                "type": "string",
                                            },
                                            "new_string": {
                                                "description": "Replacement text. Accepts `new` or `new_string`.",
                                                "type": "string",
                                            },
                                            "replace_all": {
                                                "default": False,
                                                "description": "Replace all occurrences. When False, only the first occurrence is replaced.",
                                                "type": "boolean",
                                            },
                                            "max_replacements": {
                                                "anyOf": [
                                                    {"minimum": 1, "type": "integer"},
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                                "description": "Maximum number of occurrences to replace when replace_all=True. None means unlimited.",
                                            },
                                            "match_mode": {
                                                "default": "fuzzy",
                                                "description": "'fuzzy' (default): Use fuzzy matching when exact match fails (may match similar text). 'exact': Only replace literal matches of `old`.",
                                                "enum": ["exact", "fuzzy"],
                                                "type": "string",
                                            },
                                        },
                                        "required": ["old_string", "new_string"],
                                        "type": "object",
                                    },
                                    "type": "array",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "One or more literal replace edits. Accepts `edit` or `edits`.",
                        },
                        "old_string": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Literal text to replace. Single-edit shorthand for `edit`.",
                        },
                        "new_string": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Literal replacement text. Single-edit shorthand for `edit`.",
                        },
                        "replace_all": {
                            "default": False,
                            "description": "Replace all matches. Only used with the single-edit shorthand.",
                            "type": "boolean",
                        },
                        "input": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Input text for sloppy mode.",
                        },
                        "sandbox_permissions": {
                            "anyOf": [
                                {
                                    "enum": ["workspace-write", "danger-full-access"],
                                    "type": "string",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "The wider sandbox mode this file operation needs.",
                        },
                        "justification": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Required with sandbox_permissions: explanation for the user.",
                        },
                        "allow_conflicts": {
                            "default": False,
                            "description": "When True, allow editing files that contain conflict markers.",
                            "type": "boolean",
                        },
                        "allow_auto_generated": {
                            "default": False,
                            "description": "When True, allow editing files that appear to be auto-generated (opt-out of the auto-generated-file guard). Default False refuses to modify generated files.",
                            "type": "boolean",
                        },
                        "resolved_mode": {
                            "anyOf": [
                                {"enum": ["replace", "sloppy"], "type": "string"},
                                {"type": "null"},
                            ],
                            "default": None,
                        },
                    },
                    "type": "object",
                },
            },
            {
                "name": "fetch_url",
                "description": "Fetch a URL and extract main text.",
                "parameters": {
                    "properties": {
                        "url": {
                            "description": "URL to fetch content from.",
                            "type": "string",
                        },
                        "timeout": {
                            "default": 30.0,
                            "description": "Request timeout in seconds (1-300).",
                            "maximum": 300.0,
                            "minimum": 1.0,
                            "type": "number",
                        },
                        "method": {
                            "default": "GET",
                            "description": "HTTP method to use.",
                            "enum": ["GET", "POST"],
                            "type": "string",
                        },
                        "headers": {
                            "anyOf": [
                                {
                                    "additionalProperties": {"type": "string"},
                                    "type": "object",
                                },
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Custom HTTP headers (e.g., {'Authorization': 'Bearer token'}).",
                        },
                        "body": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Request body for POST requests.",
                        },
                        "follow_redirects": {
                            "default": True,
                            "description": "Automatically follow HTTP redirects.",
                            "type": "boolean",
                        },
                        "max_redirects": {
                            "default": 5,
                            "description": "Maximum number of redirects to follow (0-20).",
                            "maximum": 20,
                            "minimum": 0,
                            "type": "integer",
                        },
                    },
                    "required": ["url"],
                    "type": "object",
                },
            },
            {
                "name": "web_extract",
                "description": "Extract page content from URLs as markdown/text (no LLM). Within char budget pages return whole; larger pages head+tail truncate with the full text saved to disk (read_file the omitted middle). On failure/timeout use fetch_url.",
                "parameters": {
                    "properties": {
                        "urls": {
                            "description": "List of URLs (or search-result objects with a 'url'/'href' field) to extract content from (max 5)",
                            "items": {},
                            "maxItems": 5,
                            "type": "array",
                        },
                        "char_limit": {
                            "anyOf": [
                                {"maximum": 500000, "minimum": 2000, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Per-page character budget (default 15000). Larger pages are head+tail truncated with the full text saved to disk.",
                        },
                    },
                    "required": ["urls"],
                    "type": "object",
                },
            },
            {
                "name": "context_prune",
                "description": "Prune old session content (reasoning, tool results, stale messages) to save tokens. Recent turns and tool-call pairs are always preserved. Modes: 'prune' (smart elision), 'compact' (full compaction), 'strip_reasoning' (remove old thinking content). Use dry_run=True to preview changes.",
                "parameters": {
                    "properties": {
                        "mode": {
                            "default": "prune",
                            "description": "Strategy: prune stale content, compact old turns, or strip old reasoning.",
                            "enum": ["prune", "compact", "strip_reasoning"],
                            "type": "string",
                        },
                        "target_token_count": {
                            "anyOf": [
                                {"minimum": 1000, "type": "integer"},
                                {"type": "null"},
                            ],
                            "default": None,
                            "description": "Target max tokens after pruning.",
                        },
                        "remove_reasoning": {
                            "default": True,
                            "description": "Remove old reasoning/thinking content.",
                            "type": "boolean",
                        },
                        "remove_tool_results": {
                            "default": True,
                            "description": "Remove old tool-result messages.",
                            "type": "boolean",
                        },
                        "keep_recent_turns": {
                            "default": 6,
                            "description": "Recent user/assistant turns to keep.",
                            "maximum": 20,
                            "minimum": 1,
                            "type": "integer",
                        },
                        "dry_run": {
                            "default": False,
                            "description": "Report what would be removed without changing the session.",
                            "type": "boolean",
                        },
                    },
                    "type": "object",
                },
            },
        ],
    },
}, {
    "method": "event",
    "type": "LLMRequest",
    "payload": {
        "kind": "loop",
        "provider": "scripted_echo",
        "model": "scripted_echo",
        "thinking_effort": None,
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "system_prompt_hash": "<SYSTEM_PROMPT_HASH>",
        "system_prompt": "<SYSTEM_PROMPT>",
        "tools_hash": "ca84214f72334d7d2e21737050eac5e86339525fce4c8d1dcd57a14b2df2fa2f",
        "message_count": 1,
        "turn_step": 1,
        "attempt": 1,
        "dropped_count": None,
    },
}, {"method": "event", "type": "StepBegin", "payload": {"n": 2}}, {"method": "event", "type": "ContentPart", "payload": {"type": "text", "text": "done"}}, {
    "method": "event",
    "type": "StatusUpdate",
    "payload": {
        "context_usage": None,
        "context_tokens": None,
        "max_context_tokens": None,
        "token_usage": None,
        "message_id": None,
        "mcp_status": None,
    },
}, {
    "method": "event",
    "type": "LLMRequest",
    "payload": {
        "kind": "loop",
        "provider": "scripted_echo",
        "model": "scripted_echo",
        "thinking_effort": None,
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "system_prompt_hash": "<SYSTEM_PROMPT_HASH>",
        "system_prompt": "<SYSTEM_PROMPT>",
        "tools_hash": "ca84214f72334d7d2e21737050eac5e86339525fce4c8d1dcd57a14b2df2fa2f",
        "message_count": 3,
        "turn_step": 2,
        "attempt": 1,
        "dropped_count": None,
    },
}, {"method": "event", "type": "TurnEnd", "payload": {}}]
        )
    finally:
        wire.close()
