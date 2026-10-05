"""Sealed offline packages for Vault collections: the snowden-mode producer.

One package per sealed collection (`vault_sealed_<id>`), generated on demand from
an unlocked tab and never stored: the manifest serves the passphrase-wrapped DEK
from its columns, the items endpoint opens every card in RAM and seals the
snapshot under per-URL resource keys, and media endpoints decrypt-then-reseal as
a stream. Nothing here commits: opening mutates rows in memory only, and the
endpoint rolls back before returning so a crash-shaped accident cannot persist
plaintext over sealed columns.

What is deliberately absent: posters and thumbnails (a sealed package has no
previews — rendering one would put decrypted pixels somewhere), media duration
beyond what the sealed payload already holds, and `size`/`sha256` on sealed
resources. Sizes and hashes would have to describe bytes that change with every
fresh nonce; omitting them is the standard's documented legacy fallback, so the
client downloads sealed resources on every refresh and verifies them by
decrypting instead.
"""

import json
import logging
from collections.abc import Iterator

from sqlalchemy import select

from app.core.crypto.transfer import (
    iter_sealed_chunks,
    seal_small_resource,
    transfer_resource_key,
)
from app.core.storage import get_storage
from app.modules.vault.images import media_type_for
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.sealing import (
    SEALED_FIELDS,
    is_sealed_collection,
    open_item,
    sealed_package_id,
)

logger = logging.getLogger(__name__)

# A snapshot bigger than this is refused rather than buffered: the items JSON is
# sealed whole, so an unbounded vault would turn a manifest refresh into an OOM.
# Media never flows through here — it streams per file through its own endpoint.
SEALED_SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024
# Legacy non-chunked media above this is refused: it would have to be held whole
# to re-seal. Chunked media streams; this only ever bites pre-envelope files.
SEALED_LEGACY_MEDIA_MAX_BYTES = 512 * 1024 * 1024


def sealed_items_url(collection_id: int, package_id: str) -> str:
    """The exact manifest URL of a collection's sealed items snapshot.

    The exact string — query included — is also the HKDF info and the envelope
    AAD on both ends. Build it here or not at all.
    """
    return f"/api/vault/sealed/{int(collection_id)}/items?package_id={package_id}"


def sealed_media_url(item_id: int, package_id: str) -> str:
    """The exact manifest URL of one card's sealed media bytes."""
    return f"/api/vault/sealed/media/{int(item_id)}?package_id={package_id}"


async def sealed_package_resources(session, collection: VaultCollection) -> list[dict]:
    """Manifest resource descriptors for one sealed collection. Ciphertext has no
    size or hash here (fresh nonces per generation — see module docstring)."""
    package_id = sealed_package_id(collection.id)
    resources = [
        {
            "url": sealed_items_url(collection.id, package_id),
            "type": "sealed",
            "mime": "application/json",
        }
    ]
    rows = (
        (
            await session.execute(
                select(VaultItem).where(VaultItem.collection_id == collection.id).order_by(VaultItem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    for item in rows:
        if not (item.image_path or item.media_path):
            continue
        if item.media_path and item.media_key_wrap:
            # Blind, not finalized: the bytes are under the download's own item
            # key, so neither the file key here nor the package DEK can open
            # them. The card travels in the snapshot with `has_media` false and
            # rejoins the package after the next unlock finalizes it — shipping
            # a resource nobody can open would be worse than shipping none.
            continue
        mime = item.media_mime or "video/mp4"
        if item.image_path and not item.media_path:
            mime = media_type_for(item.image_path)
        resources.append({"url": sealed_media_url(item.id, package_id), "type": "sealed", "mime": mime})
    return resources


def open_snapshot_item(private_key: bytes, item: VaultItem) -> dict:
    """One card as plain data for the sealed snapshot. In-memory only."""
    opened = open_item(private_key, item)
    fields = {field: getattr(opened, field, None) for field in SEALED_FIELDS}
    og_image = fields.get("og_image")
    if opened.image_path and isinstance(og_image, str) and og_image.startswith("data:"):
        # A blind write not yet externalized carries the picture twice: once as
        # a data URL in the payload, once as a file. The file is the copy the
        # package ships (as its own sealed resource); the duplicate only burns
        # snapshot budget toward the 32 MB ceiling.
        fields["og_image"] = None
    media = None
    if opened.image_path or opened.media_path:
        media = {
            "mime": opened.media_mime,
            "size": opened.media_size,
            "status": opened.media_status,
            "has_image": bool(opened.image_path),
            # A blind video is stored but unopenable until the finalize: report
            # no media rather than a resource the manifest deliberately omits.
            "has_media": bool(opened.media_path) and not opened.media_key_wrap,
        }
    return {
        "id": opened.id,
        "public_title": opened.public_title,
        "fields": fields,
        "media": media,
    }


async def build_sealed_items_plaintext(session, collection: VaultCollection, private_key: bytes) -> bytes:
    """Every sealed card of the collection as one JSON document, in id order."""
    rows = (
        (
            await session.execute(
                select(VaultItem).where(VaultItem.collection_id == collection.id).order_by(VaultItem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    cards = [open_snapshot_item(private_key, item) for item in rows if item.sealed_payload]
    raw = json.dumps({"items": cards}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > SEALED_SNAPSHOT_MAX_BYTES:
        raise ValueError("The sealed snapshot is larger than the offline ceiling")
    return raw


def seal_items_snapshot(dek: bytes, package_id: str, collection_id: int, plaintext: bytes) -> bytes:
    """Seal an items snapshot under its own resource key."""
    url = sealed_items_url(collection_id, package_id)
    return seal_small_resource(transfer_resource_key(dek, package_id, url), url, plaintext)


def iter_sealed_media(dek: bytes, package_id: str, item: VaultItem, file_key: bytes) -> Iterator[bytes]:
    """Yield one card's media re-sealed under its resource key, header first.

    Decrypts under the file key and seals under the resource key chunk by
    chunk: the plaintext exists only in transit. Covers the card's video when
    it has one, else its image. Posters are never served sealed — a sealed
    package has no previews.
    """
    if getattr(item, "media_key_wrap", None):
        raise ValueError("The card's media is blind until the finalize re-seals it")
    storage = get_storage()
    path = item.media_path or item.image_path
    if not path:
        raise ValueError("The card holds no media")
    url = sealed_media_url(item.id, package_id)
    key = transfer_resource_key(dek, package_id, url)
    if storage.is_seekable_encrypted(path):
        total = storage.get_seekable_plaintext_size(path)
        source = storage.read_seekable_range(path, 0, total, key=file_key)
        yield from iter_sealed_chunks(key, url, source, total)
        return
    plaintext = storage.get_file_decrypted(path, key=file_key)
    if len(plaintext) > SEALED_LEGACY_MEDIA_MAX_BYTES:
        raise ValueError("Legacy media is too large to re-seal for offline use")
    yield from iter_sealed_chunks(key, url, iter((plaintext,)), len(plaintext))


def sealed_package_title(collection: VaultCollection) -> str:
    """The manifest title. The alias — never the real name — while locked, and
    the manifest is built unlocked, so the alias is what travels in all cases."""
    return f"Vault (sealed): {collection.public_name or 'Vault'}"


def is_sealed_package_id(package_id: str) -> bool:
    from app.modules.vault.sealing import SEALED_PACKAGE_PREFIX

    return package_id.startswith(SEALED_PACKAGE_PREFIX + "_")


def collection_id_for_package(package_id: str) -> int:
    """The collection a sealed package id names, or ValueError for anything else."""
    from app.modules.vault.sealing import SEALED_PACKAGE_PREFIX

    prefix = SEALED_PACKAGE_PREFIX + "_"
    if not package_id.startswith(prefix):
        raise ValueError(f"Invalid sealed package ID: {package_id!r}")
    remainder = package_id.removeprefix(prefix)
    if not remainder.isdigit() or len(remainder) > 10:
        raise ValueError(f"Invalid sealed package ID: {package_id!r}")
    return int(remainder)


async def require_sealed_collection(session, collection_id: int) -> VaultCollection:
    """The sealed collection a sealed-package request names, else 404 semantics."""
    collection = await session.get(VaultCollection, collection_id)
    if collection is None or not is_sealed_collection(collection):
        raise LookupError(f"Sealed Vault {collection_id} not found")
    return collection


__all__ = [
    "SEALED_LEGACY_MEDIA_MAX_BYTES",
    "SEALED_SNAPSHOT_MAX_BYTES",
    "build_sealed_items_plaintext",
    "collection_id_for_package",
    "is_sealed_package_id",
    "iter_sealed_media",
    "open_snapshot_item",
    "require_sealed_collection",
    "seal_items_snapshot",
    "sealed_items_url",
    "sealed_media_url",
    "sealed_package_resources",
    "sealed_package_title",
]
