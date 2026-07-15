"""Common utility functions for kimix tools.

This module provides shared helpers used across the kimix.tools.* modules,
starting with rtk (reasoning toolkit) installation support.
"""

from __future__ import annotations

from kimi_cli.install import RTK_VERSION, ensure_rtk_path

__all__ = [
    "RTK_VERSION",
    "get_rtk_path",
]


async def get_rtk_path() -> str:
    """Ensure rtk is installed and return its path.

    Downloads and installs rtk to the shared bin directory if not already present.
    """
    return await ensure_rtk_path()
