from __future__ import annotations

from typing import Any
import os
import sys
from pathlib import Path
import orjson
import kimix.base as base
from kimi_cli.config import BackgroundConfig, LoopControl, SecretStr, NotificationConfig, MCPConfig, OAuthRef, OpenAISettings  # type: ignore[attr-defined]
from kimi_agent_sdk import Config
from . import _globals


# ── Provider validation constants ──────────────────────────────────────────

_REQUIRED_PROVIDER_KEYS: tuple[str, ...] = ("type", "max_context_size", "model", "url")


# Canonical sub-provider role names.
# - ``sub_agent``: default role for the Agent / subagent tool and summarization
# - ``planner``: dedicated role for /plan mode
# - ``researcher``: read-only child session for the research tool
# - ``backup``: failover role used when the main agent LLM provider fails after
#   retries (switches to backup provider and resumes the session)
_BACKUP_ROLE: str = "backup"
SUB_PROVIDER_ROLES: tuple[str, ...] = ("sub_agent", "planner", "researcher", _BACKUP_ROLE)


# Priority for picking a sub_provider as the main provider when root lacks 'model'.
# Earlier values in the tuple have higher priority.
# "researcher" is deliberately last: a researcher sub-provider must never
# become the main-provider fallback ahead of the general-purpose roles.
# "backup" is intentionally excluded: a backup provider is reserved solely for
# main-provider failover and must never become the initial primary provider.
_SUB_PROVIDER_PICK_PRIORITY: tuple[str | None, ...] = (None, "sub_agent", "planner", "researcher")


# Default agent JSON manifest for role-specific child sessions, relative to
# ``base._default_agent_file_dir``. A tool that creates a child session for a
# known role can use this instead of hardcoding the manifest path. The
# ``researcher`` role backs the research tool's read-only child session.
_ROLE_DEFAULT_AGENT_FILES: dict[str, str] = {
    "researcher": "agent_readonly.json",
}


# Keys that identify a specific provider entry and must never be inherited
# from the top-level config into sub-providers.
_SUB_PROVIDER_NO_INHERIT_KEYS: frozenset[str] = frozenset({"model", "model_name", "name", "role"})

# Provider-specific auth/transport keys that must not be inherited when a
# sub-provider explicitly declares a different ``type`` than the root config.
_CROSS_TYPE_NO_INHERIT_KEYS: frozenset[str] = frozenset({"oauth", "openai_settings"})


# Key marking a sub-provider entry as a link to another JSON config file, e.g.
# ``{"role": "sub_agent", "json": "subagent.json"}``.
_SUB_PROVIDER_LINK_KEY: str = "json"

# Keys stripped from a linked config's root before it becomes a sub-provider
# entry: the nested sub-provider fields are ignored (links do not recurse) and
# a root-level ``json`` key has no meaning outside a link entry.
_LINKED_CONFIG_DROP_KEYS: frozenset[str] = frozenset(
    {"sub_provider", "sub_providers", _SUB_PROVIDER_LINK_KEY}
)


# ── Sub-provider normalization ─────────────────────────────────────────────


def _inherit_sub_provider_defaults(
    config_data: dict[str, Any],
    sub_provider: Any,
    sub_providers: Any,
) -> None:
    """Fill missing keys in each sub-provider entry from the top-level config.

    Any key present in ``config_data`` (except identity keys such as ``model``
    and ``role``) is copied into a sub-provider entry only when that entry does
    not already define the key. Mutates the raw sub-provider entries in place
    so that later normalization/validation sees the inherited values.
    """
    if not config_data:
        return
    defaults = {
        k: v for k, v in config_data.items() if k not in _SUB_PROVIDER_NO_INHERIT_KEYS
    }
    if not defaults:
        return

    main_type = config_data.get("type")

    def _fill(entry: Any) -> None:
        if isinstance(entry, dict):
            entry_type = entry.get("type")
            cross_type = bool(main_type and entry_type and entry_type != main_type)
            for key, value in defaults.items():
                if cross_type and key in _CROSS_TYPE_NO_INHERIT_KEYS:
                    continue
                entry.setdefault(key, value)

    for src in (sub_provider, sub_providers):
        if isinstance(src, dict):
            _fill(src)
        elif isinstance(src, list):
            for entry in src:
                _fill(entry)


def _normalize_sub_providers(
    sub_provider: Any,
    sub_providers: Any,
) -> list[dict[str, Any]]:
    """Normalize ``sub_provider`` and ``sub_providers`` into a flat list.

    Each entry is validated to be a dict with required provider keys.
    Missing or empty ``role`` defaults to ``sub_agent``.
    """
    from kimix.ui.printing import print_debug, print_warning

    raw_entries: list[Any] = []
    for src in (sub_provider, sub_providers):
        if src is None:
            continue
        if isinstance(src, dict):
            raw_entries.append(src)
        elif isinstance(src, list):
            raw_entries.extend(src)
        else:
            print_warning(f"Ignoring invalid sub_provider value of type {type(src).__name__}")

    normalized: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            print_warning("Ignoring invalid sub_provider entry: expected dict")
            continue
        missing = [k for k in _REQUIRED_PROVIDER_KEYS if k not in entry]
        if missing:
            print_warning(
                f"Ignoring invalid sub_provider entry (missing keys: {', '.join(missing)})"
            )
            continue
        role = entry.get("role")
        if not role:
            entry = dict(entry)
            entry["role"] = "sub_agent"
        role = entry["role"]
        if role in seen_roles and role != "backup":
            print_debug(f"Multiple sub_providers with role '{role}'; using first match")
        seen_roles.add(role)
        normalized.append(entry)
    return normalized


def _pick_main_from_sub_providers(
    config_data: dict[str, Any],
    sub_provider: Any,
    sub_providers: Any,
) -> None:
    """If ``config_data`` lacks a ``model`` key, pick a sub_provider as the main provider.

    Priority (highest first):
      1. Entry with no ``role`` key (or empty role)
      2. Entry with ``role = "sub_agent"``
      3. Entry with ``role = "planner"``
      4. Entry with ``role = "researcher"`` (read-only role; always last)

    Mutates ``config_data`` in place by copying all non-``role`` keys from the winner.
    """
    from kimix.ui.printing import print_debug

    if "model" in config_data:
        return

    # Collect raw entries (pre-normalization) to distinguish "no role" from explicit roles.
    raw_entries: list[dict[str, Any]] = []
    for src in (sub_provider, sub_providers):
        if src is None:
            continue
        if isinstance(src, dict):
            raw_entries.append(src)
        elif isinstance(src, list):
            raw_entries.extend(src)

    picked: dict[str, Any] | None = None
    for role_priority in _SUB_PROVIDER_PICK_PRIORITY:
        if picked is not None:
            break
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            if role_priority is None:
                # no-role: entry has no 'role' key or role is empty/falsy
                if not entry.get("role"):
                    picked = entry
                    break
            else:
                if entry.get("role") == role_priority:
                    picked = entry
                    break

    if picked is not None:
        for k, v in picked.items():
            if k != "role":
                config_data[k] = v
        print_debug(
            f"Picked sub_provider (role='{picked.get('role', '')}') "
            f"as main provider (no 'model' in root)."
        )


# ── Sub-provider identity: is the child on a different LLM? ────────────────
#
# A sub-provider entry may point at a cheaper model than the main agent:
# smaller weights, less knowledge, weaker reasoning, more likely to fail. Tools
# that delegate work to a child session compare the two providers here, warn
# the parent agent in their tool description, and seed the child's system
# prompt with the resulting capability gap.


_PROVIDER_ENDPOINT_KEYS: tuple[str, ...] = ("url", "base_url")


def provider_identity(provider: Any) -> tuple[str, str, str]:
    """Return the ``(type, model, endpoint)`` identity of a provider entry.

    Two entries with the same identity name the same LLM, so a sub-provider with
    a different identity is a different (usually cheaper, weaker) model. A
    missing or malformed entry yields empty components instead of raising.
    """
    if not isinstance(provider, dict):
        return ("", "", "")
    endpoint = ""
    for key in _PROVIDER_ENDPOINT_KEYS:
        value = provider.get(key)
        if value:
            endpoint = str(value).strip().rstrip("/").lower()
            break
    return (
        str(provider.get("type") or "").strip().lower(),
        str(provider.get("model") or "").strip(),
        endpoint,
    )


def is_different_provider(a: Any, b: Any) -> bool:
    """True when *a* and *b* describe different LLMs (see :func:`provider_identity`)."""
    return provider_identity(a) != provider_identity(b)


def sub_provider_differs(role: str = "sub_agent", main_provider: Any = None) -> bool:
    """True when the sub-provider for *role* runs a different LLM than the main agent.

    Args:
        role: Sub-provider role to look up (default: the sub-agent role).
        main_provider: The main agent's provider entry; defaults to the
            process-wide one (``base._default_provider``).

    Returns:
        ``False`` when the role has no sub-provider or when either side is
        unknown -- "no comparison possible" is not evidence of a weaker model.
    """
    sub_provider = base.get_default_sub_provider(role)
    if not isinstance(sub_provider, dict):
        return False
    main = main_provider if main_provider is not None else base._default_provider
    if not isinstance(main, dict):
        return False
    return is_different_provider(sub_provider, main)


def get_backup_providers() -> list[dict[str, Any]]:
    """Return all configured ``backup`` sub-providers in declaration order."""
    return base.get_default_sub_providers_by_role(_BACKUP_ROLE)


# ── Sub-provider JSON links ────────────────────────────────────────────────
#
# A sub-provider entry may point at another JSON config file instead of
# inlining the provider fields::
#
#     "sub_providers": [
#         {"role": "sub_agent", "json": "subagent.json"}
#     ]
#
# ``subagent.json`` is looked up in the directory holding the JSON file that
# declares the link, then in each of its parent directories up to the
# filesystem root (``subagent.json`` next to the declaring file always wins).
# Only the referenced file's root-level fields are registered, as exactly one
# sub-provider entry. Expansion is deliberately NOT recursive: when the linked
# file declares its own ``sub_provider`` / ``sub_providers`` (or a nested
# ``json`` link), those are ignored -- a linked config contributes its own
# provider only, never a chain of providers.
# Extra keys on the link entry (everything but ``json``/``role``) override the
# linked file's root fields; ``role`` overrides the linked file's ``role``.


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read *path* and return its JSON object payload (empty dict on failure)."""
    from kimix.ui.printing import print_warning

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = orjson.loads(f.read())
    except orjson.JSONDecodeError as e:
        print_warning(f"Invalid JSON in config file: {str(path)} ({e})")
        return {}
    except Exception as e:
        print_warning(f"Failed to load config file: {str(path)} ({e})")
        return {}
    if not isinstance(data, dict):
        print_warning(f"Config file must contain a JSON object: {str(path)}")
        return {}
    return data


def _resolve_linked_config(name: str, origin_dir: Path) -> Path | None:
    """Locate linked config *name* starting at *origin_dir*, walking up to root.

    Absolute paths are used as-is. Returns the resolved file path, or ``None``
    when the file exists in no ancestor directory of *origin_dir*.
    """
    candidate = Path(name)
    if candidate.is_absolute():
        return candidate.resolve() if candidate.is_file() else None

    try:
        origin_dir = origin_dir.resolve()
    except OSError:
        pass
    for directory in (origin_dir, *origin_dir.parents):
        found = directory / name
        if found.is_file():
            return found.resolve()
    return None


def _expand_sub_provider_links(entries: list[Any], origin_dir: Path) -> list[Any]:
    """Return *entries* with every ``{"json": ...}`` link replaced by its config.

    Non-link entries are passed through untouched so inline sub-providers keep
    working. Each link yields exactly one entry built from the linked config's
    root fields; expansion never recurses, so a linked config's own
    ``sub_provider`` / ``sub_providers`` are ignored.
    """
    from kimix.ui.printing import print_debug, print_warning

    expanded: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict) or _SUB_PROVIDER_LINK_KEY not in entry:
            expanded.append(entry)
            continue

        target = entry.get(_SUB_PROVIDER_LINK_KEY)
        if not isinstance(target, str) or not target.strip():
            print_warning(
                f"Ignoring sub_provider link with invalid "
                f"'{_SUB_PROVIDER_LINK_KEY}' value: {target!r}"
            )
            continue

        resolved = _resolve_linked_config(target, origin_dir)
        if resolved is None:
            print_warning(
                f"Sub_provider config file not found: {target} "
                f"(searched upward from {str(origin_dir)})"
            )
            continue

        linked = _read_json_file(resolved)
        if not linked:
            continue

        print_debug(f"Sub_provider config link resolved: {target} -> {str(resolved)}")
        expanded.append(_linked_config_to_entry(linked, entry, resolved))
    return expanded


def _linked_config_to_entry(
    linked: dict[str, Any],
    link_entry: dict[str, Any],
    origin: Path | None = None,
) -> dict[str, Any]:
    """Turn a linked JSON config into the single sub-provider entry it declares.

    The linked file's root-level fields become the entry; its own
    ``sub_provider`` / ``sub_providers`` (and any nested ``json`` link) are
    dropped, because links do not recurse. Keys on *link_entry* other than
    ``json`` / ``role`` override the linked root fields, and ``role`` on the
    link entry overrides the linked file's role.
    """
    from kimix.ui.printing import print_debug

    ignored = [key for key in ("sub_provider", "sub_providers") if linked.get(key)]
    if ignored:
        print_debug(
            f"Ignoring nested {' / '.join(ignored)} declared in linked sub_provider "
            f"config {str(origin) if origin is not None else '<linked config>'}: "
            f"sub_provider json links are not recursive"
        )

    entry = {key: value for key, value in linked.items() if key not in _LINKED_CONFIG_DROP_KEYS}
    for key, value in link_entry.items():
        if key not in (_SUB_PROVIDER_LINK_KEY, "role"):
            entry[key] = value
    role = link_entry.get("role")
    if role:
        entry["role"] = role
    return entry


def _expand_config_sub_provider_links(config_data: dict[str, Any], origin_dir: Path) -> None:
    """Rewrite ``sub_provider``/``sub_providers`` in *config_data* in place.

    Every ``{"json": ...}`` entry is replaced by the entries of the referenced
    config file (see :func:`_expand_sub_provider_links`), so downstream
    normalization only ever sees fully materialized provider dicts.
    """
    for key in ("sub_provider", "sub_providers"):
        src = config_data.get(key)
        if isinstance(src, dict):
            entries: list[Any] = [src]
        elif isinstance(src, list):
            entries = src
        else:
            continue
        config_data[key] = _expand_sub_provider_links(entries, origin_dir)


# ── Config file loading ────────────────────────────────────────────────────


def _resolve_config_file(config_path: Path) -> Path | None:
    """Search for a JSON config file, returning its resolved path or ``None``.

    Searches in order:
      1. Direct path (``config_path`` itself)
      2. Parent directories of CWD
      3. Parent directories of this file (``config.py``)
      4. ``PATH`` environment variable directories
    """
    path = config_path

    if path.exists() and path.is_file():
        return path.resolve()

    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / path.name
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    file_dir = Path(__file__).resolve().parent
    for parent in [file_dir, *file_dir.parents]:
        candidate = parent / path.name
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        path_dir = path_dir.strip()
        if not path_dir:
            continue
        candidate = Path(path_dir) / path.name
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    return None


def _load_config_file(config_path: Path) -> dict[str, Any]:
    """Search for and load a JSON config file (see :func:`_resolve_config_file`)."""
    from kimix.ui.printing import print_error

    path = _resolve_config_file(config_path)
    if path is None:
        print_error(f"Config file not found: {str(config_path)}")
        sys.exit(1)

    return _read_json_file(path)


# ── .kimix/config.json loading ─────────────────────────────────────────────

# Cache of parsed ``.kimix/config.json`` contents, keyed by resolved path, so
# the file is read and parsed with orjson at most once per path per process.
_config_json_cache: dict[Path, dict[str, Any]] = {}


def load_config_json(work_dir: str | Path | None = None) -> dict[str, Any]:
    """Load and parse ``<work_dir>/.kimix/config.json`` with orjson (once per path).

    The parsed dict is cached by resolved path, so repeated calls never touch
    the filesystem or re-parse the JSON. Missing files, invalid JSON and
    non-object payloads all yield an empty dict.

    Args:
        work_dir: Directory containing ``.kimix/config.json``. Defaults to CWD.

    Returns:
        The parsed config dict (possibly empty). The cached instance is
        returned directly; callers must not mutate it.
    """
    root = Path(work_dir) if work_dir is not None else Path.cwd()
    config_path = (root / ".kimix" / "config.json").resolve()
    cached = _config_json_cache.get(config_path)
    if cached is not None:
        return cached

    config_json: dict[str, Any] = {}
    try:
        raw = config_path.read_bytes()
        loaded = orjson.loads(raw)
        if isinstance(loaded, dict):
            config_json = loaded
    except (OSError, orjson.JSONDecodeError, ValueError):
        pass
    _config_json_cache[config_path] = config_json
    return config_json


def clear_config_json_cache() -> None:
    """Drop all cached ``.kimix/config.json`` payloads (useful after edits/tests)."""
    _config_json_cache.clear()


# ── Skill directory auto-loading ───────────────────────────────────────────


def _collect_skill_dirs(
    skill_dir_cfg: Any,
    cwd: Path,
) -> list["KaosPath"]:
    """Resolve a ``skill_dir`` config value (str or list) into existing dirs."""
    from kimix.ui.printing import print_debug, print_warning
    from kaos.path import KaosPath

    if isinstance(skill_dir_cfg, str):
        skill_dir_cfg = [skill_dir_cfg]
    if not isinstance(skill_dir_cfg, list):
        return []

    resolved: list[KaosPath] = []
    for sd in skill_dir_cfg:
        if not isinstance(sd, str):
            continue
        sd_path = Path(sd)
        if not sd_path.is_absolute():
            sd_path = cwd / sd_path
        sd_path = sd_path.resolve()
        if sd_path.exists() and sd_path.is_dir():
            resolved.append(KaosPath(str(sd_path)))
            print_debug(f"Skill dir from config: {str(sd_path)}")
        else:
            print_warning(f"Skill dir from config not found: {str(sd_path)}")
    return resolved


def _load_skill_dirs() -> None:
    """Auto-load skill directories from config files in CWD.

    Skill dirs are sought in both ``.kimix/config.json`` (the current setting,
    parsed once via :func:`load_config_json`) and the deprecated
    ``.kimix/skill.json``. If ``skill.json`` exists, a deprecation warning is
    logged for the user.
    """
    from kimix.ui.printing import print_debug, print_warning

    cwd = Path.cwd()
    skill_dirs_from_cfg: list[Any] = []

    # 1. Current setting: skill_dir inside .kimix/config.json
    config_json = load_config_json(cwd)
    skill_dirs_from_cfg.extend(_collect_skill_dirs(config_json.get("skill_dir"), cwd))

    # 2. Deprecated setting: .kimix/skill.json
    skill_json_path = cwd / ".kimix" / "skill.json"
    if skill_json_path.exists():
        print_warning(
            ".kimix/skill.json is deprecated; move its 'skill_dir' setting "
            "into .kimix/config.json instead."
        )
        try:
            skill_json = orjson.loads(skill_json_path.read_text(encoding="utf-8"))
            if isinstance(skill_json, dict):
                print_debug(".kimix/skill.json exists.")
                skill_dirs_from_cfg.extend(
                    _collect_skill_dirs(skill_json.get("skill_dir"), cwd)
                )
        except Exception as e:
            print_warning(f"Failed to read skill_dir from .kimix/skill.json: {e}")

    if skill_dirs_from_cfg:
        existing = list(base._default_skill_dirs)
        seen = {str(p) for p in existing}
        for sd in skill_dirs_from_cfg:
            if str(sd) not in seen:
                seen.add(str(sd))
                existing.append(sd)
        base.set_default_skill_dirs(existing)


# ── Load provider config dict (used by init and by default_config path) ─────


def _load_and_set_provider(
    config_data: dict[str, Any],
    config_path: str | Path | None = None,
) -> None:
    """Extract sub_provider info, normalize, and set on base globals.

    ``config_path`` is the JSON file ``config_data`` came from, when known. It
    anchors relative ``{"json": ...}`` sub-provider links and their upward
    search (see :func:`_expand_config_sub_provider_links`); when unknown -- e.g.
    inline ``config_json`` -- the current working directory is used.

    Mutates ``config_data`` in place (expands and pops
    ``sub_provider``/``sub_providers``).
    """
    from kimix.ui.printing import print_debug

    if config_path is not None:
        origin_dir = Path(config_path).resolve().parent
    else:
        origin_dir = Path.cwd()
    _expand_config_sub_provider_links(config_data, origin_dir)

    sub_provider = config_data.pop("sub_provider", None)
    sub_providers = config_data.pop("sub_providers", None)
    _inherit_sub_provider_defaults(config_data, sub_provider, sub_providers)
    normalized_providers = _normalize_sub_providers(sub_provider, sub_providers)
    _pick_main_from_sub_providers(config_data, sub_provider, sub_providers)
    base.set_default_provider(config_data)
    print_debug(f"Provider model: {config_data.get('model', 'None')}")
    if normalized_providers:
        base.set_default_sub_providers(normalized_providers)
        roles = [p.get("role", "sub_agent") for p in normalized_providers]
        print_debug(f"Sub-provider roles loaded: {', '.join(roles)}")


# ── Public init() ──────────────────────────────────────────────────────────


def init(
    config_path: str | Path | None = None,
    config_json: str | None = None,
    *,
    yolo: bool = True,
    think: bool = True,
    skill_dir: list[str] | None = None,
    manually_cot: bool = False,
    colorful_print: bool = True,
    clean: bool = False,
) -> None:
    """Initialize kimix global state. Must be called before ``prompt()`` in non-CLI scripts.

    Args:
        config_path: Path to a JSON config file (provider settings).
        config_json: JSON string with provider settings (alternative to config_path).
        yolo: Enable YOLO mode (auto-approve tool calls).
        think: Enable thinking mode.
        skill_dir: Additional skill directories to load.
        manually_cot: Enable manually CoT mode.
        colorful_print: Enable ANSI-colorful output.
        clean: Delete session cache after quit.
    """
    # Lazy imports to avoid circular dependencies:
    #   session.py imports _create_config from this module,
    #   so we import session internals only inside this function body.
    from kimix.utils.session import close_session
    from kimix.cli_impl import constants
    from kimix.ui.printing import print_debug, print_error, print_warning
    from kaos.path import KaosPath

    # 1. Dispose existing default session (if any)
    if _globals._default_session is not None:
        sess = _globals._default_session
        _globals._default_session = None
        close_session(sess)

    # 2. Reset / set base globals
    base._colorful_print = colorful_print
    import kimix.ui.printing as _printing

    _printing._colorful_print = colorful_print
    base.set_default_thinking(think)
    base.set_default_yolo(yolo)
    base.set_default_manually_cot(manually_cot)

    # 3. Clean mode
    constants.CLEAN_MODE = clean
    if clean:
        print_debug("Clean mode ON, delete cache file after quit.")

    # 4. Skill directories
    # Reset to auto-detected defaults (empty first, then auto-load)
    base._default_skill_dirs = []

    # Auto-load from .kimix/config.json (and deprecated .kimix/skill.json)
    _load_skill_dirs()

    # Add explicit skill dirs
    if skill_dir:
        existing = list(base._default_skill_dirs)
        for sd in skill_dir:
            sd_path = Path(sd)
            if not sd_path.is_absolute():
                sd_path = Path.cwd() / sd_path
            sd_path = sd_path.resolve()
            if sd_path.exists() and sd_path.is_dir():
                existing.append(KaosPath(str(sd_path)))
                print_debug(f"Skill dir added: {str(sd_path)}")
            else:
                print_warning(f"Skill dir not found: {str(sd_path)}")
        base.set_default_skill_dirs(existing)

    # 5. Load provider config
    if config_path is not None:
        resolved_config = _resolve_config_file(Path(config_path))
        if resolved_config is None:
            print_error(f"Config file not found: {str(config_path)}")
            sys.exit(1)
        config_data = _read_json_file(resolved_config)
        if config_data:
            # Anchor relative `{"json": ...}` sub-provider links on this file.
            _load_and_set_provider(config_data, resolved_config)
    elif config_json is not None:
        try:
            config_data = orjson.loads(config_json)
            if not isinstance(config_data, dict):
                raise ValueError("config_json must parse to a JSON object")
            # No backing file: resolve sub-provider links from the CWD upward.
            _load_and_set_provider(config_data, None)
        except orjson.JSONDecodeError as e:
            print_error(f"Invalid config JSON: {e}")
            sys.exit(1)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Fallback to default_config.json
        default_config_path = Path(__file__).parent.parent / "default_config.json"
        if default_config_path.exists():
            config_data = _read_json_file(default_config_path)
            if config_data:
                _load_and_set_provider(config_data, default_config_path)


# ── Generic config construction from a provider dict ───────────────────────


def _resolve_api_key(provider_dict: dict[str, Any]) -> str:
    """Resolve the API key from the provider dict or environment variables."""
    api_key = provider_dict.get("api_key")
    if not api_key:
        api_key = os.environ.get("KIMI_API_KEY")
    if not api_key:
        api_key = os.environ.get("KIMIX_API_KEY")
    if not api_key:
        from kimix.ui.printing import print_warning

        print_warning(
            "api_key not found. May config in JSON, or set to env `KIMI_API_KEY` or `KIMIX_API_KEY`"
        )
        api_key = ""
    return api_key


def _build_llm_model(provider_dict: dict[str, Any]) -> "LLMModel":
    """Build ``LLMModel`` generically from provider_dict keys.

    Any key that is a declared field of ``LLMModel`` is extracted from
    ``provider_dict`` and passed to the constructor.
    """
    from kimi_cli.config import LLMModel

    model_data = {key: provider_dict[key] for key in LLMModel.model_fields if key in provider_dict}
    model_data["max_context_size"] = int(model_data["max_context_size"])
    return LLMModel(**model_data)


def _build_llm_provider(provider_dict: dict[str, Any]) -> "LLMProvider":
    """Build ``LLMProvider`` generically from provider_dict keys.

    Any key that is a declared field of ``LLMProvider`` is extracted from
    ``provider_dict``. Special handling applies to ``base_url``/``url``,
    ``api_key``, ``oauth`` and ``openai_settings``.
    """
    from kimi_cli.config import LLMProvider

    provider_data: dict[str, Any] = {}
    for key in LLMProvider.model_fields:
        if key == "base_url":
            provider_data[key] = provider_dict.get("base_url") or provider_dict.get("url")
        elif key == "api_key":
            provider_data[key] = SecretStr(_resolve_api_key(provider_dict))
        elif key == "oauth":
            oauth_dict = provider_dict.get("oauth")
            if isinstance(oauth_dict, dict):
                provider_data[key] = OAuthRef(**oauth_dict)
        elif key == "openai_settings":
            settings_dict = provider_dict.get("openai_settings")
            if isinstance(settings_dict, dict):
                provider_data[key] = OpenAISettings(**settings_dict)
        elif key in provider_dict:
            provider_data[key] = provider_dict[key]
    return LLMProvider(**provider_data)


def _get_config_recognized_keys() -> set[str]:
    """Return the set of provider_dict keys that are recognized by ``Config``.

    This is derived directly from the declared fields of ``Config``,
    ``LLMModel`` and ``LLMProvider``, plus legacy/alias keys.
    """
    from kimi_cli.config import LLMModel, LLMProvider

    return (
        set(Config.model_fields.keys())
        | set(LLMModel.model_fields.keys())
        | set(LLMProvider.model_fields.keys())
        | {"url", "model_name", "name", "role"}
    )


def _create_config(provider_dict: dict[str, Any] | None = None) -> tuple[Config, dict[str, Any] | None]:
    from kimix.ui.printing import print_warning

    provider_dict = provider_dict if provider_dict is not None else base._default_provider
    cfg = Config()

    if provider_dict is None:
        try:
            provider_dict = orjson.loads(
                (Path(__file__).parent.parent / 'default_config.json').read_text(encoding='utf-8', errors='replace'))
            if not isinstance(provider_dict, dict):
                provider_dict = None
        except Exception:
            pass

    if provider_dict is not None:
        # Apply environment variables first.
        env = provider_dict.get("env")
        if isinstance(env, dict):
            for k, v in env.items():
                os.environ[k] = v

        # Validate required provider/model keys.
        assert provider_dict.get("type") is not None, "`provider_type` must be provided in config"
        assert provider_dict.get("max_context_size") is not None, "`max_context_size` must be provided in config"
        assert isinstance(provider_dict.get("model"), str), "model(str) must be provided in config"
        assert provider_dict.get("url") is not None or provider_dict.get("base_url") is not None, "url must be provided in config"

        from kimi_cli.config import LLMModel, LLMProvider

        provider_flat_keys = (
            set(LLMModel.model_fields.keys())
            | set(LLMProvider.model_fields.keys())
            | {"url"}
        )

        # Build a normalized dict that matches the ``Config`` schema.
        config_data: dict[str, Any] = {
            key: value
            for key, value in provider_dict.items()
            if key in Config.model_fields and key not in provider_flat_keys
        }
        config_data["model"] = _build_llm_model(provider_dict)
        config_data["provider"] = _build_llm_provider(provider_dict)

        cfg = Config.model_validate(config_data)

        # Warn about unrecognized keys.
        unrecognized_keys = [k for k in provider_dict if k not in _get_config_recognized_keys()]
        if unrecognized_keys:
            print_warning(f"Unrecognized keys in provider_dict: {unrecognized_keys}")

    return cfg, provider_dict
