"""Sealing and opening Vault items, and holding the unlocked data key.

The data key for a sealed collection lives only in Redis, for as long as the
owner's session has it unlocked. Nothing here writes a plaintext key to the
database, and nothing falls back to "read as empty" when a vault is locked — a
locked vault answers with an error, never with blank rows that look like data.

What stays in the clear is deliberate. The structural columns (which collection,
which node type, pinned, archived, parent, timestamps, and where the file lives)
describe the *shape* of a record and are what the grid needs in order to lay out a
tile at all. Everything the owner wrote — title, body, caption, tags, links, the
embedded picture — moves inside one AEAD blob.
"""

import datetime
import hashlib
import json
import logging
import secrets
from base64 import b64decode, b64encode
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select

from app.core.config import get_settings
from app.modules.vault.crypto import (
    ARGON2_M_COST,
    ARGON2_PARALLELISM,
    ARGON2_T_COST,
    KDF_NAME,
    LEGACY_KDF_NAME,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SealedWrite,
    VaultUnlockError,
    WrappedKey,
    context_for,
    generate_inbox_keypair,
    is_sealed,
    open_from_inbox,
    seal_for_inbox,
    unwrap_data_key,
    wrap_data_key,
)
from app.modules.vault.models import VaultCollection, VaultItem

logger = logging.getLogger(__name__)

redis_client = aioredis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)

# An unlock lives with a browser tab, not with the instance. The token only ever
# exists in the page's memory, so reloading asks for the passphrase again, and a
# token from one tab cannot open the vault in another.
UNLOCK_TTL_SECONDS = 15 * 60

# What a sealed thing is called before anyone has unlocked it.
DEFAULT_SEALED_ALIAS = "Зашифрованный Vault"
DEFAULT_ITEM_ALIAS = "Зашифрованная запись"

# Everything the owner authored. Structural columns are deliberately absent.
SEALED_FIELDS = (
    "title",
    "content",
    "url",
    "og_title",
    "og_description",
    "og_image",
    "tags",
    "canvas_data",
    "category",
    "score",
    "status",
    "media_mime",
)


class VaultLockedError(RuntimeError):
    """The collection is sealed and its key is not available right now."""


def collection_key(collection_id: int, unlock_token: str) -> str:
    """Where the private key sits for one tab.

    The token is hashed rather than embedded: the key name is visible to anything
    that can list Redis, and a leaked key name must not be a usable token.
    """
    digest = hashlib.sha256(unlock_token.encode("utf-8")).hexdigest()[:32]
    return f"vault_key:{collection_id}:{digest}"


def is_sealed_collection(collection: VaultCollection | None) -> bool:
    return bool(collection is not None and collection.is_encrypted)


def item_is_sealed(item: VaultItem) -> bool:
    return is_sealed(item.sealed_payload)


def wrapper_for(collection: VaultCollection) -> WrappedKey:
    """Read back the wrapper, with the cost parameters it was sealed with.

    An empty `key_kdf` means a row written before the column existed. Those were
    all scrypt, so the default here is the legacy name — defaulting to the current
    KDF would derive a different key and report a wrong passphrase for every vault
    created before the upgrade.
    """
    params = collection.key_kdf_params or {}
    kdf = collection.key_kdf or LEGACY_KDF_NAME
    if collection.wrapped_key and not collection.key_kdf and not params:
        # Both columns arrived in one migration and are always written together, so
        # a wrapper with neither name nor cost cannot be read by guessing: the cost
        # is part of what makes the key. Saying "wrong passphrase" here would send
        # the owner hunting for a typo instead of at the row.
        raise VaultUnlockError("This Vault's key wrapper records no derivation cost and cannot be opened")
    return WrappedKey(
        salt=collection.key_salt or "",
        wrapped=collection.wrapped_key or "",
        kdf=kdf,
        t_cost=int(params.get("t_cost", ARGON2_T_COST)),
        m_cost=int(params.get("m_cost", ARGON2_M_COST)),
        parallelism=int(params.get("parallelism", ARGON2_PARALLELISM)),
        n=int(params.get("n", SCRYPT_N)),
        r=int(params.get("r", SCRYPT_R)),
        p=int(params.get("p", SCRYPT_P)),
    )


def store_wrapper(collection: VaultCollection, wrapped: WrappedKey) -> None:
    """Persist a wrapper and the cost parameters that go with it."""
    collection.key_salt = wrapped.salt
    collection.wrapped_key = wrapped.wrapped
    collection.key_kdf = wrapped.kdf
    collection.key_kdf_params = wrapped.params()


async def create_sealed_collection(
    session,
    name: str,
    passphrase: str,
    *,
    description: str | None = None,
    color: str = "teal",
    icon: str | None = None,
    public_name: str | None = None,
    parent_id: int | None = None,
) -> VaultCollection:
    """Create a collection whose contents only its passphrase can open.

    Two keys come out of this. The inbox private key is sealed under the
    passphrase and is what an unlock recovers; the inbox public key stays in the
    clear so a write can be sealed by a client that has never seen the passphrase.

    `parent_id` is honoured here as it is for a plain space. It used to be
    dropped on this path alone, so a sealed space created inside another one
    landed at the top level while a plain one nested — the tree looked broken for
    a reason that only showed up with a passphrase set.
    """
    collection = VaultCollection(
        name=name,
        description=description,
        color=color,
        icon=icon,
        is_encrypted=True,
        public_name=public_name or DEFAULT_SEALED_ALIAS,
        parent_id=parent_id,
    )
    session.add(collection)
    await session.flush()

    private_key, public_key = generate_inbox_keypair()
    wrapped = wrap_data_key(
        private_key,
        passphrase,
        context=context_for("collection", collection.id),
    )
    store_wrapper(collection, wrapped)
    collection.inbox_public_key = b64encode(public_key).decode("ascii")
    await session.commit()
    await session.refresh(collection)

    await unlock_collection(collection, passphrase)
    return collection


async def unlock_collection(
    collection: VaultCollection,
    passphrase: str,
    unlock_token: str = "",
    *,
    session=None,
) -> str:
    """Recover the inbox private key and register it under this tab's token.

    The caller keeps the token; the server keeps the key. The tab's memory is the
    only place the token exists, which is what makes the unlock per-tab. A token
    supplied by the caller is reused, so one tab registers several collections
    under a single token instead of accumulating them.

    Passing `session` also re-wraps the key under the current KDF. The passphrase
    has just been proven correct by the unwrap, so this is the one moment the
    upgrade can happen without asking for anything again — otherwise vaults sealed
    before the change keep their old cost parameters until their owner renames
    them, which is not a thing anyone does.
    """
    private_key = unwrap_data_key(
        wrapper_for(collection),
        passphrase,
        context=context_for("collection", collection.id),
    )
    token = unlock_token or secrets.token_urlsafe(32)
    await redis_client.set(collection_key(collection.id, token), private_key.hex(), ex=UNLOCK_TTL_SECONDS)
    if session is not None:
        await upgrade_wrapper(session, collection, private_key, passphrase)
    return token


async def upgrade_wrapper(session, collection: VaultCollection, private_key: bytes, passphrase: str) -> bool:
    """Re-wrap a key under the current KDF, once the passphrase is known good.

    The wrapper holds a key, not the data: re-wrapping changes how hard the
    passphrase is to guess offline and touches nothing else, so no card is
    re-encrypted and no image is rewritten.

    A failure here must not cost the owner their unlock — they have already proved
    the passphrase — so it is logged and the old wrapper stays put.
    """
    if (collection.key_kdf or LEGACY_KDF_NAME) == KDF_NAME:
        return False
    try:
        store_wrapper(
            collection,
            wrap_data_key(private_key, passphrase, context=context_for("collection", collection.id)),
        )
        await session.commit()
    except Exception:
        logger.exception("Could not upgrade the key wrapper for Vault %s", collection.id)
        await session.rollback()
        return False
    return True


async def lock_collection(collection_id: int, unlock_token: str) -> None:
    """Forget this tab's key. Other tabs keep theirs."""
    if not unlock_token:
        return
    await redis_client.delete(collection_key(collection_id, unlock_token))


async def sealed_collection_ids(session) -> set[int]:
    result = await session.execute(select(VaultCollection.id).where(VaultCollection.is_encrypted.is_(True)))
    return set(result.scalars().all())


async def locked_collection_ids(session, unlock_token: str = "") -> set[int]:
    """Sealed collections this tab cannot open right now.

    Scoped to the token on purpose: another tab having the vault open says nothing
    about whether *this* page may read it.
    """
    sealed = await sealed_collection_ids(session)
    if not sealed:
        return set()
    if not unlock_token:
        return sealed
    locked = set()
    for collection_id in sealed:
        if await redis_client.get(collection_key(collection_id, unlock_token)) is None:
            locked.add(collection_id)
    return locked


async def collection_for(session, collection_id: int | None) -> VaultCollection | None:
    if collection_id is None:
        return None
    return await session.get(VaultCollection, collection_id)


async def data_key_for(collection: VaultCollection | None, unlock_token: str = "") -> bytes | None:
    """This tab's key for a sealed collection, or None while it stays locked."""
    if not is_sealed_collection(collection) or collection is None:
        return None
    if not unlock_token:
        return None
    stored = await redis_client.get(collection_key(collection.id, unlock_token))
    if not stored:
        return None
    # Reading the key is activity: push the expiry out so a tab in use stays open.
    await redis_client.expire(collection_key(collection.id, unlock_token), UNLOCK_TTL_SECONDS)
    try:
        return bytes.fromhex(stored)
    except ValueError:
        logger.warning("Vault %s has a malformed key in the key store", collection.id)
        return None


async def require_data_key(collection: VaultCollection | None, unlock_token: str = "") -> bytes | None:
    """The data key, or a refusal. A sealed vault must never read as empty."""
    if not is_sealed_collection(collection):
        return None
    key = await data_key_for(collection, unlock_token)
    if key is None:
        raise VaultLockedError("This Vault is locked")
    return key


def _payload_for(item: VaultItem) -> dict[str, Any]:
    return {field: getattr(item, field) for field in SEALED_FIELDS if getattr(item, field, None) is not None}


def inbox_public_key(collection: VaultCollection) -> bytes | None:
    """The readable half of a sealed collection's inbox keypair."""
    if not collection.inbox_public_key:
        return None
    try:
        return b64decode(collection.inbox_public_key)
    except Exception:
        logger.warning("Vault %s has a malformed inbox public key", collection.id)
        return None


def _blank_sealed_columns(item: VaultItem) -> None:
    for field in SEALED_FIELDS:
        setattr(item, field, None)
    # There must be no second, readable copy of what the owner wrote. `title`,
    # `tags` and `canvas_data` are NOT NULL, so they are emptied rather than
    # nulled; an empty title is not the content, and relaxing the constraint would
    # have cost every plain item its guarantee as well.
    item.title = ""
    item.tags = []
    item.canvas_data = {}


def seal_item(item: VaultItem, public_key: bytes) -> VaultItem:
    """Seal an item using nothing but the collection's public inbox key.

    This is the blind write: it needs no passphrase and no session key, which is
    what lets the browser extension drop a capture into a locked vault. Every item
    gets its own key, wrapped under the public key, so one compromised item key
    never exposes the rest of the collection.
    """
    payload = json.dumps(_payload_for(item), ensure_ascii=False, separators=(",", ":")).encode()
    write = seal_for_inbox(payload, public_key, context=context_for("item", item.id))
    item.sealed_payload = write.payload
    item.wrapped_key = write.wrapped_key
    _blank_sealed_columns(item)
    return item


def open_item(private_key: bytes, item: VaultItem) -> VaultItem:
    """Restore a sealed item's content from its own wrapped key.

    A payload without its wrapped key is corruption, not an empty item. Returning
    quietly here would show a sealed entry as blank rather than as broken, which is
    exactly the failure this module refuses to have anywhere else.
    """
    if not item.sealed_payload:
        return item
    if not item.wrapped_key:
        raise ValueError(f"Sealed Vault item {item.id} has no wrapped key")
    write = SealedWrite(payload=item.sealed_payload, wrapped_key=item.wrapped_key)
    raw = json.loads(open_from_inbox(write, private_key, context=context_for("item", item.id)).decode())
    for field in SEALED_FIELDS:
        setattr(item, field, raw.get(field))
    item.title = raw.get("title") or ""
    item.tags = raw.get("tags") or []
    item.canvas_data = raw.get("canvas_data") or {}
    # `sealed_payload` and `wrapped_key` deliberately stay in place: they are the
    # state at rest, and dropping them here would let a save write the item back
    # in the clear.
    return item


async def update_sealed_item(
    session, item: VaultItem, update_in, private_key: bytes, public_key: bytes
) -> VaultItem:
    """Apply an update to a sealed item and re-seal it inside a single commit.

    Opening the item, writing the change and sealing it again has to be one
    transaction. Committing the opened columns first would leave a window in
    which the row on disk holds plaintext, and a crash inside that window would
    make the leak permanent.

    The keys are passed in because the caller has already fetched them in order to
    decide whether this Vault is locked; looking them up again would be a second
    round-trip to Redis for nothing.
    """
    open_item(private_key, item)
    for field, value in update_in.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    item.updated_at = datetime.datetime.utcnow()
    seal_item(item, public_key)
    await session.commit()
    await session.refresh(item)
    return item


class VaultMoveError(ValueError):
    """The card cannot change spaces without becoming unopenable."""


async def move_sealed_item(
    session,
    item: VaultItem,
    target: VaultCollection,
    source_private_key: bytes,
) -> VaultItem:
    """Move a sealed card into another sealed collection, keeping it readable.

    The payload is sealed under the *collection's* inbox key, so carrying the row
    across would leave a card whose ciphertext nothing in the new space can open.
    Opening it with the source key and re-sealing under the target's public key is
    the only way it stays readable — which is why this needs the source Vault
    unlocked, exactly like editing a sealed card does.

    A stack travels with its cover. Moving the cover alone left the cards inside it
    sealed under the space the stack had left, and their collection still pointed
    at a cover in the new one.
    """
    if target is None:
        # Reached by filing a sealed card onto "Все карточки". Its payload is
        # sealed under the space it came from, so there is nowhere for it to go.
        raise VaultMoveError("Зашифрованную карточку нельзя убрать из пространства")
    if not target.is_encrypted:
        raise VaultMoveError("Зашифрованную карточку можно перенести только в зашифрованное пространство")
    try:
        key = require_inbox_public_key(target)
    except VaultLockedError as exc:
        # Refused here rather than raised past the endpoint: a sealed space with no
        # inbox key is a space whose owner set it up wrong, not a crash.
        raise VaultMoveError("Это пространство не может принять зашифрованную карточку") from exc
    kids = list(
        (await session.execute(select(VaultItem).where(VaultItem.parent_id == item.id))).scalars().all()
    )
    rows = [item, *kids]
    # Everything is opened before anything moves. A child that cannot be opened
    # aborts the whole move, instead of leaving ciphertext behind in a space whose
    # key no longer has anything to do with it.
    for row in rows:
        if row.sealed_payload:
            open_item(source_private_key, row)
    for row in rows:
        row.collection_id = target.id
        row.updated_at = datetime.datetime.utcnow()
        seal_item(row, key)
    await session.commit()
    await session.refresh(item)
    return item


def require_inbox_public_key(collection: VaultCollection) -> bytes:
    key = inbox_public_key(collection)
    if key is None:
        raise VaultLockedError("This Vault cannot accept a sealed write yet")
    return key


async def open_items(session, items: list[VaultItem], unlock_token: str = "") -> list[VaultItem]:
    """Open every item whose collection is unlocked, in as few key lookups as possible."""
    if not items:
        return items
    collection_ids = {item.collection_id for item in items if item.collection_id is not None}
    keys: dict[int, bytes] = {}
    if collection_ids:
        rows = await session.execute(select(VaultCollection).where(VaultCollection.id.in_(collection_ids)))
        for collection in rows.scalars().all():
            if is_sealed_collection(collection):
                key = await data_key_for(collection, unlock_token)
                if key is not None:
                    keys[collection.id] = key
    for item in items:
        key = keys.get(item.collection_id) if item.sealed_payload else None
        if key is not None:
            open_item(key, item)
    return items
