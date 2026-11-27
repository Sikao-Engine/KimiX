"""kimix networking layer: length-prefixed TCP transport and JSON-RPC on top of it.

Public surface
--------------
* :class:`TCPClient` / :class:`TCPServer` — low-level length-prefixed sockets.
* :class:`TcpGroupServer` — many-socket broadcast server.
* :class:`JSONRPCClient` / :class:`JSONRPCServer` — JSON-RPC 2.0 over TCP
  (the server also exposes a WebSocket bridge).

Startup-performance design (Python 3.15+)
-----------------------------------------
``kimix.network.tcp_client`` (and everything above it) imports
``kimix.ui.printing``, which pulls in ``kimi_cli``.  Importing this package for
its name alone must not pay that cost, so the exports are provided lazily
through PEP 562 (``__getattr__`` + ``__all__`` + ``__dir__``); each name
resolves to the identical object the eager ``from ... import *`` would have
produced.  ``from kimix.network.tcp_client import TCPClient`` keeps working
unchanged.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    # -- kimix.network.tcp_client
    "TCPClient": "kimix.network.tcp_client",
    # -- kimix.network.tcp_server
    "TCPServer": "kimix.network.tcp_server",
    # -- kimix.network.tcp_group_server
    "TcpGroupServer": "kimix.network.tcp_group_server",
    # -- kimix.network.rpc_client
    "JSONRPCClient": "kimix.network.rpc_client",
    # -- kimix.network.rpc_server
    "JSONRPCServer": "kimix.network.rpc_server",
}

_SUBMODULES = (
    "rpc_client",
    "rpc_server",
    "tcp_client",
    "tcp_group_server",
    "tcp_server",
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
    value = getattr(module, name)
    # Cache the reified object so repeat lookups see a normal namespace.
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # PEP 562 module-level __dir__
    """Expose the full public namespace (resolved or not) to ``dir()``."""
    return sorted(set(globals()) | set(__all__))
