"""Deep memory-benchmark harness for the kimi-agent project.

Measurement-only tool: it never mutates production code.  Every measurement is
taken in the *current* process (or, for ``--all``, in a fresh subprocess per
case so that import/RSS numbers are never polluted by earlier cases).

Usage
-----
    python tools/mem_bench.py --list
    python tools/mem_bench.py import_cli
    python tools/mem_bench.py agent_loop --repeat 3 --json .kimix_cache/mem.json
    python tools/mem_bench.py --all --json .kimix_cache/mem_bench_results.json
    python tools/mem_bench.py static_sweep

``--repeat N`` re-runs a case in the *same* process: use it for steady-state
workloads only (module imports are warm after the first iteration, so import
cases should stay at ``--repeat 1``; ``--all`` already isolates every case in a
fresh subprocess).

Metrics recorded per case
-------------------------
* ``tracemalloc`` current + peak bytes, plus the top-N allocation sites
  (snapshot diff, ``compare_to(..., 'lineno')``).
* Process RSS before/after (``psutil`` when importable, otherwise a tiny
  cross-platform ``ctypes``/``/proc`` fallback).
* Live-object census: count + deep size of the top-N types, computed with an
  id-based visited set and ``sys.getsizeof`` per object (``deep_size``).
* Wall-clock duration in ms (reported, but never the primary metric).

Optional third-party helpers (``psutil``, ``pympler``) are used when present and
silently skipped when absent, so the harness degrades gracefully.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import ctypes
import gc
import importlib
import io
import json
import os
import pkgutil
import platform
import sys
import tempfile
import time
import tracemalloc
import types
from collections import Counter, deque
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------- #
# Repo layout / import path
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent
KIMI_SRC = ROOT / "kimi-cli" / "src"
KIMIX_SRC = ROOT / "src"
CACHE_DIR = ROOT / ".kimix_cache"

_TRACE_FRAMES = 12
"""tracemalloc frame depth: enough to attribute a site, cheap enough for 500-msg cases."""

_TOP_SITES = 15
_TOP_TYPES = 20


def ensure_import_paths() -> None:
    """Make ``kimi_cli`` / ``kimix`` / ``kosong`` importable from the repo root.

    Order matters: ``src`` (the real ``kimix`` package) must come before
    ``kimi-cli/src``, which contains a smaller legacy ``kimix`` shim that would
    otherwise shadow it.
    """
    for path in reversed((KIMIX_SRC, KIMI_SRC, ROOT)):
        entry = str(path)
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


# --------------------------------------------------------------------------- #
# RSS helper (psutil -> ctypes/psapi -> /proc)
# --------------------------------------------------------------------------- #

_PSUTIL_PROC: Any | None = None
_PSUTIL_TRIED = False
_PSAPI_AVAILABLE: bool | None = None


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _init_rss_backend() -> None:
    global _PSUTIL_PROC, _PSUTIL_TRIED, _PSAPI_AVAILABLE
    if _PSUTIL_TRIED:
        return
    _PSUTIL_TRIED = True
    try:  # pragma: no cover - optional dependency
        import psutil  # type: ignore[import-not-found]

        _PSUTIL_PROC = psutil.Process()
        return
    except Exception:
        _PSUTIL_PROC = None
    if sys.platform == "win32":
        try:  # pragma: no cover - Windows only
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.K32GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            kernel32.K32GetProcessMemoryInfo.restype = ctypes.c_int
            k32 = kernel32.K32GetProcessMemoryInfo
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            ok = k32(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb)
            _PSAPI_AVAILABLE = bool(ok)
        except Exception:
            _PSAPI_AVAILABLE = False


def rss_backend() -> str:
    _init_rss_backend()
    if _PSUTIL_PROC is not None:
        return "psutil"
    if _PSAPI_AVAILABLE:
        return "ctypes:K32GetProcessMemoryInfo"
    return "procfs" if Path("/proc/self/status").exists() else "unavailable"


def rss_bytes() -> int:
    """Resident set size of the current process, in bytes (0 when unavailable)."""
    _init_rss_backend()
    if _PSUTIL_PROC is not None:  # pragma: no cover - exercised when installed
        try:
            return int(_PSUTIL_PROC.memory_info().rss)
        except Exception:
            pass
    if sys.platform == "win32" and _PSAPI_AVAILABLE:  # pragma: no cover - Windows only
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32 = kernel32.K32GetProcessMemoryInfo
            k32.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            k32.restype = ctypes.c_int
            if k32(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            pass
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


# --------------------------------------------------------------------------- #
# deep_size walker
# --------------------------------------------------------------------------- #

_SHALLOW_ONLY = (
    str,
    bytes,
    bytearray,
    int,
    float,
    complex,
    bool,
    type(None),
    range,
    slice,
    memoryview,
)

_NEVER_TRAVERSE = (
    types.ModuleType,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.BuiltinMethodType,
    types.FrameType,
    types.CodeType,
    types.TracebackType,
    types.GeneratorType,
    type,
)

_MAX_VISITED = 3_000_000


def _slot_values(obj: Any) -> Iterable[Any]:
    for klass in type(obj).__mro__:
        slots = klass.__dict__.get("__slots__")
        if not slots:
            continue
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name in ("__dict__", "__weakref__", "__pydantic_fields_set__"):
                continue
            try:
                yield getattr(obj, name)
            except Exception:
                continue


def deep_size(obj: Any, seen: set[int] | None = None, depth: int = 0) -> int:
    """Recursive (``sys.getsizeof``-per-object) deep size of *obj*.

    ``seen`` is an id-based visited set so shared references are counted once
    across a whole census pass (objects reachable twice are attributed to the
    first path that reached them).
    """
    if seen is None:
        seen = set()
    if depth > 80:
        return 0
    oid = id(obj)
    if oid in seen:
        return 0
    if len(seen) > _MAX_VISITED:
        return 0
    seen.add(oid)
    try:
        size = sys.getsizeof(obj, 0)
    except Exception:
        size = 0

    if isinstance(obj, _SHALLOW_ONLY):
        return size

    if isinstance(obj, _NEVER_TRAVERSE):
        # never walk modules / functions / classes: they own the whole program
        return size

    if isinstance(obj, dict):
        for key, value in obj.items():
            size += deep_size(key, seen, depth + 1)
            size += deep_size(value, seen, depth + 1)
        return size

    if isinstance(obj, (list, tuple, set, frozenset, deque)):
        for item in obj:
            size += deep_size(item, seen, depth + 1)
        return size

    # pydantic v2 keeps field values in __dict__; extra fields are separate.
    # Attribute access on some pydantic internals (e.g. MockValSer) raises, so
    # every probe is defensive: an unreadable object just contributes shallow.
    try:
        extra = getattr(obj, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            size += deep_size(extra, seen, depth + 1)
        fields_set = getattr(obj, "__pydantic_fields_set__", None)
        if isinstance(fields_set, (set, frozenset)):
            if id(fields_set) not in seen:
                seen.add(id(fields_set))
                size += sys.getsizeof(fields_set, 0)

        namespace = getattr(obj, "__dict__", None)
        if isinstance(namespace, dict):
            size += deep_size(namespace, seen, depth + 1)
    except Exception:
        return size

    for value in _slot_values(obj):
        try:
            size += deep_size(value, seen, depth + 1)
        except Exception:
            continue
    return size


def _type_label(obj: Any) -> str:
    cls = type(obj)
    module = getattr(cls, "__module__", "")
    if module in ("builtins", "__main__", ""):
        return cls.__qualname__
    return f"{module}.{cls.__qualname__}"


def census(top_n: int = _TOP_TYPES) -> dict[str, Any]:
    """Live-object census: count + deep size per type (ranked by deep size).

    Run with ``tracemalloc`` stopped: the walker allocates (label cache, seen
    sets) and tracing would slow it down by an order of magnitude.
    """
    gc.collect()
    started = time.perf_counter()
    objs = gc.get_objects()
    seen: set[int] = set()
    label_of_type: dict[type, str] = {}
    skipped = 0
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    for obj in objs:
        cls = type(obj)
        label = label_of_type.get(cls)
        if label is None:
            label = _type_label(obj)
            label_of_type[cls] = label
        counts[label] += 1
        try:
            sizes[label] += deep_size(obj, seen)
        except Exception:
            skipped += 1
    ranked = sorted(sizes.items(), key=lambda item: item[1], reverse=True)
    interesting = {
        "dict": counts.get("dict", 0),
        "list": counts.get("list", 0),
        "str": counts.get("str", 0),
        "tuple": counts.get("tuple", 0),
        "set": counts.get("set", 0),
        "base_model": sum(
            count for label, count in counts.items() if _is_pydantic_model_label(label)
        ),
        "message": counts.get("kosong.message.Message", 0),
        "text_part": counts.get("kosong.message.TextPart", 0),
        "tool_call": counts.get("kosong.message.ToolCall", 0),
    }
    return {
        "objects_scanned": len(objs),
        "objects_visited": len(seen),
        "census_ms": round((time.perf_counter() - started) * 1000, 2),
        "truncated": len(seen) >= _MAX_VISITED,
        "skipped_objects": skipped,
        "top_types": [
            {"type": label, "count": counts[label], "deep_bytes": size}
            for label, size in ranked[:top_n]
        ],
        "selected_counts": interesting,
    }


def _is_pydantic_model_label(label: str) -> bool:
    return label.startswith(("pydantic.", "kosong.message.", "kimi_cli."))


# --------------------------------------------------------------------------- #
# tracemalloc snapshot helpers
# --------------------------------------------------------------------------- #

def _site_of(stat: tracemalloc.Statistic) -> dict[str, Any]:
    tb = stat.traceback
    frame = tb[0] if len(tb) else None
    return {
        "site": f"{frame.filename}:{frame.lineno}" if frame else "<unknown>",
        "function": getattr(frame, "name", "") if frame else "",
        "size_bytes": int(stat.size_diff),
        "count": int(stat.count_diff),
    }


def snapshot_diff(
    base: tracemalloc.Snapshot, top: int = _TOP_SITES
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Top allocation sites + top allocating files between *base* and now."""
    snap = tracemalloc.take_snapshot()
    stats = snap.compare_to(base, "lineno")
    ranked = sorted(stats, key=lambda s: s.size_diff, reverse=True)
    sites = [_site_of(s) for s in ranked[:top] if s.size_diff > 0]
    stats_file = snap.compare_to(base, "filename")
    ranked_file = sorted(stats_file, key=lambda s: s.size_diff, reverse=True)
    files = [
        {
            "file": str(s.traceback[0].filename) if len(s.traceback) else "<unknown>",
            "size_bytes": int(s.size_diff),
            "count": int(s.count_diff),
        }
        for s in ranked_file[:top]
        if s.size_diff > 0
    ]
    return sites, files


def module_attribution(base: tracemalloc.Snapshot, top: int = 25) -> list[dict[str, Any]]:
    """Attribute allocated bytes to the Python module that owns each filename."""
    snap = tracemalloc.take_snapshot()
    file_to_module = _file_module_map()
    per_module: dict[str, list[int]] = {}
    for stat in snap.compare_to(base, "filename"):
        if stat.size_diff <= 0 or not len(stat.traceback):
            continue
        filename = str(stat.traceback[0].filename)
        module = file_to_module.get(os.path.normcase(filename), filename)
        bucket = per_module.setdefault(module, [0, 0])
        bucket[0] += int(stat.size_diff)
        bucket[1] += int(stat.count_diff)
    ranked = sorted(per_module.items(), key=lambda kv: kv[1][0], reverse=True)
    return [
        {"module": name, "size_bytes": size, "count": count}
        for name, (size, count) in ranked[:top]
    ]


_CACHED_FILE_MAP: dict[str, str] | None = None


def _file_module_map() -> dict[str, str]:
    global _CACHED_FILE_MAP
    if _CACHED_FILE_MAP is None:
        mapping: dict[str, str] = {}
        for name, module in list(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if filename:
                mapping[os.path.normcase(os.path.abspath(filename))] = name
        _CACHED_FILE_MAP = mapping
    return _CACHED_FILE_MAP


# --------------------------------------------------------------------------- #
# Case registry
# --------------------------------------------------------------------------- #

CaseFn = Callable[[], dict[str, Any]]
CASES: dict[str, CaseFn] = {}
CASE_ORDER: list[str] = []
CASE_DOCS: dict[str, str] = {}
CASE_OPTIONS: dict[str, dict[str, Any]] = {}


def case(
    name: str,
    doc: str = "",
    *,
    use_tracemalloc: bool = True,
    frames: int | None = None,
    preload: Callable[[], Any] | None = None,
) -> Callable[[CaseFn], CaseFn]:
    """Register a case.

    ``preload`` runs *before* the measurement window so that module-import cost
    is not attributed to the workload (except for the import cases, which are
    about import cost itself).
    """

    def register(fn: CaseFn) -> CaseFn:
        CASES[name] = fn
        CASE_ORDER.append(name)
        CASE_DOCS[name] = doc or (fn.__doc__ or "").strip().split("\n")[0]
        CASE_OPTIONS[name] = {
            "use_tracemalloc": use_tracemalloc,
            "frames": frames,
            "preload": preload,
        }
        return fn

    return register


def _measured(fn: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    """Run ``fn`` and report duration + peak/retained traced bytes for that phase.

    Peak is measured relative to the traced bytes at phase start (``reset_peak``
    alone would report the whole-process high-water mark).
    """
    before = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    started = time.perf_counter()
    out: Any = None
    error: str | None = None
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.perf_counter() - started) * 1000
    peak = tracemalloc.get_traced_memory()[1] - before
    after = tracemalloc.get_traced_memory()[0]
    return out, {
        "duration_ms": round(duration_ms, 2),
        "peak_bytes": peak,
        "retained_bytes": after - before,
        "error": error,
    }


def _mib(num_bytes: float) -> float:
    return round(num_bytes / (1024 * 1024), 3)


def _kib(num_bytes: float) -> float:
    return round(num_bytes / 1024, 1)


@dataclass
class Measurement:
    tracemalloc_current: int = 0
    tracemalloc_peak: int = 0
    rss_before: int = 0
    rss_after: int = 0
    py_alloc_before: int = 0
    duration_ms: float = 0.0
    top_sites: list[dict[str, Any]] = field(default_factory=list)
    top_files: list[dict[str, Any]] = field(default_factory=list)
    census: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracemalloc_current_bytes": self.tracemalloc_current,
            "tracemalloc_current_mib": _mib(self.tracemalloc_current),
            "tracemalloc_peak_bytes": self.tracemalloc_peak,
            "tracemalloc_peak_mib": _mib(self.tracemalloc_peak),
            "rss_before_bytes": self.rss_before,
            "rss_after_bytes": self.rss_after,
            "rss_delta_bytes": self.rss_after - self.rss_before,
            "rss_delta_mib": _mib(self.rss_after - self.rss_before),
            "py_alloc_before_bytes": self.py_alloc_before,
            "duration_ms": round(self.duration_ms, 2),
            "top_sites": self.top_sites,
            "top_files": self.top_files,
            "census": self.census,
        }


def run_case(name: str, *, do_census: bool = True) -> dict[str, Any]:
    """Run one case in this process and measure it."""
    fn = CASES[name]
    options = CASE_OPTIONS.get(name, {})
    use_tracemalloc = options.get("use_tracemalloc", True)
    frames = options.get("frames") or _TRACE_FRAMES
    preload = options.get("preload")
    if preload is not None:
        try:
            preload()
        except Exception:  # noqa: BLE001 - a broken preload must not kill the case
            pass
    gc.collect()
    if use_tracemalloc:
        tracemalloc.start(frames)
        tracemalloc.reset_peak()
        base = tracemalloc.take_snapshot()
        py0 = tracemalloc.get_traced_memory()[0]
    else:
        base = None
        py0 = 0
    rss0 = rss_bytes()
    started = time.perf_counter()
    payload: dict[str, Any] = {}
    error: str | None = None
    traceback_text: str | None = None
    try:
        payload = fn() or {}
    except Exception as exc:  # noqa: BLE001 - a broken case must not kill --all
        import traceback as _tb

        error = f"{type(exc).__name__}: {exc}"
        traceback_text = _tb.format_exc(limit=12)
    duration_ms = (time.perf_counter() - started) * 1000
    gc.collect()
    if use_tracemalloc:
        current, peak = tracemalloc.get_traced_memory()
    else:
        current, peak = 0, 0
    rss1 = rss_bytes()
    keep_alive = payload.pop("keep", None)
    measurement = Measurement(
        tracemalloc_current=current - py0,
        tracemalloc_peak=peak - py0,
        rss_before=rss0,
        rss_after=rss1,
        py_alloc_before=py0,
        duration_ms=duration_ms,
    )
    if use_tracemalloc and base is not None:
        try:
            measurement.top_sites, measurement.top_files = snapshot_diff(base, _TOP_SITES)
        except Exception as exc:  # pragma: no cover - defensive
            error = error or f"snapshot_diff failed: {exc}"
        tracemalloc.stop()
    if do_census and not payload.get("skip_census"):
        try:
            measurement.census = census(_TOP_TYPES)
        except Exception as exc:  # pragma: no cover - defensive
            measurement.census = {"error": f"{type(exc).__name__}: {exc}"}
    del keep_alive  # release only after measurement
    result = {
        "case": name,
        "measurement": measurement.as_dict(),
        "metrics": payload.get("metrics", {}),
        "tables": payload.get("tables", {}),
        "notes": payload.get("notes", []),
        "error": error,
        "traceback": traceback_text,
    }
    return result


# --------------------------------------------------------------------------- #
# Fixtures shared by several cases (mirrors kimi-cli/tests/conftest.py)
# --------------------------------------------------------------------------- #

def _kaos_setup():
    from kaos import get_current_kaos, reset_current_kaos, set_current_kaos
    from kaos.local import LocalKaos

    return get_current_kaos, reset_current_kaos, set_current_kaos, LocalKaos


@contextmanager
def temp_env(prefix: str = "membench_"):
    """Temp work dir + LocalKaos + chdir, cleaned up afterwards."""
    import shutil

    get_current_kaos, reset_current_kaos, set_current_kaos, LocalKaos = _kaos_setup()
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    work = Path(tmpdir) / "work"
    work.mkdir()
    previous_cwd = Path.cwd()
    token = set_current_kaos(LocalKaos())
    try:
        os.chdir(work)
        yield Path(tmpdir), work
    finally:
        try:
            reset_current_kaos(token)
        except Exception:
            pass
        os.chdir(previous_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _default_config():
    from pydantic import SecretStr

    from kimi_cli.config import SearchConfig, get_default_config

    conf = get_default_config()
    try:
        conf.services.search = SearchConfig(
            base_url="https://api.kimi.com/coding/v1/search",
            api_key=SecretStr("test-api-key"),
        )
    except Exception:
        pass
    return conf


def build_runtime(tmpdir: Path, work: Path):
    """Build a Runtime equivalent to the ``runtime`` fixture in tests/conftest.py."""
    from kaos import get_current_kaos
    from kaos.path import KaosPath
    from kosong.chat_provider.mock import MockChatProvider

    from kimi_cli.auth.oauth import OAuthManager
    from kimi_cli.background import BackgroundTaskManager
    from kimi_cli.llm import ALL_MODEL_CAPABILITIES, LLM
    from kimi_cli.metadata import WorkDirMeta
    from kimi_cli.notifications import NotificationManager
    from kimi_cli.session import Session
    from kimi_cli.session_state import SessionState
    from kimi_cli.soul.agent import BuiltinSystemPromptArgs, LaborMarket, Runtime
    from kimi_cli.soul.approval import Approval
    from kimi_cli.soul.denwarenji import DenwaRenji
    from kimi_cli.utils.environment import Environment
    from kimi_cli.wire.file import WireFile

    config = _default_config()
    kaos_path = KaosPath.unsafe_from_local_path(work)
    session = Session(
        id="membench",
        work_dir=kaos_path,
        work_dir_meta=WorkDirMeta(path=str(work), kaos=get_current_kaos().name),
        context_file=work / "context.jsonl",
        wire_file=WireFile(path=work / "wire.jsonl"),
        state=SessionState(),
        title="mem-bench",
        updated_at=0.0,
        custom_data={},
        custom_config={},
        sessions_dir_override=tmpdir / "sessions",
    )
    notifications = NotificationManager(work / "notifications", config.notifications)
    environment = Environment(
        os_kind="Windows" if sys.platform == "win32" else "Unix",
        os_arch="x86_64",
        os_version="1.0",
        shell_name="pwsh" if sys.platform == "win32" else "bash",
        shell_path=KaosPath(
            r"C:\Program Files\Git\bin\bash.exe"
            if sys.platform == "win32"
            else "/bin/bash"
        ),
    )
    llm = LLM(
        chat_provider=MockChatProvider([]),
        max_context_size=100_000,
        capabilities=ALL_MODEL_CAPABILITIES,
    )
    runtime = Runtime(
        config=config,
        oauth=OAuthManager(config),
        llm=llm,
        session=session,
        builtin_args=BuiltinSystemPromptArgs(
            KIMI_WORK_DIR=kaos_path,
            KIMI_SKILLS="No skills found.",
            KIMI_OS=environment.os_kind,
        ),
        denwa_renji=DenwaRenji(),
        approval=Approval(yolo=True),
        labor_market=LaborMarket(),
        environment=environment,
        notifications=notifications,
        background_tasks=BackgroundTaskManager(
            session, config.background, notifications=notifications
        ),
        skills={},
        additional_dirs=[],
        skills_dirs=[],
        role="root",
    )
    return runtime, config, session


DEFAULT_AGENT_TOOLS: list[str] = []
"""Filled lazily from ``kimi-cli/src/kimi_cli/agents/default/agent.yaml``."""


def default_agent_tool_paths() -> list[str]:
    """Tool paths from the shipped default agent manifest (the real registry)."""
    global DEFAULT_AGENT_TOOLS
    if DEFAULT_AGENT_TOOLS:
        return list(DEFAULT_AGENT_TOOLS)
    manifest = KIMI_SRC / "kimi_cli" / "agents" / "default" / "agent.yaml"
    paths: list[str] = []
    try:
        text = manifest.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('- "') and ":" in stripped:
                candidate = stripped[2:].strip().strip('"').strip("'")
                if candidate.startswith("kimi_cli.tools."):
                    paths.append(candidate)
    except OSError:
        paths = []
    DEFAULT_AGENT_TOOLS = paths
    return list(paths)


def build_toolset(runtime, config, session):
    """Full builtin toolset, mirroring ``load_agent`` in soul/agent.py."""
    from kimi_cli.soul.agent import BuiltinSystemPromptArgs, LaborMarket, Runtime
    from kimi_cli.soul.approval import Approval
    from kimi_cli.soul.denwarenji import DenwaRenji
    from kimi_cli.soul.toolset import KimiToolset
    from kimi_cli.utils.environment import Environment
    from kimi_cli.vfs import VFS

    from kimi_cli.config import Config
    from kimi_cli.session import Session

    toolset = KimiToolset(runtime)
    deps: dict[type[Any], Any] = {
        KimiToolset: toolset,
        Runtime: runtime,
        Config: config,
        BuiltinSystemPromptArgs: runtime.builtin_args,
        Session: session,
        DenwaRenji: runtime.denwa_renji,
        Approval: runtime.approval,
        LaborMarket: runtime.labor_market,
        Environment: runtime.environment,
        VFS: None,
    }
    loaded: list[str] = []
    failed: list[dict[str, str]] = []
    for path in default_agent_tool_paths():
        try:
            toolset.load_tools([path], deps)
            loaded.append(path)
        except Exception as exc:  # noqa: BLE001 - report, never abort
            failed.append({"tool": path, "error": f"{type(exc).__name__}: {exc}"})
    return toolset, loaded, failed


# --------------------------------------------------------------------------- #
# Message factories (shared by agent_loop / session / pruning / compaction)
# --------------------------------------------------------------------------- #

_LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. "
)


def _lorem(n_chars: int) -> str:
    reps = max(1, n_chars // len(_LOREM))
    return (_LOREM * reps)[:n_chars]


def make_tool_output_text(n_chars: int, *, lineno: bool = True, label: str = "") -> str:
    """ReadFile-ish tool output: numbered lines (what a real `read` returns).

    ``label`` is mixed into every line so synthetic outputs are *unique* per call
    (otherwise a duplicate-string analysis would only measure this generator).
    """
    if not lineno:
        return f"[{label}] " + _lorem(n_chars)
    reps = max(1, n_chars // 82)
    out_lines = []
    for i in range(reps):
        out_lines.append(f"{i + 1:>6}\t" + f"[{label}] " + _lorem(64))
    return "\n".join(out_lines)


def make_history(n_turns: int = 50, *, tools_per_turn: int = 2, output_chars: int = 3000):
    """Build a realistic agent history: user, assistant(tool calls), tool results."""
    from kosong.message import Message
    from kosong.message import TextPart as KosongTextPart
    from kosong.message import ThinkPart, ToolCall

    from kimi_cli.soul.message import tool_result_to_message
    from kosong.tooling import ToolOk, ToolResult

    history: list[Message] = []
    call_ids: list[str] = []
    for turn in range(n_turns):
        history.append(
            Message(
                role="user",
                content=[KosongTextPart(text=f"Turn {turn}: please inspect module {turn}")],
            )
        )
        calls = [
            ToolCall(
                id=f"call_{turn}_{k}",
                function=ToolCall.FunctionBody(
                    name="read",
                    arguments=json.dumps(
                        {"path": f"src/pkg/mod_{turn}_{k}.py", "line_offset": 1, "n_lines": 400}
                    ),
                ),
            )
            for k in range(tools_per_turn)
        ]
        history.append(
            Message(
                role="assistant",
                content=[
                    ThinkPart(think=f"[turn {turn}] " + _lorem(400)),
                    KosongTextPart(text=f"I will read {tools_per_turn} files."),
                ],
                tool_calls=calls,
            )
        )
        for k, call in enumerate(calls):
            tool_result = ToolResult(
                tool_call_id=call.id,
                return_value=ToolOk(
                    output=KosongTextPart(
                        text=make_tool_output_text(
                            output_chars + 400 * k, label=f"t{turn}s{k}"
                        )
                    ),
                    message=f"Read {output_chars // 80} lines from src/pkg/mod_{turn}_{k}.py",
                    brief="Read file",
                ),
            )
            history.append(tool_result_to_message(tool_result))
            call_ids.append(call.id)
    return history, call_ids


def duplicate_text_report(objects: Sequence[Any], *, min_len: int = 256) -> dict[str, Any]:
    """Count identical string values held by distinct objects (real copies)."""
    seen: dict[str, int] = {}
    copies_wasted = 0
    total = 0
    for text in _iter_strings(objects):
        if len(text) < min_len:
            continue
        total += 1
        previous = seen.get(text)
        if previous is None:
            seen[text] = len(text)
        else:
            copies_wasted += len(text)
    return {
        "long_strings": total,
        "distinct_values": len(seen),
        "duplicate_copies": total - len(seen),
        "wasted_bytes_estimate": copies_wasted,
    }


def _iter_strings(obj: Any, seen: set[int] | None = None, depth: int = 0):
    if seen is None:
        seen = set()
    if depth > 40:
        return
    oid = id(obj)
    if oid in seen:
        return
    seen.add(oid)
    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, bytes):
        return
    if isinstance(obj, (list, tuple, set, frozenset, deque)):
        for item in obj:
            yield from _iter_strings(item, seen, depth + 1)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _iter_strings(key, seen, depth + 1)
            yield from _iter_strings(value, seen, depth + 1)
        return
    if isinstance(obj, _NEVER_TRAVERSE):
        return
    namespace = getattr(obj, "__dict__", None)
    if isinstance(namespace, dict):
        yield from _iter_strings(namespace, seen, depth + 1)
    for value in _slot_values(obj):
        yield from _iter_strings(value, seen, depth + 1)


# --------------------------------------------------------------------------- #
# Preload hooks: import modules *before* the measurement window
# --------------------------------------------------------------------------- #

def _preload_toolset_registry() -> None:
    ensure_import_paths()
    importlib.import_module("kimi_cli.soul.toolset")
    importlib.import_module("kimi_cli.soul.agent")
    for path in default_agent_tool_paths():
        module = path.rsplit(":", 1)[0]
        try:
            importlib.import_module(module)
        except Exception:
            continue


def _preload_soul_runtime() -> None:
    ensure_import_paths()
    for module in (
        "kosong.message",
        "kosong.tooling",
        "kosong.chat_provider.mock",
        "kimi_cli.soul.message",
        "kimi_cli.soul.toolset",
        "kimi_cli.soul.llm_request_recorder",
        "kimi_cli.session",
        "kimi_cli.soul.context_db",
    ):
        importlib.import_module(module)


def _preload_context_pruning() -> None:
    ensure_import_paths()
    _preload_soul_runtime()
    importlib.import_module("kimi_cli.soul.context_pruning")


def _preload_compaction() -> None:
    ensure_import_paths()
    _preload_soul_runtime()
    for module in (
        "kimi_cli.soul.compaction",
        "kimi_cli.soul.compaction_ledger",
        "kimi_cli.soul.tool_pairing",
        "kimi_cli.llm",
    ):
        importlib.import_module(module)


def _preload_history_index() -> None:
    ensure_import_paths()
    importlib.import_module("kimi_cli.soul.history_index")


def _preload_message_types() -> None:
    ensure_import_paths()
    for module in (
        "kosong.message",
        "kosong.tooling",
        "kimi_cli.tools.display",
        "kimi_cli.session_state",
    ):
        importlib.import_module(module)


# --------------------------------------------------------------------------- #
# Case 1 — import_cli
# --------------------------------------------------------------------------- #

IMPORT_TARGETS: list[str] = [
    "kimi_cli.cli",
    "kimi_cli.app",
    "kimix.cli_impl.main",
    "kimix.cli_impl.core",
    "kimix.cli_impl.commands",
    "kimix.cli_impl.args",
    "kimix.cli_impl.constants",
    "kimix.cli_impl.ipc",
    "kimix.cli_impl.mcp_cmd",
    "kimix.cli_impl.sse_cli",
    "kimix.cli_impl.utils",
    "kimix.cli_impl.gui_cmd",
    "kimix.cli_impl.init",
    "typer",
    "rich",
    "rich.console",
    "rich.markdown",
    "rich.syntax",
    "aiohttp",
    "anthropic",
    "openai",
    "google.genai",
    "google.genai.types",
    "fastmcp",
    "mcp",
    "trafilatura",
    "tiktoken",
    "numpy",
    "PIL",
    "PIL.Image",
    "pymupdf",
    "pydantic",
    "kosong",
    "kimi_agent_sdk",
    "jinja2",
    "playwright",
    "httpx",
    "apsw",
]


def import_probe(module: str) -> dict[str, Any]:
    """Child-process entry point: measure the RSS cost of importing *module*."""
    gc.collect()
    rss0 = rss_bytes()
    modules_before = len(sys.modules)
    started = time.perf_counter()
    error = None
    try:
        if module:
            importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - a missing module is data, not a crash
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.perf_counter() - started) * 1000
    gc.collect()
    rss1 = rss_bytes()
    return {
        "module": module or "<baseline>",
        "rss_before_bytes": rss0,
        "rss_after_bytes": rss1,
        "rss_delta_bytes": rss1 - rss0,
        "modules_imported": len(sys.modules) - modules_before,
        "duration_ms": round(duration_ms, 2),
        "error": error,
    }


def native_probe() -> dict[str, Any]:
    """Child-process entry point: footprint of the optional kimix_native bridge."""
    ensure_import_paths()
    gc.collect()
    rss0 = rss_bytes()
    modules_before = len(sys.modules)
    started = time.perf_counter()
    report: dict[str, Any] = {}
    try:
        import kimi_cli.native_loader as native

        import_ms = (time.perf_counter() - started) * 1000
        rss_import = rss_bytes()
        codec = native.get_compat("codec")
        report = {
            "version": native.version(),
            "codec_module": getattr(codec, "__name__", str(type(codec))),
            "import_ms": round(import_ms, 2),
            "load_ms": round((time.perf_counter() - started) * 1000 - import_ms, 2),
            "rss_after_import_bytes": rss_import - rss0,
            "rss_after_load_bytes": rss_bytes() - rss0,
            "modules_imported": len(sys.modules) - modules_before,
            "kernels_native": {
                kernel: native.use_native(kernel)
                for kernel in ("TEXT", "INDEX", "SEARCH", "PARSE", "TOOLS", "STREAM", "CODEC")
            },
            "shim_modules": sorted(
                name for name in sys.modules if name.startswith("kimix_native")
            )[:20],
        }
    except Exception as exc:  # noqa: BLE001
        report = {"error": f"{type(exc).__name__}: {exc}"}
    return report


def _run_probe(module: str, timeout: float = 180.0) -> dict[str, Any]:
    """Spawn a fresh interpreter and run ``--import-probe`` inside it."""
    import subprocess

    script = str(Path(__file__).resolve())
    with tempfile.TemporaryDirectory(prefix="membench_probe_") as tmp:
        out = Path(tmp) / "probe.json"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(KIMIX_SRC), str(KIMI_SRC), str(ROOT), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        cmd = [sys.executable, script, "--import-probe", module, "--json-out", str(out)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {"module": module or "<baseline>", "error": "timeout"}
        if not out.exists():
            return {
                "module": module or "<baseline>",
                "error": (proc.stderr or "probe produced no output").strip()[-400:],
            }
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            return {"module": module or "<baseline>", "error": f"bad probe json: {exc}"}


def _run_native_probe() -> dict[str, Any]:
    import subprocess

    script = str(Path(__file__).resolve())
    with tempfile.TemporaryDirectory(prefix="membench_native_") as tmp:
        out = Path(tmp) / "probe.json"
        cmd = [sys.executable, script, "--native-probe", "--json-out", str(out)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=180,
                env=_child_env(),
            )
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
        if not out.exists():
            return {"error": (proc.stderr or "no output").strip()[-300:]}
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": f"bad probe json: {exc}"}


@case(
    "import_cli",
    "import footprint of kimi_cli.cli / kimix CLI / heavy leaf modules",
    frames=4,
)
def case_import_cli() -> dict[str, Any]:
    ensure_import_paths()
    baseline = _run_probe("")
    base_rss = baseline.get("rss_after_bytes", 0)
    native_report = _run_native_probe()

    isolated: list[dict[str, Any]] = []
    for module in IMPORT_TARGETS:
        probe = _run_probe(module)
        if "error" in probe and probe.get("error"):
            isolated.append(
                {
                    "module": module,
                    "isolated_rss_bytes": None,
                    "duration_ms": probe.get("duration_ms"),
                    "error": probe["error"],
                }
            )
            continue
        isolated.append(
            {
                "module": module,
                "isolated_rss_bytes": max(0, probe.get("rss_after_bytes", 0) - base_rss),
                "isolated_own_delta_bytes": probe.get("rss_delta_bytes"),
                "modules_imported": probe.get("modules_imported"),
                "duration_ms": probe.get("duration_ms"),
                "error": None,
            }
        )
    isolated.sort(key=lambda row: row.get("isolated_rss_bytes") or -1, reverse=True)

    # --- incremental in-process pass (marginal cost + module attribution) ---
    incremental: list[dict[str, Any]] = []
    cli_attribution: dict[str, Any] = {}
    base_snapshot = tracemalloc.take_snapshot()
    for module in IMPORT_TARGETS:
        before_rss = rss_bytes()
        before_modules = set(sys.modules)
        before_traced = tracemalloc.get_traced_memory()[0]
        started = time.perf_counter()
        error = None
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started) * 1000
        gc.collect()
        traced_delta = tracemalloc.get_traced_memory()[0] - before_traced
        delta_rss = rss_bytes() - before_rss
        new_modules = sorted(set(sys.modules) - before_modules)
        incremental.append(
            {
                "module": module,
                "rss_delta_bytes": delta_rss,
                "rss_delta_mib": _mib(delta_rss),
                "traced_delta_bytes": traced_delta,
                "modules_imported": len(new_modules),
                "duration_ms": round(duration_ms, 2),
                "error": error,
            }
        )
        if module in ("kimi_cli.cli", "kimix.cli_impl.main") and not cli_attribution:
            cli_attribution[module] = {
                "new_modules": len(new_modules),
                "new_module_names": new_modules[:400],
            }
    incremental.sort(
        key=lambda row: max(row["traced_delta_bytes"], row["rss_delta_bytes"]), reverse=True
    )
    total_attribution = module_attribution(base_snapshot, 25)
    for name, entry in cli_attribution.items():
        entry["attribution_note"] = "see metrics.total_module_attribution"
        del name

    metrics = {
        "baseline_rss_bytes": base_rss,
        "baseline_modules": baseline.get("modules_imported"),
        "isolated": isolated,
        "incremental": incremental,
        "isolated_total_rss_bytes": sum(
            row.get("isolated_rss_bytes") or 0 for row in isolated
        ),
        "total_module_attribution": total_attribution,
        "native_bridge": native_report,
        "total_traced_bytes": tracemalloc.get_traced_memory()[0],
        "total_rss_bytes": rss_bytes(),
        "modules_loaded": len(sys.modules),
    }
    return {
        "metrics": metrics,
        "tables": {"kimi_cli_cli_attribution": cli_attribution},
        "notes": [
            "isolated = fresh subprocess per module; footprint is RSS(after import) - RSS(baseline probe).",
            "incremental = same-process marginal cost in the listed order (greedy attribution).",
            "baseline probe RSS includes this harness's own imports (argparse/json/tracemalloc/...).",
        ],
    }


# --------------------------------------------------------------------------- #
# Case 2 — import_tools
# --------------------------------------------------------------------------- #

def _tool_module_names() -> list[str]:
    import kimi_cli.tools as tools_pkg

    names = ["kimi_cli.tools"]
    for info in pkgutil.walk_packages(tools_pkg.__path__, "kimi_cli.tools."):
        names.append(info.name)
    return sorted(set(names))


def _module_constant_payloads(
    module_name: str, seen_ids: set[int], min_bytes: int = 1024
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Large module-level constants of one module.

    Objects already attributed to another module (the same dict imported into
    many namespaces) are reported separately as ``aliased`` instead of being
    counted twice.
    """
    module = sys.modules.get(module_name)
    if module is None:
        return [], []
    rows: list[dict[str, Any]] = []
    aliased: list[dict[str, Any]] = []
    for name, value in list(vars(module).items()):
        if name.startswith("__"):
            continue
        if isinstance(value, _NEVER_TRAVERSE):
            continue
        if not isinstance(value, (dict, list, tuple, str, bytes, set, frozenset)):
            continue
        try:
            shallow = sys.getsizeof(value)
        except Exception:
            continue
        if shallow < min_bytes:
            continue
        if id(value) in seen_ids:
            aliased.append({"module": module_name, "name": name})
            continue
        seen_ids.add(id(value))
        rows.append(
            {
                "module": module_name,
                "name": name,
                "type": type(value).__name__,
                "shallow_bytes": shallow,
                "deep_bytes": deep_size(value),
                "len": len(value) if hasattr(value, "__len__") else None,
            }
        )
    return rows, aliased


@case(
    "import_tools",
    "cost of importing kimi_cli.tools + module-level constant payloads",
    frames=4,
)
def case_import_tools() -> dict[str, Any]:
    ensure_import_paths()
    module_names = _tool_module_names()
    rows: list[dict[str, Any]] = []
    base_snapshot = tracemalloc.take_snapshot()
    base_rss = rss_bytes()
    traced_start = tracemalloc.get_traced_memory()[0]
    for module_name in module_names:
        before_modules = set(sys.modules)
        before_rss = rss_bytes()
        before_traced = tracemalloc.get_traced_memory()[0]
        started = time.perf_counter()
        error = None
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started) * 1000
        gc.collect()
        rows.append(
            {
                "module": module_name,
                "rss_delta_bytes": rss_bytes() - before_rss,
                "traced_delta_bytes": tracemalloc.get_traced_memory()[0] - before_traced,
                "modules_imported": len(set(sys.modules) - before_modules),
                "duration_ms": round(duration_ms, 2),
                "error": error,
            }
        )
    rows.sort(key=lambda row: row["traced_delta_bytes"], reverse=True)
    attribution = module_attribution(base_snapshot, 25)

    constants: list[dict[str, Any]] = []
    aliased_constants: list[dict[str, Any]] = []
    seen_constant_ids: set[int] = set()
    for module_name in module_names + [
        "kimi_cli.soul.toolset",
        "kimi_cli.prompts",
        "kimi_cli.soul.compaction",
        "kimi_cli.soul.context_pruning",
        "kimi_cli.soul.error_log",
    ]:
        rows_c, aliased_c = _module_constant_payloads(module_name, seen_constant_ids)
        constants.extend(rows_c)
        aliased_constants.extend(aliased_c)
    constants.sort(key=lambda row: row["deep_bytes"], reverse=True)

    total_traced = tracemalloc.get_traced_memory()[0] - traced_start
    metrics = {
        "module_count": len(module_names),
        "total_traced_bytes": total_traced,
        "total_rss_delta_bytes": rss_bytes() - base_rss,
        "total_rss_delta_mib": _mib(rss_bytes() - base_rss),
        "module_attribution": attribution,
        "total_traced_mib": _mib(total_traced),
        "modules": rows,
        "module_constants_top": constants[:30],
        "module_constants_total_bytes": sum(row["deep_bytes"] for row in constants),
        "module_constants_count": len(constants),
        "module_constants_aliased": aliased_constants,
        "module_constants_aliased_count": len(aliased_constants),
        "toolset_redirect_table_bytes": _redirect_table_footprint(),
    }
    return {
        "metrics": metrics,
        "notes": [
            "traced_delta = Python-heap bytes attributed to files imported by this module.",
            "module_constants_top = biggest module-level dict/list/str/bytes literals (>1 KiB shallow).",
        ],
    }


def _redirect_table_footprint() -> dict[str, Any]:
    """Size of the tool-name redirect tables that ship in toolset/kosong."""
    out: dict[str, Any] = {}
    try:
        from kosong.tooling import TOOL_NAME_REDIRECTS

        out["kosong.tooling.TOOL_NAME_REDIRECTS"] = {
            "entries": len(TOOL_NAME_REDIRECTS),
            "deep_bytes": deep_size(TOOL_NAME_REDIRECTS),
        }
    except Exception as exc:
        out["kosong.tooling.TOOL_NAME_REDIRECTS"] = {"error": str(exc)}
    try:
        import kimi_cli.tools as tools_pkg

        retired = getattr(tools_pkg, "RETIRED_TODO_TOOL_NAMES", None)
        if retired is not None:
            out["kimi_cli.tools.RETIRED_TODO_TOOL_NAMES"] = {
                "entries": len(retired),
                "deep_bytes": deep_size(retired),
            }
    except Exception as exc:
        out["kimi_cli.tools.RETIRED_TODO_TOOL_NAMES"] = {"error": str(exc)}
    return out


# --------------------------------------------------------------------------- #
# Case 3 — toolset_registry
# --------------------------------------------------------------------------- #

@case(
    "toolset_registry",
    "full KimiToolset registry: retained bytes + per-tool JSON schema",
    preload=_preload_toolset_registry,
)
def case_toolset_registry() -> dict[str, Any]:
    ensure_import_paths()
    import orjson

    with temp_env("membench_toolset_") as (tmpdir, work):
        runtime, config, session = build_runtime(tmpdir, work)
        toolset, loaded, failed = build_toolset(runtime, config, session)
        tools = list(toolset._tool_dict.values())
        keep = [runtime, config, session, toolset, tools]

        seen: set[int] = set()
        per_tool: list[dict[str, Any]] = []
        total_schema = 0
        total_desc = 0
        for name, tool in toolset._tool_dict.items():
            base = tool.base
            schema = orjson.dumps(base.parameters) if base.parameters is not None else b"{}"
            description = base.description or ""
            total_schema += len(schema)
            total_desc += len(description.encode("utf-8"))
            per_tool.append(
                {
                    "name": name,
                    "class": f"{type(tool).__module__}.{type(tool).__qualname__}",
                    "marginal_deep_bytes": deep_size(tool, seen),
                    "schema_bytes": len(schema),
                    "description_bytes": len(description.encode("utf-8")),
                    "params_model": getattr(getattr(tool, "params", None), "__qualname__", None),
                }
            )
        per_tool.sort(key=lambda row: row["marginal_deep_bytes"], reverse=True)
        tool_dict_bytes = deep_size(toolset._tool_dict)

        llm_payload = 0
        for tool in tools:
            base = tool.base
            payload = {
                "name": base.name,
                "description": base.description,
                "parameters": base.parameters,
            }
            llm_payload += len(orjson.dumps(payload))

        hook_engine_bytes = deep_size(toolset._hook_engine)
        runtime_bytes = deep_size(runtime)

        schema_snapshot = list(toolset.tools)  # property materialises a list each call
        keep.extend([schema_snapshot])

        metrics = {
            "registered_tools": len(toolset._tool_dict),
            "manifest_tools": len(default_agent_tool_paths()),
            "loaded": loaded,
            "failed": failed,
            "tool_dict_deep_bytes": tool_dict_bytes,
            "tools_marginal_total_bytes": sum(row["marginal_deep_bytes"] for row in per_tool),
            "per_tool": per_tool,
            "schema_total_bytes": total_schema,
            "description_total_bytes": total_desc,
            "llm_payload_total_bytes": llm_payload,
            "llm_payload_avg_bytes": round(llm_payload / max(1, len(tools)), 1),
            "hook_engine_bytes": hook_engine_bytes,
            "runtime_deep_bytes": runtime_bytes,
            "tool_list_materialisation_bytes": sys.getsizeof(schema_snapshot),
            "class_slots": _class_layout_report(
                [type(tool) for _, tool in toolset._tool_dict.items()]
            ),
        }
    return {
        "metrics": metrics,
        "keep": keep,
        "notes": [
            "marginal_deep_bytes uses one shared visited set: shared deps (runtime/session) "
            "are attributed to the first tool that reaches them.",
            "llm_payload_total_bytes is the JSON actually shipped in the tools array.",
        ],
    }


def _class_layout_report(classes: Sequence[type]) -> list[dict[str, Any]]:
    rows = []
    seen: set[type] = set()
    for cls in classes:
        for klass in cls.__mro__:
            if klass in seen or klass is object:
                continue
            seen.add(klass)
            slots = klass.__dict__.get("__slots__")
            model_config = getattr(klass, "model_config", None)
            rows.append(
                {
                    "class": f"{klass.__module__}.{klass.__qualname__}",
                    "slots": slots if isinstance(slots, (list, tuple)) else bool(slots),
                    "pydantic": model_config is not None,
                    "model_config": _config_summary(model_config) if model_config else None,
                }
            )
    return rows


def _config_summary(model_config: Any) -> dict[str, Any]:
    if not isinstance(model_config, dict):
        return {}
    keep = {
        "extra",
        "frozen",
        "defer_build",
        "strict",
        "revalidate_instances",
        "validate_assignment",
        "arbitrary_types_allowed",
        "populate_by_name",
    }
    return {key: value for key, value in model_config.items() if key in keep}


# --------------------------------------------------------------------------- #
# Case 4 — agent_loop
# --------------------------------------------------------------------------- #

@case(
    "agent_loop",
    "50-turn agent loop: retained growth per turn + unbounded bookkeeping",
    preload=_preload_soul_runtime,
    frames=6,
)
def case_agent_loop() -> dict[str, Any]:
    ensure_import_paths()
    import orjson
    from kosong.chat_provider.mock import MockChatProvider
    from kosong.message import Message, TextPart as KosongTextPart
    from kosong.message import ThinkPart, ToolCall
    from kosong.tooling import Tool, ToolOk, ToolResult

    from kimi_cli.soul.llm_request_recorder import LLMRequestRecorder
    from kimi_cli.soul.message import normalize_history, tool_result_to_message
    from kimi_cli.soul.toolset import KimiToolset
    from kimi_cli.soul.toolset import _normalize_call_key

    turns = 50
    steps_per_turn = 5
    calls_per_step = 2

    toolset = KimiToolset(None)
    recorder = LLMRequestRecorder()
    chat_provider = MockChatProvider([])
    tools = [
        Tool(
            name="read",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    history: list[Message] = []
    per_turn: list[dict[str, Any]] = []
    seen_sizes: list[int] = []
    cycle_sizes: list[int] = []
    recorder_failures = 0
    normalize_peaks: list[int] = []

    def build_step(turn: int, step: int) -> list[ToolCall]:
        return [
            ToolCall(
                id=f"call_{turn}_{step}_{k}",
                function=ToolCall.FunctionBody(
                    name="read",
                    arguments=orjson.dumps(
                        {"path": f"src/t{turn}/s{step}_{k}.py", "n_lines": 400}
                    ).decode(),
                ),
            )
            for k in range(calls_per_step)
        ]

    async def drive() -> None:
        nonlocal previous_calls, recorder_failures
        from kimi_cli.soul import _current_wire
        from kimi_cli.wire import Wire

        token = _current_wire.set(Wire(file_backend=None))
        try:
            for turn in range(turns):
                if turn:
                    toolset.begin_step([], step_no=0, turn_id=f"turn-{turn}")
                for step in range(steps_per_turn):
                    calls = build_step(turn, step)
                    toolset.begin_step(previous_calls, step_no=step, turn_id=f"turn-{turn}")
                    history.append(
                        Message(
                            role="assistant",
                            content=[
                                ThinkPart(think=f"[{turn}/{step}] " + _lorem(500)),
                                KosongTextPart(
                                    text=f"Reading {calls_per_step} files (step {step})."
                                ),
                            ],
                            tool_calls=calls,
                        )
                    )
                    for k, call in enumerate(calls):
                        history.append(
                            tool_result_to_message(
                                ToolResult(
                                    tool_call_id=call.id,
                                    return_value=ToolOk(
                                        output=KosongTextPart(
                                            text=make_tool_output_text(
                                                3000 + 250 * k, label=f"t{turn}s{step}k{k}"
                                            )
                                        ),
                                        message=(
                                            f"Read 300 lines from src/t{turn}/s{step}_{k}.py"
                                        ),
                                        brief="Read file",
                                    ),
                                )
                            )
                        )
                    for call in calls:
                        key = _normalize_call_key(call.function.name, call.function.arguments)
                        toolset._current_step_calls.append(key)
                        toolset._call_key_counts[key] = (
                            toolset._call_key_counts.get(key, 0) + 1
                        )
                        toolset._seen_call_keys.add(key)
                    previous_calls = toolset.end_step()
                    seen_sizes.append(len(toolset._seen_call_keys))
                    cycle_sizes.append(len(toolset._call_key_counts))
                    try:
                        recorder.record(
                            chat_provider,
                            "system prompt for the loop",
                            tools,
                            history,
                            kind="loop",
                            turn_step=step,
                        )
                    except Exception:  # noqa: BLE001
                        recorder_failures += 1
                    await asyncio.sleep(0)
                # one normalize_history pass per turn (LLM request assembly path)
                snapshot, phase = _measured(lambda: normalize_history(history))
                normalize_peaks.append(phase["peak_bytes"])
                del snapshot
                per_turn.append(
                    {
                        "turn": turn,
                        "messages": len(history),
                        "traced_bytes": tracemalloc.get_traced_memory()[0],
                        "history_deep_bytes": deep_size(history),
                        "seen_call_keys": len(toolset._seen_call_keys),
                        "call_key_counts": len(toolset._call_key_counts),
                        "tool_warned_at": sum(len(v) for v in toolset._tool_warned_at.values()),
                    }
                )
        finally:
            _current_wire.reset(token)

    previous_calls: list[tuple[str, str]] = []
    asyncio.run(drive())

    # --- unbounded / accumulator bookkeeping ---
    wire_queue_report = _wire_queue_retention(turns * steps_per_turn)
    steer_report = _steer_queue_retention()
    accumulator = {
        "history_messages": len(history),
        "history_deep_bytes": deep_size(history),
        "toolset_seen_call_keys_bytes": deep_size(toolset._seen_call_keys),
        "toolset_seen_call_keys_max": max(seen_sizes) if seen_sizes else 0,
        "toolset_call_key_counts_bytes": deep_size(toolset._call_key_counts),
        "toolset_call_key_counts_max": max(cycle_sizes) if cycle_sizes else 0,
        "toolset_previous_step_calls_bytes": deep_size(toolset._previous_step_calls),
        "toolset_tool_call_counts_bytes": deep_size(toolset._tool_call_counts),
        "toolset_tool_warned_at_bytes": deep_size(toolset._tool_warned_at),
        "recorder_seen_tools_hashes": len(recorder._seen_tools_hashes),
        "recorder_seen_prompt_hashes": len(recorder._seen_prompt_hashes),
        "recorder_state_bytes": deep_size(recorder),
        "recorder_failures": recorder_failures,
    }

    first, last = per_turn[0], per_turn[-1]
    growth = last["traced_bytes"] - first["traced_bytes"]
    metrics = {
        "turns": turns,
        "steps_per_turn": steps_per_turn,
        "calls_per_step": calls_per_step,
        "messages": len(history),
        "traced_first_turn_bytes": first["traced_bytes"],
        "traced_last_turn_bytes": last["traced_bytes"],
        "retained_growth_bytes": growth,
        "retained_growth_per_turn_bytes": round(growth / max(1, turns - 1), 1),
        "history_deep_bytes": deep_size(history),
        "history_bytes_per_message": round(deep_size(history) / max(1, len(history)), 1),
        "normalize_history_peak_bytes_avg": round(sum(normalize_peaks) / len(normalize_peaks), 1),
        "normalize_history_peak_bytes_max": max(normalize_peaks) if normalize_peaks else 0,
        "accumulators": accumulator,
        "wire_queue": wire_queue_report,
        "steer_queue": steer_report,
        "per_turn": per_turn,
        "duplicate_text": duplicate_text_report(history),
    }
    return {
        "metrics": metrics,
        "keep": [history, toolset, recorder, tools],
        "notes": [
            "Uses the real tool_result_to_message / normalize_history / KimiToolset.begin_step paths.",
            "Recorder runs inside an event loop with an attached no-op Wire.",
        ],
    }


def _wire_queue_retention(messages: int) -> dict[str, Any]:
    """Bytes retained by an undrained Wire subscriber queue (UI not consuming)."""
    try:
        from kimi_cli.wire import Wire
        from kimi_cli.wire.types import StepBegin, TextPart

        wire = Wire(file_backend=None)
        subscriber = wire.ui_side(merge=True)
        payload = "x" * 512
        base = deep_size(subscriber)
        for i in range(messages):
            wire.soul_side.send(StepBegin(n=i))
            wire.soul_side.send(TextPart(text=payload))
        after = deep_size(subscriber)
        depth = len(subscriber._ui_queue._queue) if hasattr(subscriber, "_ui_queue") else None
        return {
            "messages_sent": messages * 2,
            "retained_bytes": after - base,
            "queue_depth": depth,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _steer_queue_retention() -> dict[str, Any]:
    """Bytes retained by an unconsumed asyncio steer queue."""
    try:
        from kosong.message import TextPart as KosongTextPart

        queue: asyncio.Queue[Any] = asyncio.Queue()
        base = deep_size(queue)
        for i in range(50):
            queue.put_nowait([KosongTextPart(text="steer " + _lorem(400))])
        return {
            "items": queue.qsize(),
            "retained_bytes": deep_size(queue) - base,
            "bounded": getattr(queue, "maxsize", 0),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# Case 5 — session_context
# --------------------------------------------------------------------------- #

@case(
    "session_context",
    "Session + ContextDB: 500 messages written, read back, measured",
    preload=_preload_soul_runtime,
)
def case_session_context() -> dict[str, Any]:
    ensure_import_paths()
    from kosong.message import Message, TextPart as KosongTextPart

    from kimi_cli.soul.context_db import ContextDB

    messages_total = 500
    batch = 10

    with temp_env("membench_session_") as (tmpdir, work):
        runtime, config, session = build_runtime(tmpdir, work)
        db_path = tmpdir / "session" / "context.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = ContextDB(db_path)
        history, _ = make_history(n_turns=messages_total // 4, output_chars=2500)

        async def workload() -> dict[str, Any]:
            await db.initialize()
            await db.set_system_prompt("You are Kimi.")
            write_ms: list[float] = []
            for start in range(0, messages_total, batch):
                chunk = history[start : start + batch]
                if not chunk:
                    break
                started = time.perf_counter()
                await db.append_messages(chunk)
                write_ms.append((time.perf_counter() - started) * 1000)
            written = await db.get_message_count()
            read_started = time.perf_counter()
            loaded = await db.get_messages()
            read_ms = (time.perf_counter() - read_started) * 1000
            paged = await db.get_messages(limit=50)
            per_message = [deep_size(message) for message in loaded[:50]]
            first_payload = deep_size(loaded[0]) if loaded else 0
            return {
                "written": written,
                "loaded": len(loaded),
                "read_ms": round(read_ms, 2),
                "write_ms_avg": round(sum(write_ms) / max(1, len(write_ms)), 3),
                "write_ms_max": round(max(write_ms) if write_ms else 0.0, 3),
                "loaded_deep_bytes": deep_size(loaded),
                "paged_50_deep_bytes": deep_size(paged),
                "per_message_deep_bytes_avg": round(
                    sum(per_message) / max(1, len(per_message)), 1
                ),
                "per_message_deep_bytes_first": first_payload,
                "loads_entire_history": len(loaded) == written,
                "text_identity_preserved": _text_identity_ratio(loaded),
                "loaded_keep": loaded,
            }

        result = asyncio.run(workload())
        loaded = result.pop("loaded_keep")
        disk = _sqlite_disk_report(db_path)
        method_costs = _measure_methods()
        metrics = {
            **result,
            "sqlite": disk,
            "session_deep_bytes": deep_size(session),
            "runtime_deep_bytes": deep_size(runtime),
            "db_object_deep_bytes": deep_size(db),
            "method_costs": method_costs,
        }
        asyncio.run(db.close())
    return {
        "metrics": metrics,
        "keep": [loaded, db],
        "notes": [
            "get_messages() re-parses every row into fresh pydantic Message objects (no cache).",
            "per_message_deep_bytes_* includes the pydantic Message + TextPart + str payload.",
        ],
    }


def _text_identity_ratio(messages: Sequence[Any]) -> float:
    """Fraction of loaded text payloads that share an id with any other message."""
    ids = Counter()
    for message in messages:
        for part in getattr(message, "content", []) or []:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                ids[id(text)] += 1
    if not ids:
        return 0.0
    shared = sum(1 for count in ids.values() if count > 1)
    return round(shared / len(ids), 3)


def _measure_methods(_unused: Any = None) -> list[dict[str, Any]]:
    """Micro-cost of the token-estimation helper used by the loop (ms + bytes)."""
    try:
        from kimi_cli.soul.compaction import estimate_text_tokens

        history, _ = make_history(n_turns=125, output_chars=2500)
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        started = time.perf_counter()
        tokens = estimate_text_tokens(history)
        duration_ms = (time.perf_counter() - started) * 1000
        peak = tracemalloc.get_traced_memory()[1] - before
        return [
            {
                "method": "estimate_text_tokens(500 msg)",
                "tokens": tokens,
                "duration_ms": round(duration_ms, 2),
                "peak_bytes": peak,
            }
        ]
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def _sqlite_disk_report(db_path: Path, *, checkpoint: bool = True) -> dict[str, Any]:
    """On-disk cost of a SQLite DB (files before/after a WAL checkpoint)."""
    before = _file_sizes(db_path)
    pages = None
    table_bytes: dict[str, int] = {}
    if checkpoint:
        try:
            import apsw

            conn = apsw.Connection(str(db_path))
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    pages = int(cursor.execute("PRAGMA page_count").fetchone()[0])
                except Exception:
                    pages = None
                try:
                    names = [
                        row[0]
                        for row in cursor.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type IN ('table','index') ORDER BY name"
                        ).fetchall()
                    ]
                    stat_cursor = conn.cursor()
                    for name in names:
                        try:
                            row = stat_cursor.execute(
                                "SELECT SUM(pgsize) FROM dbstat WHERE name = ?", (name,)
                            ).fetchone()
                            table_bytes[name] = int(row[0]) if row and row[0] else 0
                        except Exception:
                            table_bytes[name] = 0
                except Exception:
                    pass
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            table_bytes = {"error": 0}
            del exc
    after = _file_sizes(db_path)
    return {
        "files": after,
        "total_bytes": sum(after.values()),
        "wal_bytes_before_checkpoint": sum(before.values()) - after.get(db_path.name, 0),
        "page_count": pages,
        "dbstat_bytes": table_bytes,
    }


def _file_sizes(db_path: Path) -> dict[str, int]:
    files: dict[str, int] = {}
    for path in sorted(db_path.parent.glob(db_path.name + "*")):
        try:
            files[path.name] = path.stat().st_size
        except OSError:
            continue
    return files


# --------------------------------------------------------------------------- #
# Case 6 — context_pruning
# --------------------------------------------------------------------------- #

@case(
    "context_pruning",
    "ContextPruner over 500 messages: elision records + duplicate copies",
    preload=_preload_context_pruning,
)
def case_context_pruning() -> dict[str, Any]:
    ensure_import_paths()
    from kimi_cli.soul.context_pruning import ContextPruner

    history, _ = make_history(n_turns=125, tools_per_turn=2, output_chars=3000)
    small_history, _ = make_history(n_turns=10, output_chars=1200)

    pruner = ContextPruner(
        enabled=True,
        trigger_ratio=0.5,
        target_ratio=0.3,
        micro_compress_enabled=False,
    )
    result, prune_stats = _measured(
        lambda: pruner.prune(
            history,
            current_step=10,
            context_usage=0.9,
            max_context_size=128_000,
            current_turn_index=len(history) - 8,
        )
    )
    duration_ms = prune_stats["duration_ms"]
    peak = prune_stats["peak_bytes"]
    elided = result.elided
    elided_bytes = deep_size(elided)
    original_text_bytes = sum(len(record.original_text) for record in elided)
    identity_hits = 0
    for record in elided:
        if record.index < len(history):
            for part in history[record.index].content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text is record.original_text:
                    identity_hits += 1
                    break
    pruned_history = result.messages
    pruned_text_bytes = sum(
        len(part.text)
        for message in pruned_history
        for part in message.content
        if hasattr(part, "text")
    )
    original_text_bytes_total = sum(
        len(part.text)
        for message in history
        for part in message.content
        if hasattr(part, "text")
    )

    # micro-compress path: full deep copy of the history (peak spike)
    micro_pruner = ContextPruner(
        enabled=True,
        trigger_ratio=0.5,
        target_ratio=0.3,
        micro_compress_enabled=True,
    )
    micro_result, micro_stats = _measured(
        lambda: micro_pruner.prune(
            history,
            current_step=20,
            context_usage=0.9,
            max_context_size=128_000,
            current_turn_index=len(history) - 8,
        )
    )
    micro_ms = micro_stats["duration_ms"]
    micro_peak = micro_stats["peak_bytes"]

    # small history: does the pass even fire?
    small_result = pruner.prune(
        small_history, current_step=30, context_usage=0.95, max_context_size=128_000
    )

    metrics = {
        "history_messages": len(history),
        "history_text_bytes": original_text_bytes_total,
        "prune_duration_ms": round(duration_ms, 2),
        "prune_peak_bytes": peak,
        "pruned_messages": len(pruned_history),
        "pruned_text_bytes": pruned_text_bytes,
        "freed_tokens": result.freed_tokens,
        "earliest_removed_index": result.earliest_removed_index,
        "elided_count": len(elided),
        "elided_deep_bytes": elided_bytes,
        "elided_original_text_bytes": original_text_bytes,
        "elided_original_text_same_object_hits": identity_hits,
        "elided_duplicate_payload_ratio": round(
            original_text_bytes / max(1, original_text_bytes_total), 3
        ),
        "elided_kinds": dict(Counter(record.kind for record in elided)),
        "elided_roles": dict(Counter(record.role for record in elided)),
        "elided_records_top": [
            {
                "index": record.index,
                "kind": record.kind,
                "role": record.role,
                "original_text_bytes": len(record.original_text),
                "summary_bytes": len(record.summary),
            }
            for record in sorted(elided, key=lambda r: len(r.original_text), reverse=True)[:10]
        ],
        "duplicate_text_in_history": duplicate_text_report(history),
        "duplicate_text_in_pruned": duplicate_text_report(pruned_history),
        "micro_compress": {
            "duration_ms": round(micro_ms, 2),
            "peak_bytes": micro_peak,
            "elided_count": len(micro_result.elided),
            "freed_tokens": micro_result.freed_tokens,
        },
        "small_history": {
            "messages": len(small_history),
            "elided_count": len(small_result.elided),
            "freed_tokens": small_result.freed_tokens,
        },
        "pruner_state_bytes": deep_size(pruner),
    }
    return {
        "metrics": metrics,
        "keep": [result, pruned_history, micro_result, history],
        "notes": [
            "elided_original_text_same_object_hits counts records whose original_text is the very "
            "same str object as the history TextPart (0 => the record holds a duplicate copy).",
        ],
    }


# --------------------------------------------------------------------------- #
# Case 7 — compaction
# --------------------------------------------------------------------------- #

@case(
    "compaction",
    "compaction pass + ledger + token estimation over 500 messages",
    preload=_preload_compaction,
)
def case_compaction() -> dict[str, Any]:
    ensure_import_paths()
    from kosong.chat_provider.mock import MockChatProvider
    from kosong.message import TextPart as KosongTextPart

    from kimi_cli.llm import ALL_MODEL_CAPABILITIES, LLM
    from kimi_cli.soul.compaction import (
        SimpleCompaction,
        adaptive_preserve_depth,
        estimate_text_tokens,
        should_auto_compact,
    )
    from kimi_cli.soul.compaction_ledger import CompactionLedger, CompactionRecord
    from kimi_cli.soul.tool_pairing import balanced_cut_indices

    history, call_ids = make_history(n_turns=125, tools_per_turn=2, output_chars=3000)

    phases: list[dict[str, Any]] = []

    def phase(name: str, fn: Callable[[], Any]) -> Any:
        out, stats = _measured(fn)
        phases.append({"phase": name, **stats})
        return out

    tokens = phase("estimate_text_tokens", lambda: estimate_text_tokens(history))
    auto = phase(
        "should_auto_compact",
        lambda: should_auto_compact(
            197_000,
            128_000,
            trigger_ratio=0.8,
            reserved_context_size=8_000,
        ),
    )
    depth = phase("adaptive_preserve_depth", lambda: adaptive_preserve_depth(history))
    cuts = phase("balanced_cut_indices", lambda: balanced_cut_indices(history))
    llm = LLM(
        chat_provider=MockChatProvider([KosongTextPart(text="SUMMARY: the conversation ...")]),
        max_context_size=128_000,
        capabilities=ALL_MODEL_CAPABILITIES,
    )

    with temp_env("membench_compaction_") as (tmpdir, work):
        ledger = CompactionLedger.for_session(tmpdir / "session", enabled=True)

        def make_record(i: int) -> CompactionRecord:
            return CompactionRecord(
                compaction_id=f"{i:032x}",
                trigger="auto",
                started_at=float(i),
                shadowed_range=(0, 480),
                shadowed_tokens=120_000,
                summary_tokens=4_000,
                preserved_tokens=3_500,
                shrank=True,
            )

        def write_ledger() -> int:
            for i in range(20):
                record = make_record(i)
                ledger.record_start(record)
                ledger.record_end(record.compaction_id, summary_tokens=4_000, shrank=True)
            return len(ledger._read_records())

        records = phase("compaction_ledger_20_writes", write_ledger)
        ledger_bytes = phase("compaction_ledger_read", lambda: deep_size(ledger._read_records()))
        ledger_file = tmpdir / "session" / ".kimix_cache" / "compaction_ledger.jsonl"
        ledger_disk = ledger_file.stat().st_size if ledger_file.exists() else 0

        compaction = SimpleCompaction()
        result = phase(
            "SimpleCompaction.compact(mock llm)",
            lambda: asyncio.run(compaction.compact(history, llm)),
        )
        result_bytes = 0
        result_messages = 0
        if result is not None:
            result_bytes = deep_size(result.messages)
            result_messages = len(result.messages)

    metrics = {
        "history_messages": len(history),
        "history_deep_bytes": deep_size(history),
        "tokens": tokens,
        "should_auto_compact": auto,
        "adaptive_preserve_depth": depth,
        "balanced_cut_count": len(cuts) if isinstance(cuts, (set, list)) else cuts,
        "phases": phases,
        "ledger": {
            "records_in_file": records,
            "file_bytes": ledger_disk,
            "read_list_deep_bytes": ledger_bytes,
            "bytes_per_record": round(ledger_disk / max(1, records or 1), 1),
        },
        "compacted_messages": result_messages,
        "compacted_deep_bytes": result_bytes,
    }
    return {
        "metrics": metrics,
        "keep": [history, result],
        "notes": [
            "SimpleCompaction runs against MockChatProvider, so the LLM call is offline and instant.",
            "ledger read cost includes json parsing of every line into a fresh dict list.",
        ],
    }


# --------------------------------------------------------------------------- #
# Case 8 — history_index_fts
# --------------------------------------------------------------------------- #

@case(
    "history_index_fts",
    "HistoryIndex/FTS5 over 500 messages: memory + on-disk size",
    preload=_preload_history_index,
)
def case_history_index_fts() -> dict[str, Any]:
    ensure_import_paths()
    from kimi_cli.soul.history_index import HistoryIndex

    history, _ = make_history(n_turns=125, tools_per_turn=2, output_chars=2500)

    with temp_env("membench_history_") as (tmpdir, work):
        db_path = tmpdir / "history" / "history.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_started = time.perf_counter()
        index = HistoryIndex(db_path=db_path)
        init_ms = (time.perf_counter() - init_started) * 1000

        index_started = time.perf_counter()
        index.index_messages(history)
        index_ms = (time.perf_counter() - index_started) * 1000
        search_started = time.perf_counter()
        hits = index.search("module", top_k=5)
        search_ms = (time.perf_counter() - search_started) * 1000
        cjk_hits = index.search("模块", top_k=3)
        index.save()

        legacy_state = {
            "turns_list_len": len(index._turns_list),
            "turns_list_bytes": deep_size(index._turns_list),
            "inverted_index_bytes": deep_size(index._index),
            "tokenizer_bytes": deep_size(index._tokenizer),
            "index_deep_bytes": deep_size(index),
            "uses_fts": bool(index._use_fts),
        }
        disk = _sqlite_disk_report(db_path)
        metrics = {
            "messages_indexed": len(history),
            "init_ms": round(init_ms, 2),
            "index_ms": round(index_ms, 2),
            "search_ms": round(search_ms, 2),
            "hits": len(hits),
            "cjk_hits": len(cjk_hits),
            "legacy_state": legacy_state,
            "disk": disk,
            "bytes_per_message_on_disk": round(
                disk["total_bytes"] / max(1, len(history)), 1
            ),
        }
        index.close()

        # --- ContextDB side: sqlite table + FTS5 index for the same history ---
        context_metrics = _context_db_index_report(tmpdir / "contextdb", history)
        metrics["context_db"] = context_metrics
    return {
        "metrics": metrics,
        "notes": [
            "HistoryIndex always builds the legacy BM25 InvertedIndex + NgramTokenizer "
            "(kimix.retrieval import) even when the FTS5 backend is used.",
            "context_db.* covers ContextDB's messages table + FTS5 virtual table/triggers.",
        ],
    }


def _context_db_index_report(root: Path, history: Sequence[Any]) -> dict[str, Any]:
    """Memory + disk cost of ContextDB's messages table + FTS5 index."""
    from kimi_cli.soul.context_db import ContextDB

    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "context.db"
    db = ContextDB(db_path)

    async def workload() -> dict[str, Any]:
        await db.initialize()
        await db.append_messages(list(history))
        search_started = time.perf_counter()
        hits = await db.search_messages("module", limit=20)
        search_ms = (time.perf_counter() - search_started) * 1000
        count = await db.get_message_count()
        return {
            "messages": count,
            "search_hits": len(hits),
            "search_ms": round(search_ms, 2),
            "result_deep_bytes": deep_size(hits),
            "deep_bytes": deep_size(db),
            "keep": hits,
        }

    result = asyncio.run(workload())
    hits = result.pop("keep")
    asyncio.run(db.close())
    del hits
    return {**result, "disk": _sqlite_disk_report(db_path)}


# --------------------------------------------------------------------------- #
# Case 9 — cli_startup
# --------------------------------------------------------------------------- #

@case("cli_startup", "kimi_cli + kimix CLI startup (--help) without a TTY")
def case_cli_startup() -> dict[str, Any]:
    ensure_import_paths()
    phases: list[dict[str, Any]] = []

    def phase(name: str, fn: Callable[[], Any]) -> Any:
        gc.collect()
        rss0 = rss_bytes()
        started = time.perf_counter()
        out = None
        error = None
        try:
            out = fn()
        except SystemExit as exc:
            out = f"SystemExit({exc.code})"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started) * 1000
        gc.collect()
        rss1 = rss_bytes()
        phases.append(
            {
                "phase": name,
                "duration_ms": round(duration_ms, 2),
                "rss_before_bytes": rss0,
                "rss_after_bytes": rss1,
                "rss_delta_bytes": rss1 - rss0,
                "error": error,
                "result": out if isinstance(out, (str, int, type(None))) else None,
            }
        )
        return out

    from typer.testing import CliRunner

    def import_kimi_cli():
        import kimi_cli.cli as cli_module

        return cli_module

    cli_module = phase("import kimi_cli.cli", import_kimi_cli)
    runner = CliRunner()

    def run_help() -> str:
        result = runner.invoke(cli_module.cli, ["--help"])
        return f"exit={result.exit_code} output_bytes={len(result.output)}"

    help_result = phase("kimi_cli --help (in-process CliRunner)", run_help)

    def run_version() -> str:
        result = runner.invoke(cli_module.cli, ["--version"])
        return f"exit={result.exit_code} output_bytes={len(result.output)}"

    version_result = phase("kimi_cli --version", run_version)

    # kimix CLI: plain function entry point (PEP 810 lazy imports inside)
    def import_kimix() -> Any:
        from kimix.cli_impl.main import cli as kimix_cli

        return kimix_cli

    kimix_entry = phase("import kimix.cli_impl.main", import_kimix)

    def run_kimix_help() -> str:
        previous_argv = sys.argv
        sys.argv = ["kimix", "--help"]
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                kimix_entry()
        except SystemExit as exc:
            return f"exit={exc.code} output_bytes={len(buffer.getvalue())}"
        finally:
            sys.argv = previous_argv
        return f"exit=0 output_bytes={len(buffer.getvalue())}"

    kimix_result = phase("kimix --help", run_kimix_help)

    heavy = {}
    for module in ("kimix.cli_impl.commands", "kimix.utils", "kimi_agent_sdk", "fastapi"):
        heavy[module] = phase(f"import {module}", lambda m=module: str(importlib.import_module(m)))

    rss_after_help = rss_bytes()
    metrics = {
        "phases": phases,
        "kimi_cli_help": help_result,
        "kimi_cli_version": version_result,
        "kimix_help": kimix_result,
        "rss_after_cli_import_bytes": next(
            (row["rss_after_bytes"] for row in phases if row["phase"] == "import kimi_cli.cli"),
            0,
        ),
        "rss_after_help_bytes": rss_after_help,
        "heavy_imports": heavy,
        "sys_modules_count": len(sys.modules),
    }
    return {
        "metrics": metrics,
        "notes": [
            "Runner is typer.testing.CliRunner (no TTY, no stdin, no network).",
            "kimix CLI is invoked via its console-script function with sys.argv = ['kimix','--help'].",
        ],
    }


# --------------------------------------------------------------------------- #
# Case 10 — message_types
# --------------------------------------------------------------------------- #

@case(
    "message_types",
    "pydantic Message/ContentPart/ToolCall sizes vs slots dataclass/dict/tuple",
    preload=_preload_message_types,
)
def case_message_types() -> dict[str, Any]:
    ensure_import_paths()
    from dataclasses import dataclass

    from kosong.message import (
        ContentPart,
        ImageURLPart,
        Message,
        TextPart,
        ThinkPart,
        ToolCall,
    )
    from kosong.tooling import Tool, ToolOk, ToolResult

    from kimi_cli.session_state import TodoItemState
    from kimi_cli.tools.display import TodoDisplayItem

    text = _lorem(1200)
    think = _lorem(600)
    data_uri = "data:image/png;base64," + "A" * 4000
    arguments = json.dumps({"path": "src/app/main.py", "n_lines": 400})
    tool_output = make_tool_output_text(2000)

    def sample_objects() -> dict[str, Any]:
        text_part = TextPart(text=text)
        think_part = ThinkPart(think=think, encrypted="sig-" + "b" * 64)
        image_part = ImageURLPart(image_url=ImageURLPart.ImageURL(url=data_uri, id="img-1"))
        tool_call = ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="read", arguments=arguments),
        )
        tool_result = ToolResult(
            tool_call_id="call_1",
            return_value=ToolOk(output=TextPart(text=tool_output), message="Read 400 lines"),
        )
        simple_message = Message(role="user", content=[TextPart(text=text)])
        rich_message = Message(
            role="assistant",
            content=[think_part, text_part, image_part],
            tool_calls=[tool_call],
            name="kimi",
        )
        tool_message = Message(
            role="tool", content=[TextPart(text=tool_output)], tool_call_id="call_1"
        )
        todo_display = TodoDisplayItem(title="Fix the bug", status="in_progress")
        todo_state = TodoItemState(title="Fix the bug", status="in_progress")
        todo_children = [
            TodoItemState(title=f"sub task {i}", status="pending") for i in range(5)
        ]
        return {
            "TextPart(1200 chars)": text_part,
            "ThinkPart(600 chars + encrypted)": think_part,
            "ImageURLPart(4KB data uri)": image_part,
            "ToolCall(args json)": tool_call,
            "ToolResult(ToolOk+TextPart)": tool_result,
            "Message(user, text only)": simple_message,
            "Message(assistant, think+text+image+tool_call)": rich_message,
            "Message(tool, 2000-char output)": tool_message,
            "TodoDisplayItem": todo_display,
            "TodoItemState": todo_state,
            "TodoItemState x5 children": todo_children,
        }

    objects = sample_objects()
    rows = [
        {
            "object": label,
            "shallow_bytes": sys.getsizeof(value),
            "deep_bytes": deep_size(value),
            "payload_bytes": _payload_bytes(value),
        }
        for label, value in objects.items()
    ]

    # Equivalent payloads under different representations
    @dataclass(slots=True)
    class SlotsMessage:
        role: str
        text: str
        tool_call_id: str | None = None

    @dataclass
    class PlainMessage:
        role: str
        text: str
        tool_call_id: str | None = None

    class DictLike(dict):
        pass

    slots_msg = SlotsMessage(role="tool", text=tool_output, tool_call_id="call_1")
    plain_msg = PlainMessage(role="tool", text=tool_output, tool_call_id="call_1")
    dict_msg = {"role": "tool", "text": tool_output, "tool_call_id": "call_1"}
    tuple_msg = ("tool", tool_output, "call_1")
    pydantic_msg = Message(role="tool", content=[TextPart(text=tool_output)], tool_call_id="call_1")

    representation = [
        {"repr": "pydantic Message + TextPart", "deep_bytes": deep_size(pydantic_msg)},
        {"repr": "dict[str, str]", "deep_bytes": deep_size(dict_msg)},
        {"repr": "tuple[str, str, str]", "deep_bytes": deep_size(tuple_msg)},
        {"repr": "dataclass(slots=True)", "deep_bytes": deep_size(slots_msg)},
        {"repr": "dataclass (no slots)", "deep_bytes": deep_size(plain_msg)},
    ]
    baseline_text_bytes = len(tool_output)
    for row in representation:
        row["overhead_over_payload_bytes"] = row["deep_bytes"] - baseline_text_bytes

    schema = Tool(
        name="read",
        description="desc",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    class_layout = []
    for klass in (
        Message,
        TextPart,
        ThinkPart,
        ImageURLPart,
        ToolCall,
        ToolCall.FunctionBody,
        ToolResult,
        Tool,
        ContentPart,
        TodoDisplayItem,
        TodoItemState,
        SlotsMessage,
        PlainMessage,
    ):
        class_layout.append(_layout_row(klass))

    metrics = {
        "objects": rows,
        "representations": representation,
        "class_layout": class_layout,
        "content_part_registry": {
            "entries": len(ContentPart._ContentPart__content_part_registry),
            "deep_bytes": deep_size(ContentPart._ContentPart__content_part_registry),
        },
        "tool_schema_deep_bytes": deep_size(schema),
        "message_deep_bytes_per_1k_messages_estimate": deep_size(
            [Message(role="user", content=[TextPart(text=text)]) for _ in range(50)]
        )
        * 20,
    }
    return {
        "metrics": metrics,
        "keep": [objects, representation, pydantic_msg, slots_msg, plain_msg, dict_msg, tuple_msg],
        "notes": [
            "deep_bytes uses sys.getsizeof per object (pydantic v2 __dict__ included).",
            "overhead_over_payload_bytes subtracts the raw payload string length only.",
        ],
    }


def _payload_bytes(value: Any) -> int:
    total = 0
    seen: set[int] = set()
    for text in _iter_strings(value, seen):
        total += len(text)
    return total


def _layout_row(klass: type) -> dict[str, Any]:
    slots = klass.__dict__.get("__slots__")
    model_config = getattr(klass, "model_config", None)
    fields = getattr(klass, "model_fields", None)
    return {
        "class": f"{klass.__module__}.{klass.__qualname__}",
        "slots": bool(slots) if slots is not None else False,
        "slots_value": slots if isinstance(slots, (list, tuple)) else None,
        "pydantic_model": model_config is not None,
        "model_config": _config_summary(model_config) if isinstance(model_config, dict) else None,
        "field_count": len(fields) if isinstance(fields, dict) else None,
    }


# --------------------------------------------------------------------------- #
# Static sweep
# --------------------------------------------------------------------------- #

SCAN_ROOTS = [
    KIMI_SRC / "kimi_cli" / "soul",
    KIMI_SRC / "kimi_cli" / "tools",
    KIMI_SRC / "kimi_cli" / "utils",
    KIMIX_SRC / "kimix",
]


def _iter_python_files() -> Iterable[Path]:
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


@case(
    "static_sweep",
    "AST sweep: caches, slotless classes, accumulators, eager payloads",
    use_tracemalloc=False,
)
def case_static_sweep() -> dict[str, Any]:
    import ast

    caches: list[dict[str, Any]] = []
    slotless: list[dict[str, Any]] = []
    accumulators: list[dict[str, Any]] = []
    eager_payloads: list[dict[str, Any]] = []
    deep_copies: list[dict[str, Any]] = []
    module_level_containers: list[dict[str, Any]] = []

    for path in _iter_python_files():
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        lines = source.splitlines()
        rel = _rel(path)
        class_map = _class_map(tree)

        for node in ast.walk(tree):
            # --- caches ---
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    name = _decorator_name(decorator)
                    if name in ("lru_cache", "cache", "cached_property", "functools.lru_cache",
                                "functools.cache"):
                        maxsize, typed = _lru_args(decorator)
                        caches.append(
                            {
                                "file": rel,
                                "line": node.lineno,
                                "function": node.name,
                                "decorator": name,
                                "maxsize": maxsize,
                                "bounded": maxsize not in (None, 0) and name != "cache",
                                "typed": typed,
                                "enclosing_class": class_map.get(id(node)),
                            }
                        )
            # --- classes without slots ---
            if isinstance(node, ast.ClassDef):
                info = _class_analysis(node, lines)
                if info["init_attrs"] >= 6 and not info["has_slots"]:
                    slotless.append({"file": rel, **info})
                if info["accumulators"]:
                    accumulators.append({"file": rel, **info})
            # --- large literals ---
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                length = len(node.value)
                if length >= 4096:
                    eager_payloads.append(
                        {
                            "file": rel,
                            "line": node.lineno,
                            "type": type(node.value).__name__,
                            "bytes": length,
                            "preview": str(node.value)[:60].replace("\n", "\\n"),
                        }
                    )
            # --- deep copies ---
            if isinstance(node, ast.Call):
                func = _decorator_name(node.func)
                if func in ("deepcopy", "copy.deepcopy"):
                    deep_copies.append({"file": rel, "line": node.lineno, "kind": "copy.deepcopy"})
                if func in ("model_copy",) and any(
                    kw.arg == "deep" and isinstance(kw.value, ast.Constant) and kw.value.value
                    for kw in node.keywords
                ):
                    deep_copies.append(
                        {"file": rel, "line": node.lineno, "kind": "model_copy(deep=True)"}
                    )
        # --- module-level mutable containers ---
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, (ast.Dict, ast.List, ast.Set)):
                    name = _assign_name(node)
                    if name and not name.startswith("_"):
                        continue
                    size = 0
                    segment = ast.get_source_segment(source, value) or ""
                    size = len(segment)
                    if size >= 512:
                        module_level_containers.append(
                            {
                                "file": rel,
                                "line": node.lineno,
                                "name": name,
                                "chars": size,
                            }
                        )

    slotless.sort(key=lambda row: row["init_attrs"], reverse=True)
    eager_payloads.sort(key=lambda row: row["bytes"], reverse=True)
    module_level_containers.sort(key=lambda row: row["chars"], reverse=True)
    metrics = {
        "files_scanned": sum(1 for _ in _iter_python_files()),
        "caches": caches,
        "unbounded_caches": [row for row in caches if not row["bounded"]],
        "slotless_classes": slotless[:40],
        "slotless_class_count": len(slotless),
        "accumulator_classes": accumulators[:30],
        "eager_payloads": eager_payloads[:30],
        "eager_payload_total_bytes": sum(row["bytes"] for row in eager_payloads),
        "deep_copies": deep_copies,
        "module_level_containers": module_level_containers[:20],
    }
    return {"metrics": metrics, "notes": ["AST-only sweep; no imports required."]}


def _assign_name(node: ast.AST) -> str | None:
    target = getattr(node, "target", None) or (node.targets[0] if getattr(node, "targets", None) else None)
    if isinstance(target, ast.Name):
        return target.id
    return None


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _lru_args(decorator: ast.AST) -> tuple[Any, bool]:
    if not isinstance(decorator, ast.Call):
        return (None, False)  # bare @cache / @cached_property
    maxsize: Any = None
    typed = False
    for kw in decorator.keywords:
        if kw.arg == "maxsize" and isinstance(kw.value, ast.Constant):
            maxsize = kw.value.value
        if kw.arg == "typed" and isinstance(kw.value, ast.Constant):
            typed = bool(kw.value.value)
    if maxsize is None and decorator.args and isinstance(decorator.args[0], ast.Constant):
        maxsize = decorator.args[0].value
    return (maxsize, typed)


def _class_map(tree: ast.AST) -> dict[int, str]:
    """Map ``id(node) -> enclosing class name`` with a single pass per file."""
    mapping: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.walk(node):
                mapping.setdefault(id(child), node.name)
    return mapping


def _class_analysis(node: ast.ClassDef, lines: Sequence[str]) -> dict[str, Any]:
    has_slots = any(
        isinstance(stmt, (ast.Assign, ast.AnnAssign))
        and _assign_name(stmt) == "__slots__"
        for stmt in node.body
    )
    decorators = [_decorator_name(d) for d in node.decorator_list]
    init_attrs = 0
    accumulators: list[dict[str, Any]] = []
    appended: set[str] = set()
    container_calls = {"Queue", "deque", "defaultdict", "dict", "list", "set", "OrderedDict"}
    for child in ast.walk(node):
        if isinstance(child, ast.FunctionDef) and child.name == "__init__":
            for stmt in child.body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    value = stmt.value
                    targets = (
                        list(stmt.targets) if isinstance(stmt, ast.Assign) else [stmt.target]
                    )
                    for target in targets:
                        name = _attr_name(target)
                        if not name:
                            continue
                        init_attrs += 1
                        is_container = isinstance(value, (ast.List, ast.Dict, ast.Set))
                        called = _decorator_name(value) if isinstance(value, ast.Call) else ""
                        called = called.rsplit(".", 1)[-1]
                        if is_container or called in container_calls:
                            accumulators.append(
                                {
                                    "attr": name,
                                    "line": stmt.lineno,
                                    "container": (
                                        type(value).__name__ if is_container else called
                                    ),
                                }
                            )
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in (
                "append",
                "extend",
                "add",
                "insert",
                "setdefault",
                "put_nowait",
                "put",
                "update",
                "appendleft",
            ):
                name = _attr_name(child.func.value)
                if name:
                    appended.add(name)
    growing = [entry for entry in accumulators if entry["attr"] in appended]
    return {
        "class": node.name,
        "line": node.lineno,
        "init_attrs": init_attrs,
        "has_slots": has_slots,
        "decorators": decorators,
        "accumulators": growing,
    }


def _attr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


# --------------------------------------------------------------------------- #
# Runner / reporting
# --------------------------------------------------------------------------- #

def _median_run(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(runs, key=lambda r: r["measurement"]["tracemalloc_current_bytes"])
    return ordered[len(ordered) // 2]


def run_case_block(name: str, repeat: int = 1, *, do_census: bool = True) -> dict[str, Any]:
    runs = [run_case(name, do_census=do_census) for _ in range(max(1, repeat))]
    result = _median_run(runs)
    if repeat > 1:
        result["repeats"] = [run["measurement"] for run in runs]
    return result


def _meta() -> dict[str, Any]:
    try:
        import psutil  # type: ignore[import-not-found]

        psutil_version: str | None = psutil.__version__
        cpu_count = psutil.cpu_count()
        total_memory = int(psutil.virtual_memory().total)
    except Exception:
        psutil_version = None
        cpu_count = os.cpu_count()
        total_memory = None
    try:
        import pympler  # type: ignore[import-not-found]

        pympler_version: str | None = getattr(pympler, "__version__", "unknown")
    except Exception:
        pympler_version = None
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": cpu_count,
        "total_memory_bytes": total_memory,
        "rss_backend": rss_backend(),
        "psutil": psutil_version,
        "pympler": pympler_version,
        "tracemalloc_frames": _TRACE_FRAMES,
        "repo_root": str(ROOT),
        "argv": sys.argv[1:],
    }


def _print_header(name: str, result: dict[str, Any]) -> None:
    measurement = result["measurement"]
    print("")
    print(f"=== {name} " + "=" * max(0, 60 - len(name)))
    print(f"  duration            : {measurement['duration_ms']:.1f} ms")
    print(
        "  tracemalloc         : current "
        f"{measurement['tracemalloc_current_bytes'] / 1048576:.2f} MiB | peak "
        f"{measurement['tracemalloc_peak_bytes'] / 1048576:.2f} MiB"
    )
    print(
        "  rss                 : "
        f"{measurement['rss_before_bytes'] / 1048576:.1f} -> "
        f"{measurement['rss_after_bytes'] / 1048576:.1f} MiB "
        f"({measurement['rss_delta_bytes'] / 1048576:+.2f} MiB)"
    )
    if result.get("error"):
        print(f"  ERROR               : {result['error']}")
    print("  top allocation sites (tracemalloc lineno diff):")
    for index, site in enumerate(measurement.get("top_sites", [])[:10], 1):
        print(
            f"   {index:2d}. {site['size_bytes'] / 1024:10.1f} KiB "
            f"({site['count']:6d} blocks) {site['site']}  [{site['function']}]"
        )
    census = measurement.get("census") or {}
    if census and census.get("top_types"):
        print(
            f"  live objects        : {census.get('objects_scanned', 0)} scanned in "
            f"{census.get('census_ms', 0)} ms"
        )
        for index, row in enumerate(census["top_types"][:8], 1):
            print(
                f"   {index:2d}. {row['type'][:52]:52s} count={row['count']:8d} "
                f"deep={row['deep_bytes'] / 1048576:8.2f} MiB"
            )


def _print_scalars(metrics: dict[str, Any]) -> None:
    for key, value in metrics.items():
        if isinstance(value, (int, float, str, bool)) or value is None:
            if isinstance(value, float):
                print(f"  {key:38s}: {value:,.2f}")
            elif isinstance(value, int) and not isinstance(value, bool):
                print(f"  {key:38s}: {value:,}")
            else:
                print(f"  {key:38s}: {value}")
        elif isinstance(value, dict):
            scalars = {
                k: v
                for k, v in value.items()
                if isinstance(v, (int, float, str, bool)) or v is None
            }
            if scalars:
                compact = ", ".join(f"{k}={v}" for k, v in scalars.items())
                print(f"  {key:38s}: {compact}")
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict) and sub_value and all(
                    isinstance(v, (int, float, str, bool)) or v is None
                    for v in sub_value.values()
                ):
                    compact = ", ".join(f"{k}={v}" for k, v in sub_value.items())
                    print(f"  {key + '.' + sub_key:38s}: {compact}")
                elif isinstance(sub_value, list) and sub_value and isinstance(sub_value[0], dict):
                    _print_tables({f"{key}.{sub_key}": sub_value})


def _print_tables(metrics: dict[str, Any], limit: int = 12) -> None:
    for key, value in metrics.items():
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            continue
        headers = [h for h in value[0].keys() if not isinstance(value[0][h], (list, dict))]
        headers = headers[:6]
        wide = {"file": 58, "site": 52, "module": 44, "object": 44, "repr": 32, "class": 46,
                "phase": 40, "method": 30, "name": 18, "function": 30, "type": 40}
        widths = [max(len(h), wide.get(h, 0)) for h in headers]
        print(f"  -- {key} (top {min(limit, len(value))} of {len(value)})")
        print("     " + " | ".join(h[:w] for h, w in zip(headers, widths)))
        for row in value[:limit]:
            cells = []
            for header, width in zip(headers, widths):
                cell = row.get(header)
                if isinstance(cell, bool):
                    cells.append("yes" if cell else "no")
                elif isinstance(cell, float):
                    cells.append(f"{cell:,.1f}")
                elif isinstance(cell, int):
                    cells.append(f"{cell:,}")
                else:
                    cells.append(str(cell)[:width])
            print("     " + " | ".join(cells))
    for key, value in metrics.items():
        if isinstance(value, dict) and value and isinstance(next(iter(value.values())), list):
            _print_tables({key: value}, limit=limit)


def print_result(result: dict[str, Any]) -> None:
    _print_header(result["case"], result)
    metrics = result.get("metrics") or {}
    if metrics:
        print("  metrics:")
        _print_scalars(metrics)
        _print_tables(metrics)
    tables = result.get("tables") or {}
    for name, table in tables.items():
        if isinstance(table, dict):
            for sub_name, sub in table.items():
                if isinstance(sub, dict):
                    scalars = {
                        k: v for k, v in sub.items() if isinstance(v, (int, float, str, bool))
                    }
                    if scalars:
                        print(f"  -- {name}.{sub_name}: " + ", ".join(
                            f"{k}={v}" for k, v in scalars.items()
                        ))
                    if isinstance(sub.get("attribution"), list):
                        _print_tables({f"{name}.{sub_name}.attribution": sub["attribution"]})
    for note in result.get("notes") or []:
        print(f"  note: {note}")


def _merge_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.setdefault("cases", {})
    existing.setdefault("meta", {})
    existing["meta"].update(payload.get("meta", {}))
    existing["cases"].update(payload.get("cases", {}))
    if "static_sweep" in payload:
        existing["static_sweep"] = payload["static_sweep"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return existing


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(KIMIX_SRC), str(KIMI_SRC), str(ROOT), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_all(repeat: int = 1, *, do_census: bool = True) -> dict[str, Any]:
    """Run every case in its own subprocess and aggregate the results."""
    import subprocess

    script = str(Path(__file__).resolve())
    cases: dict[str, Any] = {}
    for name in CASE_ORDER:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="membench_child_") as tmp:
            out = Path(tmp) / "case.json"
            cmd = [sys.executable, script, name, "--json-out", str(out)]
            if repeat > 1:
                cmd += ["--repeat", str(repeat)]
            if not do_census:
                cmd.append("--no-census")
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=900,
                    env=_child_env(),
                )
            except subprocess.TimeoutExpired:
                cases[name] = {"case": name, "error": "timeout after 900s"}
                print(f"[{name}] TIMEOUT")
                continue
            if out.exists():
                try:
                    payload = json.loads(out.read_text(encoding="utf-8"))
                    result = payload.get("cases", {}).get(name, {})
                except Exception as exc:  # pragma: no cover
                    result = {"case": name, "error": f"bad child json: {exc}"}
            else:
                result = {
                    "case": name,
                    "error": f"child produced no output (exit {proc.returncode})",
                    "stderr": (proc.stderr or "")[-1500:],
                }
            result["child_wall_s"] = round(time.perf_counter() - started, 2)
            cases[name] = result
        print_result(result)
        print(f"  wall                : {result['child_wall_s']} s")
    return {"cases": cases}


def static_sweep_only() -> dict[str, Any]:
    return run_case_block("static_sweep", 1, do_census=False)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mem_bench.py",
        description="Deep memory benchmark harness (measurement only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Cases:\n  "
            + "\n  ".join(f"{name:20s} {CASE_DOCS.get(name, '')}" for name in CASE_ORDER)
        ),
    )
    parser.add_argument("case", nargs="?", help="case name to run in this process")
    parser.add_argument("--all", action="store_true", help="run every case in a fresh subprocess")
    parser.add_argument("--json", metavar="PATH", help="merge machine-readable results into PATH")
    parser.add_argument("--repeat", type=int, default=1, metavar="N", help="repeat each case N times")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--no-census", action="store_true", help="skip the live-object census")
    parser.add_argument("--import-probe", metavar="MODULE", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--native-probe", action="store_true", default=False, help=argparse.SUPPRESS
    )
    parser.add_argument("--json-out", metavar="PATH", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    ensure_import_paths()

    if args.native_probe:
        payload = native_probe()
        text = json.dumps(payload)
        if args.json_out:
            Path(args.json_out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    if args.import_probe is not None:
        payload = import_probe(args.import_probe)
        text = json.dumps(payload)
        if args.json_out:
            Path(args.json_out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    if args.list:
        print("cases:")
        for name in CASE_ORDER:
            print(f"  {name:20s} {CASE_DOCS.get(name, '')}")
        return 0

    if args.all:
        aggregate = run_all(max(1, args.repeat), do_census=not args.no_census)
        payload = {"meta": _meta(), "cases": aggregate["cases"]}
        sweep = aggregate["cases"].get("static_sweep")
        if sweep:
            payload["static_sweep"] = sweep.get("metrics", {})
        if args.json:
            saved = _merge_json(Path(args.json), payload)
            print(f"\nwrote {len(saved.get('cases', {}))} case(s) to {args.json}")
        return 0

    if not args.case:
        parser.print_help()
        return 2

    if args.case not in CASES:
        print(f"unknown case: {args.case}", file=sys.stderr)
        print("available: " + ", ".join(CASE_ORDER), file=sys.stderr)
        return 2

    result = run_case_block(args.case, max(1, args.repeat), do_census=not args.no_census)
    print_result(result)
    payload = {"meta": _meta(), "cases": {args.case: result}}
    if args.case == "static_sweep":
        payload["static_sweep"] = result.get("metrics", {})
    if args.json:
        saved = _merge_json(Path(args.json), payload)
        print(f"\nwrote {args.case} to {args.json} ({len(saved.get('cases', {}))} cases total)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
