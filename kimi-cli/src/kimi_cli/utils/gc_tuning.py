"""Garbage-collector tuning for long-running CLI processes.

The CLI process allocates a large, permanent object graph during import and
startup: every imported module dict, the class objects, the prompt constants,
the tool registry and the JSON schemas derived from it.  The cyclic collector
re-walks all of that on every full (generation 2) collection, which costs CPU
and, more importantly, keeps the whole import graph "hot" in the collector's
bookkeeping.

:func:`freeze_startup_objects` moves everything allocated so far into the
collector's *permanent* generation, which is never scanned again.  It is called
once from the process entry point (``kimi_cli.__main__``) *before* any session
is created, so per-session objects stay fully collectible — this is the
conservative placement: freezing after a session was built would make that
session's messages immortal, which is exactly what a session-per-process server
must not do.

Nothing here changes allocation or retention semantics; it only changes what the
collector scans.  ``gc.freeze`` is a no-op when the interpreter has no cyclic
collector support (it always does, but the call is guarded anyway so that a
vendored/embedded interpreter can never break the CLI).
"""

from __future__ import annotations

import gc

from kimi_cli.utils.logging import logger

_frozen = False


def is_frozen() -> bool:
    """Whether the startup object graph has already been frozen in this process."""
    return _frozen


def freeze_startup_objects() -> bool:
    """Move the current object graph to the permanent GC generation.

    Returns ``True`` when the freeze ran, ``False`` when it was a no-op (already
    frozen, or the interpreter lacks the API).  Never raises: a broken GC hook
    must not prevent the CLI from starting.
    """
    global _frozen
    if _frozen:
        return False
    freeze = getattr(gc, "freeze", None)
    if freeze is None:  # pragma: no cover - CPython always has it
        return False
    try:
        freeze()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("gc.freeze() failed, continuing without it: {err}", err=exc)
        return False
    _frozen = True
    stats = gc.get_stats()
    logger.debug(
        "Froze startup objects into the permanent GC generation "
        "(gen0={g0} gen1={g1} gen2={g2} collections so far, threshold={threshold})",
        g0=stats[0]["collections"],
        g1=stats[1]["collections"],
        g2=stats[2]["collections"],
        threshold=gc.get_threshold(),
    )
    return True


def reset_for_tests() -> None:
    """Forget that the freeze ran (test helper; does not un-freeze objects)."""
    global _frozen
    _frozen = False
