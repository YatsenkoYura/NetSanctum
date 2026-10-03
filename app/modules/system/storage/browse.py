"""Local-first folder browser for the Storage module.

The manager is deliberately thin: it walks the storage root itself and never
goes through a module's own bookkeeping. Two rules make that safe enough to
expose:

* every path is normalized and proven to stay inside the storage root before
  it touches the filesystem, and
* deletions and renames still go through the owning module's cleanup hook, so
  a database row never keeps pointing at bytes that are gone.

Folders, uploads and renames are local-filesystem features. On S3 there are no
directories — objects are flat keys — so those calls are refused instead of
silently doing something that looks like a folder.
"""

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import get_settings

# Conservative segment sanitizer: keeps letters, digits, dot, dash, underscore
# and spaces. Everything else (separators, traversal, control characters) is
# folded away before the name ever reaches the filesystem.
_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")
MAX_SEGMENT = 120
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


class StoragePathError(ValueError):
    """The requested path is malformed or escapes the storage root."""


class UnsupportedOnBackendError(ValueError):
    """The operation has no meaning on the configured backend (e.g. folders on S3)."""


def _storage_root() -> Path:
    return Path(get_settings().LOCAL_STORAGE_ROOT)


def storage_root() -> Path:
    """Resolved storage root. Public so the router can report the same path."""
    return _storage_root().resolve()


def is_remote() -> bool:
    return get_settings().STORAGE_BACKEND == "s3"


def normalize_folder(raw: str | None) -> str:
    """Normalize a folder path to `a/b` form.

    The root is the empty string. Absolute paths, `.`/`..` segments, NUL bytes
    and backslashes are rejected outright rather than cleaned up: a caller that
    sends `../` is a bug or an attack, not a path to normalise.
    """
    text = (raw or "").strip().replace("\\", "/")
    if not text or text == "/":
        return ""
    if "\x00" in text:
        raise StoragePathError("Path contains a NUL byte")
    if text.startswith("/"):
        raise StoragePathError("Path must be relative to the storage root")
    parts = []
    for segment in text.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            raise StoragePathError("Path traversal is not allowed")
        parts.append(segment)
    if not parts:
        return ""
    return "/".join(parts)


def safe_segment(name: str, *, fallback: str = "file") -> str:
    """Sanitize one path segment (a file or folder name)."""
    text = _UNSAFE.sub("-", str(name or "")).strip(" -. ")
    text = text[:MAX_SEGMENT]
    if not text or text in {".", ".."}:
        return fallback
    return text


def join_folder(*segments: str) -> str:
    return normalize_folder("/".join(segment for segment in segments if segment))


def parent_of(path: str) -> str:
    parts = PurePosixPath(path).parts if path else ()
    return "/".join(parts[:-1]) if len(parts) > 1 else ""


def breadcrumbs(path: str) -> list[tuple[str, str]]:
    """(label, path) pairs from the root down to `path`."""
    crumbs = [("/", "")]
    walked = ""
    for segment in PurePosixPath(path).parts if path else ():
        walked = f"{walked}/{segment}" if walked else segment
        crumbs.append((segment, walked))
    return crumbs


def resolve_local(path: str) -> Path:
    """Absolute path for a normalized logical path, proven inside the root."""
    root = storage_root()
    candidate = (root / path).resolve() if path else root
    # `is_relative_to` compares resolved components, so a sibling directory whose
    # name merely starts with the root name cannot pass.
    if candidate != root and not candidate.is_relative_to(root):
        raise StoragePathError("Path escapes the storage root")
    return candidate


@dataclass(slots=True)
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified: float = 0.0
    encrypted: bool = False

    @property
    def modified_label(self) -> str:
        """Local timestamp for the table; empty when the backend has none."""
        if not self.modified:
            return ""
        return datetime.datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d %H:%M")

    def as_dict(self, *, format_size) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "size_human": "" if self.is_dir else format_size(self.size),
            "modified": self.modified,
            "modified_label": self.modified_label,
            "encrypted": self.encrypted,
        }


@dataclass(slots=True)
class Listing:
    path: str
    entries: list[Entry] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    backend: str = "local"


def _looks_encrypted(head: bytes) -> bool:
    from app.core.storage import ENCRYPTED_FILE_MAGIC, SEEKABLE_MAGIC

    return head.startswith(ENCRYPTED_FILE_MAGIC) or head.startswith(SEEKABLE_MAGIC)


def list_local(path: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0) -> Listing:
    """List one folder: directories first, then files, both alphabetical."""
    target = resolve_local(path)
    if not target.exists():
        raise StoragePathError(f"No such folder: {path or '/'}")
    if not target.is_dir():
        raise StoragePathError(f"Not a folder: {path}")

    directories: list[Entry] = []
    files: list[Entry] = []
    with os.scandir(target) as scan:
        for item in scan:
            child = f"{path}/{item.name}" if path else item.name
            try:
                stat = item.stat()
            except OSError:
                # A vanished or unreadable entry must not abort the whole listing.
                continue
            if item.is_dir():
                directories.append(Entry(item.name, child, True, modified=stat.st_mtime))
                continue
            encrypted = False
            if stat.st_size:
                try:
                    with open(item.path, "rb") as handle:
                        encrypted = _looks_encrypted(handle.read(8))
                except OSError:
                    encrypted = False
            files.append(Entry(item.name, child, False, stat.st_size, stat.st_mtime, encrypted))

    directories.sort(key=lambda entry: entry.name.lower())
    files.sort(key=lambda entry: entry.name.lower())
    combined = directories + files
    window = combined[offset : offset + limit]
    return Listing(
        path=path,
        entries=window,
        total=len(combined),
        truncated=offset + limit < len(combined),
    )


def list_remote(prefix: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0) -> Listing:
    """List an S3 prefix, folding the flat key space into folders."""
    from app.core.storage import get_storage

    backend = get_storage()
    client = getattr(backend, "_client", None)
    bucket = getattr(backend, "_bucket", None)
    if client is None or bucket is None:
        raise UnsupportedOnBackendError("S3 listing is unavailable on this backend")

    delimiter = "/"
    search = f"{prefix}/" if prefix else ""
    paginator = client.get_paginator("list_objects_v2")
    folders: dict[str, Entry] = {}
    files: list[Entry] = []
    truncated = False

    for page in paginator.paginate(Bucket=bucket, Prefix=search, Delimiter=delimiter):
        for common in page.get("CommonPrefixes", []):
            folder_key = common["Prefix"].rstrip("/")
            folders[folder_key] = Entry(
                name=folder_key.rsplit("/", 1)[-1],
                path=folder_key,
                is_dir=True,
            )
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key.rsplit("/", 1)[-1]
            if not name:
                continue
            files.append(
                Entry(
                    name=name,
                    path=key,
                    is_dir=False,
                    size=obj.get("Size", 0),
                    modified=float(obj.get("LastModified", 0).timestamp())
                    if obj.get("LastModified")
                    else 0.0,
                )
            )
        if len(folders) + len(files) > MAX_LIMIT:
            truncated = True
            break

    combined = [folders[key] for key in sorted(folders, key=str.lower)]
    combined += sorted(files, key=lambda entry: entry.name.lower())
    return Listing(
        path=prefix,
        entries=combined[offset : offset + limit],
        total=len(combined),
        truncated=truncated or offset + limit < len(combined),
        backend="s3",
    )


def list_folder(path: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0) -> Listing:
    bounded = max(1, min(int(limit), MAX_LIMIT))
    return (
        list_remote(path, limit=bounded, offset=max(0, offset))
        if is_remote()
        else list_local(path, limit=bounded, offset=max(0, offset))
    )


def read_object(path: str) -> tuple[Any, int, str, str]:
    """Return (stream-or-iterator, size, media type, download name) for one file.

    Encrypted objects are decrypted on the way out: the operator gets the bytes
    the module stored, not the envelope. Seekable envelopes are streamed chunk
    by chunk so a multi-gigabyte video never lands in memory at once.
    """
    from app.core.storage import get_storage

    if not path:
        raise StoragePathError("A file path is required")
    backend = get_storage()
    if not backend.file_exists(path):
        raise FileNotFoundError(path)

    name = PurePosixPath(path).name
    try:
        size = backend.get_file_size(path)
    except (OSError, ValueError):
        size = 0

    if backend.is_seekable_encrypted(path):
        total = backend.get_seekable_plaintext_size(path)
        return backend.read_seekable_range(path, 0, total), total, guess_media_type(name), name

    with backend.get_file_stream(path) as stream:
        head = stream.read(8)
    if _looks_encrypted(head):
        return _single_pass(backend.get_file_decrypted(path)), size, guess_media_type(name), name
    return backend.get_file_stream(path), size, guess_media_type(name), name


def _single_pass(payload: bytes):
    yield payload


MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".cbz": "application/vnd.comicbook+zip",
    ".zip": "application/zip",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
}


def guess_media_type(name: str) -> str:
    return MEDIA_TYPES.get(PurePosixPath(name).suffix.lower(), "application/octet-stream")
