"""Vault images as encrypted files rather than base64 in a text column.

A pasted screenshot arrived as a `data:image/...;base64,` URL and was stored
verbatim in `og_image`. That cost a third more space than the bytes needed, made
every list query drag megabytes through the connection, and — most importantly —
left the most personal thing in Vault as plain text in the database, while video
went through `save_file_encrypted`.

Images now land in storage under the vault namespace and are encrypted with the
application file key. This is a deliberate trust boundary: sealed collections
protect their *text* against a full database dump, and their images and videos are
protected only against someone who reaches storage without the key volume. Anyone
reading this later should not mistake the file key for a per-collection key.

Existing rows keep their embedded data URL and are still served from it, so nothing
is lost while `externalize_image` moves new writes out of the database.

This module is the single owner of the embedded-image helpers
(`LOCAL_IMAGE_PREFIXES`, `LOCAL_IMAGE_MEDIA_TYPES`, `decode_data_image`):
`services.py` and `router.py` import them from here rather than keeping copies.
"""

import logging

from app.core.storage import get_storage
from app.modules.vault.paths import safe_segment as _safe_segment, storage_root, within_root
from app.modules.vault.schemas import MAX_IMAGE_BYTES

logger = logging.getLogger(__name__)

IMAGE_PREFIX = "vault/images"
IMAGE_MEDIA_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

LOCAL_IMAGE_PREFIXES = (
    "data:image/gif;base64,",
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
    "data:image/webp;base64,",
)
LOCAL_IMAGE_MEDIA_TYPES = {
    "data:image/gif;base64": "image/gif",
    "data:image/jpeg;base64": "image/jpeg",
    "data:image/png;base64": "image/png",
    "data:image/webp;base64": "image/webp",
}


def decode_data_image(value: str | None) -> tuple[bytes, str] | None:
    """Decode an embedded `data:image/...;base64,` URL into raw bytes and media type."""
    import base64
    import binascii

    if not value:
        return None
    header, separator, payload = value.partition(",")
    media_type = LOCAL_IMAGE_MEDIA_TYPES.get(header.lower())
    if not separator or not media_type or len(payload) > (MAX_IMAGE_BYTES * 4 // 3) + 4:
        return None
    try:
        content = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not content or len(content) > MAX_IMAGE_BYTES:
        return None
    return content, media_type


def safe_segment(value: str, fallback: str = "image") -> str:
    """Sanitize a filename segment. Kept here so existing imports keep working."""
    return _safe_segment(value, fallback)


def store_image_bytes(payload: bytes, media_type: str, item_id: int) -> str:
    """Encrypt an image into storage and return its logical path.

    The plaintext never touches disk: the bytes go through the seekable envelope
    from a temporary file, so a large screenshot is not held twice in memory.
    """
    import tempfile

    suffix = IMAGE_MEDIA_TYPES.get(media_type.lower())
    if suffix is None:
        raise ValueError(f"Unsupported image type {media_type!r}")

    destination = storage_root() / IMAGE_PREFIX / f"{item_id}.{suffix}.enc"
    if not within_root(destination, root=storage_root()):
        raise ValueError("Refused a Vault image path outside the storage root")

    destination.parent.mkdir(parents=True, exist_ok=True)
    storage = get_storage()
    with tempfile.NamedTemporaryFile(suffix=".enc") as staging:
        staging.write(payload)
        staging.flush()
        staging.seek(0)
        storage.save_file_encrypted_seekable(staging, str(destination.relative_to(storage_root())))
    return f"{IMAGE_PREFIX}/{item_id}.{suffix}.enc"


def externalize_image(item) -> bool:
    """Move an item's embedded data URL into storage. Returns whether it moved.

    An external URL in `og_image` is left alone: it is a reference, not content.
    """
    decoded = decode_data_image(item.og_image)
    if decoded is None:
        return False
    payload, media_type = decoded
    item.image_path = store_image_bytes(payload, media_type, item.id)
    item.og_image = None
    logger.debug("Vault item %s image moved to %s", item.id, item.image_path)
    return True


def image_bytes(item) -> tuple[bytes, str] | None:
    """The image's bytes and media type, from the file, else from the old column."""
    if item.image_path:
        storage = get_storage()
        try:
            return storage.get_file_decrypted(item.image_path), media_type_for(item.image_path)
        except (FileNotFoundError, ValueError) as error:
            # A missing file must not look like an item without a picture: fall
            # through to the legacy column before giving up.
            logger.warning("Vault item %s image file is unusable: %s", item.id, error)
    return decode_data_image(item.og_image)


def media_type_for(path: str) -> str:
    from pathlib import Path

    name = Path(path).name
    for media_type, suffix in IMAGE_MEDIA_TYPES.items():
        if name.endswith(f".{suffix}.enc"):
            return media_type
    return "image/png"


def has_image(item) -> bool:
    """Whether `/image` can serve this item.

    An external URL does not count: that picture is fetched from someone else's
    host and rendered as `og_image`, so claiming a local image would point the
    tile at an endpoint with nothing behind it.
    """
    return bool(item.image_path) or bool(decode_data_image(item.og_image))
