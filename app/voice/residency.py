"""Which speech model is allowed to sit in memory at any moment.

Recognition runs in its own process, so within this one the only thing that needs
bounding is synthesis: the Russian engine pulls in torch and costs several times
what the English one does. A voice turn is recognition, then a cascade, then
speech, so both languages are not needed at once - only the one being spoken.

The policy is therefore least-recently-used eviction down to a limit, which keeps
the last engine warm when a reply stays in one language and costs a reload when
the conversation switches.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class Residency:
    """Keeps at most `limit` loaded engines, evicting the least recently used."""

    def __init__(self, limit: int = 1) -> None:
        self.limit = max(1, int(limit))
        self._loaded: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def resident(self) -> list[str]:
        with self._lock:
            return list(self._loaded)

    def use(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return the loaded object for `key`, loading it and evicting as needed."""
        with self._lock:
            existing = self._loaded.get(key)
            if existing is not None:
                self._loaded.move_to_end(key)
                return existing
        # Loading takes seconds, so it happens outside the lock; two callers racing
        # for the same key both load and the second one wins, which costs time but
        # never correctness.
        loaded = loader()
        self.adopt(key, loaded)
        return loaded

    def adopt(self, key: str, loaded: Any) -> Any:
        with self._lock:
            self._loaded[key] = loaded
            self._loaded.move_to_end(key)
            while len(self._loaded) > self.limit:
                evicted, _value = self._loaded.popitem(last=False)
                _release(evicted)
            return loaded

    def release_all(self) -> None:
        with self._lock:
            keys = list(self._loaded)
            self._loaded.clear()
        for key in keys:
            _release(key)


def _release(key: str) -> None:
    """Let go of a model that is no longer needed.

    Dropping the reference does not return the memory. The interpreter keeps the
    arenas, and torch's own allocator keeps the model, so a process that has spoken
    one language and then the other stays at its high-water mark: measured here at
    roughly the same figure either way. Collecting and trimming are still done
    because they help the pure-Python engine and cost nothing, but the real bound
    on this service is its memory limit, not this function - the honest way to give
    the pages back would be a process per engine, killed on switch, which is more
    machinery than a reply's latency saving is worth.
    """
    import ctypes
    import gc

    try:
        gc.collect()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # pragma: no cover - not glibc, or trimmed elsewhere
        pass


__all__ = ["Residency"]
