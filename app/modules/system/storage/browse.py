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


class ModuleOwnedPathError(PermissionError):
    """The path belongs to a module's storage namespace, and this browser may
    look but not take.

    A module namespace is where that module keeps its own bookkeeping — the Vault
    its cards and their media, Alllib its books. Those bytes are reachable through
    the module's own endpoints, which know what the owner is allowed to see and
    which files a locked vault is still allowed to serve.

    This browser knew none of that. Its download path decrypted anything the
    application file key could open — so for every file a keyless worker wrote, a
    sealed Vault's video came out in the clear to anyone who could reach this page.
    That is the hole, and it stays shut: reading, renaming, deleting and uploading
    inside a module namespace are all refused.

    Listing is allowed, which is what makes this browser useful for the question it
    is actually asked — what is taking up the disk. A name is not a secret once a
    sealed media path is a random one, and the sizes were already on screen in a
    summary. What a name can still say is what an *unencrypted* collection holds,
    which is the price of this and is stated here rather than discovered later.
    """


def module_namespace(path: str) -> str | None:
    """The module that owns this path's first segment, if any."""
    from app.core.modules import module_registry

    segments = [segment for segment in (path or "").split("/") if segment]
    if not segments:
        return None
    return module_registry.storage_owner(segments[0])


def is_module_owned(path: str) -> bool:
    """Whether this path is inside a module's storage namespace."""
    return module_namespace(path) is not None


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
    # A module-owned namespace summarised rather than listed: how many objects
    # and how many bytes, with no name and nothing to open. `path` stays empty so
    # no caller can build a download URL out of it by accident.
    opaque: bool = False
    objects: int = 0

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
            "size_human": format_size(self.size),
            "modified": self.modified,
            "modified_label": self.modified_label,
            "encrypted": self.encrypted,
            "opaque": self.opaque,
            "objects": self.objects,
        }


@dataclass(slots=True)
class Listing:
    path: str
    entries: list[Entry] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    backend: str = "local"


def _looks_encrypted(head: bytes) -> bool:
    from app.core.storage import ENCRYPTED_FILE_MAGIC, StorageInterface

    return head.startswith(ENCRYPTED_FILE_MAGIC) or StorageInterface._seekable_version(head) > 0


def list_local(path: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0) -> Listing:
    """List one folder: directories first, then files, both alphabetical.

    A module's namespace is listed like any other folder. Reading and changing
    what is inside it is not — see `ModuleOwnedPathError`.
    """
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
    """List an S3 prefix, folding the flat key space into folders.

    A module's namespace is listed like any other prefix; taking what is in it is
    refused elsewhere, for the reasons in `ModuleOwnedPathError`.
    """
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
    """Open a stored object for download.

    Refused inside a module's namespace: this function decrypts with the
    application file key and knows nothing about the lock that module applies to
    its own files. A sealed Vault's worker-written video used to come out of here
    in the clear, because the browser never asked the Vault whether it was locked.
    """
    """Return (stream-or-iterator, size, media type, download name) for one file.

    Encrypted objects are decrypted on the way out: the operator gets the bytes
    the module stored, not the envelope. Seekable envelopes are streamed chunk
    by chunk so a multi-gigabyte video never lands in memory at once.

    The download name drops the `.enc` suffix and the media type is taken from
    the extension underneath it. Without that the browser saves `clip.mp4.enc`
    as `application/octet-stream`, which is the plaintext bytes wearing the
    envelope's name — correct bytes, useless file. The suffix is only stripped
    from an object that really is an envelope: a user who uploaded their own
    `archive.enc` gets it back under that name.
    """
    from app.core.storage import get_storage

    if not path:
        raise StoragePathError("A file path is required")
    if is_module_owned(path):
        raise ModuleOwnedPathError(
            f"'{path}' belongs to a module and is served by that module's own endpoints"
        )
    backend = get_storage()
    if not backend.file_exists(path):
        raise FileNotFoundError(path)

    stored_name = PurePosixPath(path).name
    try:
        size = backend.get_file_size(path)
    except (OSError, ValueError):
        size = 0

    if backend.is_seekable_encrypted(path):
        total = backend.get_seekable_plaintext_size(path)
        name = _plain_name(stored_name)
        return backend.read_seekable_range(path, 0, total), total, guess_media_type(name), name

    with backend.get_file_stream(path) as stream:
        head = stream.read(8)
    if _looks_encrypted(head):
        name = _plain_name(stored_name)
        return _single_pass(backend.get_file_decrypted(path)), size, guess_media_type(name), name
    return backend.get_file_stream(path), size, guess_media_type(stored_name), stored_name


def _plain_name(name: str) -> str:
    """The name the object has once its envelope is off."""
    from app.core.storage import ENCRYPTED_SUFFIX

    return name[: -len(ENCRYPTED_SUFFIX)] if name.endswith(ENCRYPTED_SUFFIX) else name


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
