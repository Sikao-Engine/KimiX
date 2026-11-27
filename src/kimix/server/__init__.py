"""Kimix opencode-style HTTP server with SSE event streaming.

Public surface
--------------
* :func:`create_app` — build the FastAPI application (health, ``/event`` SSE,
  session CRUD, prompt, abort, permissions, clear, context, compact, export,
  summarize, plan).
* :class:`KimixAsyncClient` — async HTTP/SSE client for the above.
* :class:`SessionManager` — process-wide session store; the singleton lives at
  ``kimix.server.session_manager.session_manager``.
* :class:`BusEvent` / :class:`EventBus` — in-process event bus; the singleton
  lives at ``kimix.server.bus.bus``.
* :func:`serve_cli` — ``kimix serve`` entry point (uvicorn).

Startup-performance design (Python 3.15+)
-----------------------------------------
``kimix.server.app`` imports FastAPI and ``kimix.server.session_manager``,
which imports the whole agent runtime; ``kimix.server.client`` imports httpx.
Importing this package for its name alone must not pay that cost, so the
exports are provided lazily through PEP 562 (``__getattr__`` + ``__all__`` +
``__dir__``).  ``from kimix.server.bus import EventBus`` and
``from kimix.server import session_manager`` keep working unchanged, and
importing a submodule also sets it as an attribute of this package exactly as
the import system always did.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    # -- kimix.server.app
    "create_app": "kimix.server.app",
    # -- kimix.server.bus
    "BusEvent": "kimix.server.bus",
    "EventBus": "kimix.server.bus",
    # -- kimix.server.client
    "KimixAsyncClient": "kimix.server.client",
    # -- kimix.server.session_manager
    "SessionManager": "kimix.server.session_manager",
    # -- kimix.server.serve
    "serve_cli": "kimix.server.serve",
}

# ``bus`` and ``session_manager`` are deliberately *not* re-exported here: both
# are submodule names, so the import system binds them to the module. The
# singletons live one level down — ``kimix.server.bus.bus`` and
# ``kimix.server.session_manager.session_manager``.
_SUBMODULES = (
    "app",
    "bus",
    "client",
    "serve",
    "session_manager",
)

__all__ = [*_EXPORTS, *_SUBMODULES]


def __getattr__(name: str) -> Any:  # PEP 562 module-level __getattr__
    """Resolve a public export lazily from its providing module."""
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name)
    try:
        value = getattr(module, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    # Cache the reified object so repeat lookups see a normal namespace.
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # PEP 562 module-level __dir__
    """Expose the full public namespace (resolved or not) to ``dir()``."""
    return sorted(set(globals()) | set(__all__))
