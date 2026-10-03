"""Shared storage-path helpers for the Vault module.

`images.py` and `tasks.py` grew their own copies of the same three helpers
(storage root, filename sanitizing, root-containment check). They live here now,
so a path-traversal fix only has to land once.
"""

import re
from pathlib import Path

from app.core.config import get_settings

_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9._-]+")


def storage_root() -> Path:
    return Path(get_settings().LOCAL_STORAGE_ROOT)


def safe_segment(value: str, fallback: str = "image") -> str:
    cleaned = _SAFE_SEGMENT.sub("-", str(value or "")).strip("-.")
    return (cleaned or fallback)[:80]


def within_root(candidate: Path, *, root: Path | None = None) -> bool:
    base = root if root is not None else storage_root()
    try:
        resolved_root = base.resolve()
    except OSError:
        return False
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(resolved_root)


__all__ = ["safe_segment", "storage_root", "within_root"]
