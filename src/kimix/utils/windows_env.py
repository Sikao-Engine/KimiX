"""Windows registry environment reader (re-exported from kimi_cli).

Usage:
    from kimix.utils.windows_env import refresh_env_from_registry
    refresh_env_from_registry()
"""
from __future__ import annotations

from kimi_cli.utils.environment import (
    refresh_env_from_registry,
    _expand_registry_string,
    _read_registry_value,
)

__all__ = [
    "refresh_env_from_registry",
    "_expand_registry_string",
    "_read_registry_value",
]
