"""In-memory TTL cache used by :class:`RegistryClient`.

Single-process, asyncio-single-threaded — no locks needed; dict mutations
are atomic between ``await`` points. Eviction is "drop one expired entry,
else drop the oldest insertion" — good enough for the few hundred entries
a LayerLoupe instance accumulates.
"""

from __future__ import annotations

import time
from typing import Any


class TTLCache:
    """Bounded TTL cache.

    Stores arbitrary hashable keys → arbitrary Python values with a per-entry
    deadline. Calls outside of ``__init__`` are O(1) except eviction (O(n)
    in the worst case when scanning for an expired entry to drop).
    """

    def __init__(self, max_size: int = 256) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._max_size = max_size

    def get(self, key: Any) -> tuple[bool, Any]:
        """Return ``(hit, value)``. Expired entries are pruned on read."""
        entry = self._data.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._data.pop(key, None)
            return False, None
        return True, value

    def set(self, key: Any, value: Any, ttl: float) -> None:
        """Store ``value`` under ``key`` with ``ttl`` seconds to live."""
        if key not in self._data and len(self._data) >= self._max_size:
            self._evict_one()
        self._data[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: Any) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def _evict_one(self) -> None:
        """Drop one expired entry if present, otherwise the oldest insertion."""
        now = time.monotonic()
        for k, (exp, _) in self._data.items():
            if exp <= now:
                self._data.pop(k, None)
                return
        if self._data:
            # Python preserves insertion order — drop the first key.
            self._data.pop(next(iter(self._data)), None)
