from __future__ import annotations

import asyncio

QueueShutDown = asyncio.QueueShutDown  # type: ignore[assignment]

class Queue[T](asyncio.Queue[T]):
    """Asyncio Queue with shutdown support."""

