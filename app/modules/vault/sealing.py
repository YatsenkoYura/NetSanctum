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

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import secrets
import time
from base64 import b64decode, b64encode
from typing import Any

import redis.asyncio as aioredis
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.state_store import state_redis_url
from app.modules.vault.crypto import (
    ARGON2_M_COST,
    ARGON2_M_COST_MAX,
    ARGON2_PARALLELISM,
    ARGON2_PARALLELISM_MAX,
    ARGON2_T_COST,
    ARGON2_T_COST_MAX,
    KDF_NAME,
    LEGACY_KDF_NAME,
    SCRYPT_N,
    SCRYPT_N_MAX,
    SCRYPT_P,
    SCRYPT_P_MAX,
    SCRYPT_R,
    SCRYPT_R_MAX,
    WRAP_VERSION,
    SealedWrite,
    VaultUnlockError,
    WrappedKey,
    check_passphrase_strength,
    context_for,
    derive_file_key,
    derive_subkey,
    generate_inbox_keypair,
    inbox_keypair_matches,
    inbox_pub_fingerprint,
    inbox_pub_mac as crypto_inbox_pub_mac,
    is_sealed,
    kek_for_wrapper,
    open_from_inbox,
    open_inbox_key,
    rewrap_inbox_key,
    seal_for_inbox,
    transfer_kdf_params,
    unwrap_data_key,
    unwrap_with_kek,
    verify_inbox_pub_mac,
    wrap_data_key,
    wrap_package_dek,
)
from app.modules.vault.images import media_type_for, store_image_bytes
from app.modules.vault.models import VaultCollection, VaultItem

logger = logging.getLogger(__name__)

# Unlock sessions, throttles and media epochs live in their own Redis, one
# without persistence: a session record is a vault's data key, and it has no
# business surviving a restart in a file next to the broker's queue. Falls back
# to the general Redis when nothing is configured, which is the old behaviour.
redis_client = aioredis.Redis.from_url(state_redis_url(), decode_responses=True)

# An unlock lives with a browser tab, not with the instance. The token only ever
# exists in the page's memory, so reloading asks for the passphrase again, and a
# token from one tab cannot open the vault in another.
UNLOCK_TTL_SECONDS = 15 * 60


# How many passphrases may be stretched at once. Argon2id at the chosen cost holds
# ~64 MiB per derivation, so this bounds both memory and CPU: without it every
# /unlock request is 64 MiB an attacker can spend, and a handful of concurrent ones
# is a denial of service the application invited.
def _kdf_concurrency() -> int:
    import os

    return max(2, min(8, (os.cpu_count() or 2) * 2))


_KDF_GATE = asyncio.Semaphore(_kdf_concurrency())

# What a sealed thing is called before anyone has unlocked it.
DEFAULT_SEALED_ALIAS = "Зашифрованный Vault"
DEFAULT_ITEM_ALIAS = "Зашифрованная запись"

# Everything the owner authored. Structural columns are deliberately absent.
# A sealed collection's own metadata. `name` is deliberately absent: the sidebar
# shows the alias while the collection is locked, and `public_name` is already a
# deliberate disclosure rather than a leak.
SEALED_COLLECTION_FIELDS = ("description",)

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
    # How far into it the owner is, and how big it turned out to be. These were
    # "structural" on the grounds that the grid does not read them — it reads
    # `has_media` and the status — and that is exactly the problem: an episode
    # counter and a duration tell a reader of the table a good deal about a
    # sealed card without opening anything.
    "progress_current",
    "progress_total",
    "rewatch_count",
    "media_duration",
    "media_width",
    "media_height",
)

# Sealed fields the database will not let go of. `_blank_sealed_columns` zeroes
# these instead of nulling them: `progress_current` and `rewatch_count` are
# NOT NULL, and relaxing that would cost every plain card its guarantee too.
SEALED_ZEROED_FIELDS = {"progress_current": 0, "rewatch_count": 0}

# Deliberately still readable: `media_size` (a length, which says nothing about
# the content), `position` (the grid's own ordering) and `public_title` (the
# alias, which is the only handle a locked vault has).


class VaultLockedError(RuntimeError):
    """The collection is sealed and its key is not available right now."""


def collection_key(collection_id: int, unlock_token: str) -> str:
    """Where the session record sits for one tab.

    Both halves are hashed in: the key name is visible to anything that can list
    Redis, and neither a leaked key name nor a leaked collection id may be a
    usable token. A fresh collection id migrates nothing — the only thing a
    colliding reader gets is somebody else's refusal.
    """
    digest = hashlib.sha256(f"{collection_id}:{unlock_token}".encode()).hexdigest()
    return f"vault_key:{digest}"


SESSION_RECORD_VERSION = 1
# The value lifetime. The sliding part is Redis TTL, refreshed on read; the
# absolute part is `issued_at` inside the record, and it is final: two hours after
# the unlock the tab asks for the passphrase again, however active it was.
SESSION_ABSOLUTE_TTL_SECONDS = 2 * 60 * 60

# Unlock throttling. Argon2id at the chosen cost is ~350ms and 64 MiB per attempt,
# so an unauthenticated endpoint that derives on every request is both a guessing
# oracle and a denial of service. Failures are counted per collection and per IP;
# past the threshold the KDF never runs and the caller gets a 429 instead.
# Sleeping inside the endpoint would hold a worker for the same time for free, so
# the pause is a Retry-After, not a sleep.
UNLOCK_FAIL_PREFIX = "vault_unlock_fail"
UNLOCK_FAIL_TTL_SECONDS = 15 * 60

# Two axes, and they are not the same thing.
#
# The per-host axis throttles one caller: five free attempts, then a pause that
# doubles up to five minutes. The per-vault axis is the global one — it counts
# every failure against the collection from every address, so rotating source
# addresses buys an attacker nothing. Without it the only limit on a distributed
# run is the memory the KDF gate bounds.
#
# The vault's axis is deliberately slower to trip and waits longer, because
# unlike a single host it cannot tell an attack from several people failing at
# once behind the same NAT. Turning a household away because two of them forgot
# a password is the failure mode to avoid, not the one to optimise against.
UNLOCK_HOST_FREE_ATTEMPTS = 5
UNLOCK_HOST_MAX_BACKOFF_SECONDS = 300
UNLOCK_VAULT_FREE_ATTEMPTS = 12
UNLOCK_VAULT_MAX_BACKOFF_SECONDS = 900

# The old names, because the rate-limit tests and anything reading these mean
# them. `coll` is the vault-wide axis, not a per-pair one.
UNLOCK_FREE_ATTEMPTS = UNLOCK_HOST_FREE_ATTEMPTS
UNLOCK_MAX_BACKOFF_SECONDS = UNLOCK_VAULT_MAX_BACKOFF_SECONDS


def _unlock_fail_keys(collection_id: int, client_ip: str) -> tuple[str, str]:
    return (
        f"{UNLOCK_FAIL_PREFIX}:coll:{collection_id}",
        f"{UNLOCK_FAIL_PREFIX}:ip:{client_ip or 'unknown'}",
    )


def _backoff(failures: int, free: int, cap: int) -> int:
    if failures <= free:
        return 0
    return min(cap, 2 ** (failures - free - 1))


async def unlock_backoff_seconds(collection_id: int, client_ip: str) -> int:
    """How long this caller must wait before the KDF may run for them, if at all.

    The worse of the two axes. Read before the derivation, never after: counting
    failures once the KDF has already run would let anyone spend the server's
    memory by guessing.
    """
    vault_key, host_key = _unlock_fail_keys(collection_id, client_ip)
    vault_count, host_count = await redis_client.mget((vault_key, host_key))
    try:
        vault_failures = int(vault_count or 0)
        host_failures = int(host_count or 0)
    except (TypeError, ValueError):
        return 0
    return max(
        _backoff(vault_failures, UNLOCK_VAULT_FREE_ATTEMPTS, UNLOCK_VAULT_MAX_BACKOFF_SECONDS),
        _backoff(host_failures, UNLOCK_HOST_FREE_ATTEMPTS, UNLOCK_HOST_MAX_BACKOFF_SECONDS),
    )


async def record_unlock_failure(collection_id: int, client_ip: str) -> None:
    """Count a failed unlock on both axes, each expiring on its own."""
    for key in _unlock_fail_keys(collection_id, client_ip):
        try:
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, UNLOCK_FAIL_TTL_SECONDS)
        except Exception:
            logger.debug("could not record a vault unlock failure", exc_info=True)


async def clear_unlock_failures(collection_id: int, client_ip: str) -> None:
    """A correct passphrase forgives the failures before it: forgetting a password
    is not an attack, and the counter must not punish it as one."""
    for key in _unlock_fail_keys(collection_id, client_ip):
        try:
            await redis_client.delete(key)
        except Exception:
            logger.debug("could not clear vault unlock failures", exc_info=True)


def _session_record_key(unlock_token: str, collection_id: int) -> bytes:
    """The key that seals one tab's session record. The token is its only secret."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"ns:vault:session:salt:v1",
        info=f"vault-session:{collection_id}:v1".encode(),
    )
    return hkdf.derive(unlock_token.encode("utf-8"))


def _seal_session_record(private_key: bytes, unlock_token: str, collection_id: int, issued_at: int) -> str:
    """Pack the inbox private key so a Redis dump cannot read it.

    A dump used to contain the key in hex, in the clear. The record needs no schema
    migration when this changes: anything that does not parse is treated as absent,
    and the tab simply unlocks again.

    The issue time is bound into the AAD, not just written next to the ciphertext:
    it is what enforces the absolute TTL, so rewriting it must void the record
    rather than extend the session.
    """
    record_key = _session_record_key(unlock_token, collection_id)
    aad = f"{collection_id}:{issued_at}".encode()
    nonce = secrets.token_bytes(12)
    blob = nonce + AESGCM(record_key).encrypt(nonce, private_key, aad)
    return json.dumps(
        {"v": SESSION_RECORD_VERSION, "issued_at": issued_at, "wrap": blob.hex()},
        separators=(",", ":"),
    )


def _open_session_record(record: str, unlock_token: str, collection_id: int) -> tuple[bytes, int] | None:
    """The private key and its issue time, or None for anything unreadable.

    Unreadable covers three cases the caller must not distinguish: a forged record,
    a record sealed for another token, and a record written before this format
    existed. In all three the answer is the same — unlock again.
    """
    try:
        parsed = json.loads(record)
        issued_at = int(parsed["issued_at"])
        raw = bytes.fromhex(parsed["wrap"])
        private_key = AESGCM(_session_record_key(unlock_token, collection_id)).decrypt(
            raw[:12], raw[12:], f"{collection_id}:{issued_at}".encode()
        )
        return private_key, issued_at
    except Exception:
        return None


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

    Cost parameters are capped before anything derives from them: they come from
    the database, and a row claiming gigabytes of Argon2 memory must be refused
    before the KDF allocates, not after.
    """
    params = collection.key_kdf_params or {}
    kdf = collection.key_kdf or LEGACY_KDF_NAME
    if collection.wrapped_key and not collection.key_kdf and not params:
        # Both columns arrived in one migration and are always written together, so
        # a wrapper with neither name nor cost cannot be read by guessing: the cost
        # is part of what makes the key. Saying "wrong passphrase" here would send
        # the owner hunting for a typo instead of at the row.
        raise VaultUnlockError("This Vault's key wrapper records no derivation cost and cannot be opened")
    try:
        t_cost = int(params.get("t_cost", ARGON2_T_COST))
        m_cost = int(params.get("m_cost", ARGON2_M_COST))
        parallelism = int(params.get("parallelism", ARGON2_PARALLELISM))
        n = int(params.get("n", SCRYPT_N))
        r = int(params.get("r", SCRYPT_R))
        p = int(params.get("p", SCRYPT_P))
        wrap_version = int(params.get("wrap", 1))
    except (TypeError, ValueError) as error:
        raise VaultUnlockError("This Vault's key wrapper records an unreadable cost") from error
    if kdf == LEGACY_KDF_NAME:
        if not (2**8 <= n <= SCRYPT_N_MAX) or n & (n - 1) or not (1 <= r <= SCRYPT_R_MAX):
            raise VaultUnlockError("This Vault's key wrapper records an unsafe cost")
        if not (1 <= p <= SCRYPT_P_MAX):
            raise VaultUnlockError("This Vault's key wrapper records an unsafe cost")
    elif kdf == KDF_NAME:
        if not (8 <= m_cost <= ARGON2_M_COST_MAX):
            raise VaultUnlockError("This Vault's key wrapper records an unsafe cost")
        if not (1 <= t_cost <= ARGON2_T_COST_MAX) or not (1 <= parallelism <= ARGON2_PARALLELISM_MAX):
            raise VaultUnlockError("This Vault's key wrapper records an unsafe cost")
    else:
        raise VaultUnlockError(f"Unsupported key derivation {kdf!r}")
    return WrappedKey(
        salt=collection.key_salt or "",
        wrapped=collection.wrapped_key or "",
        kdf=kdf,
        wrap_version=wrap_version,
        t_cost=t_cost,
        m_cost=m_cost,
        parallelism=parallelism,
        n=n,
        r=r,
        p=p,
    )


def store_wrapper(collection: VaultCollection, wrapped: WrappedKey) -> None:
    """Persist a wrapper and the cost parameters that go with it.

    The envelope version travels inside `key_kdf_params` rather than a new column:
    it is a property of how the wrapper was sealed, exactly like the cost, and rows
    written before it existed simply carry no version — which reads as v1.
    """
    collection.key_salt = wrapped.salt
    collection.wrapped_key = wrapped.wrapped
    collection.key_kdf = wrapped.kdf
    collection.key_kdf_params = {**wrapped.params(), "wrap": wrapped.wrap_version}


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
    # There is no recovery for a sealed vault, so the strength check runs here,
    # where the passphrase is chosen — never at unlock, where it would lock the
    # owner out of a vault they already have.
    check_passphrase_strength(passphrase)
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
    # Bind the public key to the passphrase while the KEK is in hand. Recomputing
    # it later needs the KEK again, which is exactly what an unlock recovers.
    kek, _salt = kek_for_wrapper(wrapped, passphrase)
    collection.inbox_pub_mac = crypto_inbox_pub_mac(kek, public_key, collection.id)
    # The sealed offline package wrapper is stored from the start: the passphrase
    # is here now, and a manifest served later must not need it again.
    refresh_package_wrap(collection, private_key, passphrase)
    # No file-key wrapping here: the file key is derived from the private key,
    # so there is nothing to store and no backfill for older rows.
    # The description goes in under the public key too, so creating a sealed
    # collection never needs its own private key in memory.
    seal_collection_fields(collection, public_key)
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
    wrapped = wrapper_for(collection)
    context = context_for("collection", collection.id)
    # The derivation is CPU-bound and holds tens of megabytes: it runs off the
    # event loop, and the gate keeps concurrent unlocks from multiplying that.
    async with _KDF_GATE:
        kek, salt = await asyncio.to_thread(kek_for_wrapper, wrapped, passphrase)
        private_key = unwrap_with_kek(wrapped, kek, salt, context)
    # The passphrase just proved itself, so a public key that does not derive
    # from this private key is substitution, not a typo: refuse as an attack.
    stored_public = inbox_public_key(collection)
    if stored_public is None:
        raise VaultUnlockError("This Vault has no inbox key")
    if not inbox_keypair_matches(private_key, stored_public):
        logger.warning(
            "Vault %s inbox public key does not derive from its private key: refusing unlock",
            collection.id,
        )
        raise VaultUnlockError("The Vault's inbox key does not match its seal")
    await verify_collection_key(collection, kek, session)
    token = unlock_token or secrets.token_urlsafe(32)
    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    await redis_client.set(
        collection_key(collection.id, token),
        _seal_session_record(private_key, token, collection.id, now),
        ex=UNLOCK_TTL_SECONDS,
    )
    if session is not None:
        await upgrade_wrapper(session, collection, private_key, passphrase)
        try:
            # Rows from before the sealed-package columns fill their wrapper here,
            # where the passphrase is in hand. A failure only logs: the unlock
            # already succeeded, and the manifest will ask for one more unlock.
            if not collection.sealed_pkg_salt or not collection.sealed_pkg_wrapped:
                refresh_package_wrap(collection, private_key, passphrase)
                await session.commit()
        except Exception:
            logger.warning(
                "Vault %s sealed package wrapper could not be stored", collection.id, exc_info=True
            )
            try:
                await session.rollback()
            except Exception:
                pass
        await heal_collection_images(session, collection, derive_file_key(private_key, collection.id))
        collection_public_key = inbox_public_key(collection)
        if collection_public_key is not None:
            await seal_collection_plaintext(session, collection, collection_public_key)
        await autostart_pending_downloads(session, collection, token, private_key)
    return token


# How many waiting cards one unlock may start, hard. A setting may lower this and
# nothing may raise it: the point of the cap is that an unlock is bounded even
# when a vault has a hundred waiting videos.
PENDING_AUTOSTART_HARD_LIMIT = 3


async def autostart_pending_downloads(
    session, collection: VaultCollection, unlock_token: str, private_key: bytes
) -> int:
    """Start waiting video downloads for this vault, up to the cap. Off by default.

    Only ever called from an unlock, which is why it is bounded and opt-in: the
    unlock has the token these downloads need, and it is the one moment the owner
    is demonstrably present. Each card's address comes out of the sealed payload,
    which the key in hand opens, and the token goes back to the worker inside the
    sealed handoff — never in the task arguments, which the broker keeps in a
    Redis with an AOF on.

    The opened columns are rolled back afterwards: this function reads a sealed
    card, and a commit here would write it back in the clear.
    """

    settings = get_settings()
    if not settings.VAULT_PENDING_AUTOSTART or not unlock_token:
        return 0
    limit = min(int(settings.VAULT_PENDING_AUTOSTART_LIMIT), PENDING_AUTOSTART_HARD_LIMIT)
    if limit < 1:
        return 0
    try:
        rows = list(
            (
                await session.execute(
                    select(VaultItem)
                    .where(
                        VaultItem.collection_id == collection.id,
                        VaultItem.media_status == "pending_unlock",
                    )
                    .order_by(VaultItem.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.debug("could not list waiting downloads for Vault %s", collection.id, exc_info=True)
        return 0

    started = 0
    for item in rows:
        url = item.url
        if item.sealed_payload:
            # The address lives in the payload, so reading it is what this unlock
            # is for. A card that cannot be opened is skipped, not guessed at.
            try:
                open_item(private_key, item)
                url = item.url
            except Exception:
                logger.warning("could not read the address of waiting Vault item %s", item.id, exc_info=True)
                continue
        if not url:
            continue
        from app.modules.vault.services import queue_video_download

        task_id = await queue_video_download(
            session, item.id, str(url), title=item.title or None, unlock_token=unlock_token
        )
        if task_id:
            started += 1
    # Whatever was opened above is dropped rather than written back.
    if started:
        try:
            await session.rollback()
        except Exception:
            logger.debug("could not roll back the pending-download scan", exc_info=True)
    return started


async def file_key_for(collection: VaultCollection | None, unlock_token: str = "") -> bytes | None:
    """This tab's file key for a sealed collection, or None while it stays locked.

    Derived from the session's data key, never stored: there is no column, no
    backfill and no "missing key" state, because derivation cannot be missing.
    Every sealed row yields its file key the moment it is unlocked, including
    rows written before the file key existed.
    """
    if collection is None:
        return None
    private_key = await data_key_for(collection, unlock_token)
    if private_key is None:
        return None
    return derive_file_key(private_key, collection.id)


async def file_key_for_write(
    collection: VaultCollection, unlock_token: str = ""
) -> tuple[bytes | None, bool]:
    """The file key for writing, and whether the collection is locked."""
    private_key = await data_key_for(collection, unlock_token)
    if private_key is None:
        return None, True
    return derive_file_key(private_key, collection.id), False


# ── Short-lived file URLs ──────────────────────────────────────────────
# `<video>` cannot send the unlock header, and neither can `<img>` without
# giving up caching and seeking. Images, posters and previews go through an
# authorized `fetch()` into a blob URL — the page holds the token, so the
# header goes along. The player cannot do that and still seek, so it gets a
# URL that carries its own authorization.
#
# The signature binds the collection, the item, the stored path, the expiry
# and the collection's media epoch, under a server key. The epoch is the
# implementable form of "bound to the unlock token": the token itself cannot
# be checked statelessly — it never travels in the player request — but every
# lock bumps the epoch, so locking the vault kills every player URL it ever
# issued. Bytes already in flight cannot be unsent, but the next Range
# request fails and playback freezes at the end of the buffer.
#
# Fifteen minutes, one file, media only: images need the session-side file
# key, which no URL can carry, so there is no signed form of them.

FILE_URL_TTL_SECONDS = 15 * 60
FILE_URL_KINDS = ("media",)


def media_epoch_key(collection_id: int) -> str:
    return f"vault_media_epoch:{collection_id}"


async def media_epoch(collection_id: int | None) -> int:
    """The collection's current media epoch. Missing Redis reads as zero."""
    if collection_id is None:
        return 0
    try:
        raw = await redis_client.get(media_epoch_key(collection_id))
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0
    except Exception:
        logger.debug("could not read the media epoch for Vault %s", collection_id, exc_info=True)
        return 0


async def bump_media_epoch(collection_id: int | None) -> None:
    """Invalidate every player URL ever issued for this collection."""
    if collection_id is None:
        return
    try:
        await redis_client.incr(media_epoch_key(collection_id))
    except Exception:
        # The lock itself must still succeed: the residual is bounded by the
        # fifteen-minute signature expiry, and the log says it happened.
        logger.warning("could not bump the media epoch for Vault %s", collection_id, exc_info=True)


def media_signing_key() -> bytes:
    """The key that signs player URLs, derived off the application file key."""
    from app.core.encryption_keys import primary_encryption_key

    return hmac.new(primary_encryption_key(), b"netsanctum:vault:media-url:v1", hashlib.sha256).digest()


def sign_file_url(
    collection_id: int | None, item_id: int, kind: str, path: str, epoch: int, *, now: int | None = None
) -> tuple[int, str]:
    """An expiry and a signature for one player URL."""
    if kind not in FILE_URL_KINDS:
        raise ValueError(f"Cannot sign a Vault file URL for {kind!r}")
    if collection_id is None or not path:
        raise ValueError("A signed Vault file URL needs a collection and a path")
    expires = int(now if now is not None else time.time()) + FILE_URL_TTL_SECONDS
    tag = hmac.new(
        media_signing_key(),
        f"{collection_id}:{item_id}:{kind}:{path}:{expires}:{epoch}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return expires, tag


def verify_file_url_signature(
    collection_id: int | None,
    item_id: int,
    kind: str,
    path: str,
    expires: str | None,
    signature: str | None,
    epoch: int,
) -> bool:
    """Whether a player URL's signature is genuine and still alive. Pure:
    the caller supplies the epoch it read, so this stays testable without Redis."""
    if kind not in FILE_URL_KINDS or not expires or not signature:
        return False
    try:
        if int(expires) < int(time.time()):
            return False
        epoch = int(epoch)
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        media_signing_key(),
        f"{collection_id}:{item_id}:{kind}:{path}:{expires}:{epoch}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def verify_file_url(
    collection_id: int | None,
    item_id: int,
    kind: str,
    path: str,
    expires: str | None,
    signature: str | None,
) -> bool:
    """A player URL against the live epoch. A lock since minting voids it.

    Redis failing here fails closed: the sessions this protects live in Redis
    too, so a sealed video is unusable without it anyway — and failing open
    would trade an outage for an open vault.
    """
    if collection_id is None or not path or not expires or not signature:
        return False
    try:
        live = await redis_client.get(media_epoch_key(collection_id))
        epoch = int(live or 0)
    except Exception:
        logger.debug("could not read the media epoch for Vault %s", collection_id, exc_info=True)
        return False
    return verify_file_url_signature(collection_id, item_id, kind, path, expires, signature, epoch)


# ── Download handoff token ─────────────────────────────────────────────
# The video worker has no vault key, but a download queued from an unlocked
# tab can lend it one: the tab's unlock token, sealed under the server key
# and placed in the one-shot handoff next to the url and the title. The task
# arguments carry only the handoff id — the broker is the same Redis that
# keeps an AOF on disk, so a token in the arguments would be a session on
# disk. The handoff id is already the only thing a queued task names.
HANDOFF_TOKEN_AAD_PREFIX = b"vault-handoff"


def seal_handoff_token(handoff_id: str, unlock_token: str) -> str:
    """Seal a tab's unlock token for the download worker.

    The box is bound to its handoff id: a box lifted from one handoff does
    not open under another's. Fifteen hundred seconds of life at most — the
    handoff TTL decides, not this function.
    """
    nonce = secrets.token_bytes(12)
    blob = nonce + AESGCM(media_signing_key()).encrypt(
        nonce,
        unlock_token.encode("utf-8"),
        HANDOFF_TOKEN_AAD_PREFIX + b":" + handoff_id.encode("utf-8"),
    )
    return b64encode(blob).decode("ascii")


def open_handoff_token(handoff_id: str, box: str | None) -> str | None:
    """Recover the token, or None for anything unreadable.

    None is the worker's normal fallback, not an error: an expired handoff, a
    rotated server key, or a capture queued with no token all mean the video
    is stored under the application key instead, access-gated like the rest.
    """
    if not handoff_id or not box:
        return None
    try:
        # `validate=True`: base64 decoding silently drops characters outside the
        # alphabet, so without it a tampered box can decode back to the original
        # plaintext. The AEAD tag would still catch a real forgery, but a box
        # that survives editing is a box nobody can reason about.
        raw = b64decode(box.encode("ascii"), validate=True)
        if len(raw) <= 12:
            return None
        return (
            AESGCM(media_signing_key())
            .decrypt(raw[:12], raw[12:], HANDOFF_TOKEN_AAD_PREFIX + b":" + handoff_id.encode("utf-8"))
            .decode("utf-8")
        )
    except Exception:
        return None


# How many pre-file-key images one unlock moves over. Every one is small
# (bounded by the image size cap), but an unlock is not the place for an
# unbounded migration — the rest converge on later unlocks.
HEAL_BATCH_LIMIT = 25


async def heal_collection_images(session, collection: VaultCollection, file_key: bytes) -> int:
    """Move pre-file-key images onto the file key, once each.

    Images written before the file key used the shared application key under the
    old deterministic name (`{item_id}.{suffix}.enc`). They are small — bounded
    by the image size cap — so re-encrypting them on unlock is cheap, and the
    rename to the random pattern means a healed file is never touched again:
    the next unlock finds nothing matching the old pattern and does nothing.
    Videos and worker posters are excluded on purpose: the keyless worker owns
    them, and rewriting gigabytes on unlock would punish opening the vault.

    A file that no longer opens is left in place and logged. Deleting it would
    destroy the only copy on a guess, and the read path still tries the
    application key afterwards, so a skipped file keeps serving as it did.

    At most HEAL_BATCH_LIMIT files per unlock: every one is small, but an
    unlock is not the place for an unbounded migration. The rest converge on
    later unlocks — healed files never match the old pattern again.
    """
    import re

    from app.core.storage import get_storage

    if not is_sealed_collection(collection):
        return 0
    try:
        rows = (
            (
                await session.execute(
                    select(VaultItem)
                    .where(
                        VaultItem.collection_id == collection.id,
                        VaultItem.image_path.is_not(None),
                    )
                    .order_by(VaultItem.id.asc())
                    .limit(HEAL_BATCH_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.debug("could not list Vault %s images for healing", collection.id, exc_info=True)
        return 0
    storage = get_storage()
    healed = 0
    for item in rows:
        old_path = item.image_path or ""
        name = old_path.rsplit("/", 1)[-1]
        if re.fullmatch(rf"{item.id}-[0-9a-f]{{16}}\.[a-z0-9]+\.enc", name):
            continue
        try:
            plaintext = await asyncio.to_thread(storage.get_file_decrypted, old_path)
            new_path = await asyncio.to_thread(
                store_image_bytes,
                plaintext,
                media_type_for(old_path),
                item.id,
                key=file_key,
                sealed=True,
            )
        except Exception:
            logger.warning("Vault item %s image could not move to the file key", item.id, exc_info=True)
            continue
        try:
            item.image_path = new_path
            await session.commit()
        except Exception:
            logger.warning("Vault item %s healed image could not be recorded", item.id, exc_info=True)
            try:
                await session.rollback()
            except Exception:
                pass
            continue
        try:
            await asyncio.to_thread(storage.delete_file, old_path)
        except Exception:
            logger.debug("stale Vault image %s could not be removed", old_path, exc_info=True)
        healed += 1
    return healed


def verify_space_passphrase(collection: VaultCollection, passphrase: str) -> None:
    """Whether this passphrase opens this space. Synchronous: Argon2id.

    Not "is a passphrase present" and not "does the wrapper exist" — the whole
    point is that destroying a sealed space is irreversible, so the proof has to
    be the one the unlock makes: unwrap the private key at the stored cost, check
    the stored public key derives from it, and check the MAC. A passphrase that
    is merely accepted would make the prompt a speed bump rather than a guard.
    Runs on a worker thread by its callers: the derivation blocks and holds tens
    of megabytes.
    """
    wrapped = wrapper_for(collection)
    private_key = unwrap_data_key(wrapped, passphrase, context=context_for("collection", collection.id))
    public_key = inbox_public_key(collection)
    if public_key is None:
        raise VaultUnlockError("This Vault has no inbox key")
    if not inbox_keypair_matches(private_key, public_key):
        logger.warning("Vault %s inbox public key does not derive from its private key", collection.id)
        raise VaultUnlockError("The Vault's inbox key does not match its seal")
    kek, _salt = kek_for_wrapper(wrapped, passphrase)
    if not verify_inbox_pub_mac(kek, public_key, collection.id, collection.inbox_pub_mac):
        raise VaultUnlockError("Wrong passphrase for this Vault")


async def verify_collection_key(collection: VaultCollection, kek: bytes, session=None) -> None:
    """Check the inbox public key's MAC against the passphrase, or refuse.

    The private-to-public correspondence is checked by the caller, which holds
    the freshly unwrapped private key; this checks the MAC second. A rewritten
    public key diverts every blind write without touching the wrapper, so both
    checks must pass — a mismatch is logged as an attack, not as a wrong
    passphrase, because the passphrase just proved itself correct. A missing MAC
    on an old row is backfilled only here, only after the correspondence check
    already passed in `unlock_collection`.
    """
    stored = collection.inbox_pub_mac
    public_key = inbox_public_key(collection)
    if public_key is None:
        raise VaultUnlockError("This Vault has no inbox key")
    if stored:
        if verify_inbox_pub_mac(kek, public_key, collection.id, stored):
            return
        logger.warning(
            "Vault %s inbox public key does not match its MAC: refusing unlock",
            collection.id,
        )
        raise VaultUnlockError("The Vault's inbox key does not match its seal")
    if session is not None:
        collection.inbox_pub_mac = crypto_inbox_pub_mac(kek, public_key, collection.id)
        await session.commit()


async def seal_collection_plaintext(session, collection: VaultCollection, public_key: bytes) -> bool:
    """Move a collection's plaintext metadata into its payload, in place.

    Collections created before the payload existed keep their description in the
    clear, and there is no migration that could fix it: sealing needs the key, and
    the key only exists while the owner has typed the passphrase. So the unlock path
    does it, where the key has just been recovered anyway.
    """
    if not is_sealed_collection(collection) or collection.sealed_payload:
        return False
    if not any(getattr(collection, field, None) for field in SEALED_COLLECTION_FIELDS):
        return False
    seal_collection_fields(collection, public_key)
    await session.commit()
    return True


async def upgrade_wrapper(session, collection: VaultCollection, private_key: bytes, passphrase: str) -> bool:
    """Re-wrap a key under the current KDF, once the passphrase is known good.

    The wrapper holds a key, not the data: re-wrapping changes how hard the
    passphrase is to guess offline and touches nothing else, so no card is
    re-encrypted and no image is rewritten.

    A failure here must not cost the owner their unlock — they have already proved
    the passphrase — so it is logged and the old wrapper stays put.

    The write is compare-and-swap on the salt the unlock read: two tabs unlocking
    the same vault at once both derive, both write, and the loser's commit would
    otherwise resurrect the old cost over the winner's new one. Losing this race
    is the correct outcome, and it is silent — the row already says what it needs.
    """
    params = collection.key_kdf_params or {}
    if (collection.key_kdf or LEGACY_KDF_NAME) == KDF_NAME and int(params.get("wrap", 1)) >= WRAP_VERSION:
        return False
    try:
        wrapped = wrap_data_key(private_key, passphrase, context=context_for("collection", collection.id))
        old_salt = collection.key_salt
        result = await session.execute(
            update(VaultCollection)
            .where(VaultCollection.id == collection.id, VaultCollection.key_salt == old_salt)
            .values(
                key_salt=wrapped.salt,
                wrapped_key=wrapped.wrapped,
                key_kdf=wrapped.kdf,
                key_kdf_params={**wrapped.params(), "wrap": wrapped.wrap_version},
            )
        )
        rowcount = getattr(result, "rowcount", 1)
        if isinstance(rowcount, int) and rowcount < 1:
            await session.rollback()
            return False
        await session.commit()
        # The row changed through the UPDATE, not through the object: sync it, so a
        # caller holding the collection sees what the database now says. Only after
        # the commit — on failure the old values must stay put.
        store_wrapper(collection, wrapped)
        await session.refresh(collection)
    except Exception:
        logger.exception("Could not upgrade the key wrapper for Vault %s", collection.id)
        await session.rollback()
        return False
    return True


# ── Sealed offline package ───────────────────────────────────────────
# One sealed package per sealed collection: `vault_sealed_<id>`, opened with the
# collection's own passphrase on the desktop client. The package DEK is derived
# from the inbox private key — never stored, never transmitted — while the
# passphrase-wrapped copy below is what the manifest serves. It is public the
# same way the vault wrapper is public: useless without the passphrase.
#
# The wrap is written where the passphrase is in hand: creation, unlock (when
# missing) and rekey (the DEK follows the private key, so a rekey retires it).
# The manifest never wraps on demand — serving it needs only the unlock token,
# and the token proves a session, not the passphrase.

SEALED_PACKAGE_PREFIX = "vault_sealed"
PACKAGE_DEK_INFO = b"ns:sealed-pkg:dek:v1"


def sealed_package_id(collection_id: int) -> str:
    """The offline package id for one sealed collection."""
    return f"{SEALED_PACKAGE_PREFIX}_{int(collection_id)}"


def package_dek_for(private_key: bytes, collection_id: int) -> bytes:
    """The collection's package DEK. Deterministic, 32 bytes, unique per collection."""
    if len(private_key) != 32:
        raise ValueError("A data key must be 32 bytes")
    return derive_subkey(
        private_key,
        salt=f"ns:sealed-pkg:{int(collection_id)}".encode(),
        info=PACKAGE_DEK_INFO,
        length=32,
    )


def refresh_package_wrap(collection: VaultCollection, private_key: bytes, passphrase: str) -> dict:
    """Wrap the package DEK under the passphrase and store the fragment.

    The caller commits: creation and rekey fold this into their own commit,
    unlock commits separately. Returns the manifest `sealing` fragment.
    """
    dek = package_dek_for(private_key, collection.id)
    fragment = wrap_package_dek(dek, passphrase, sealed_package_id(collection.id))
    collection.sealed_pkg_salt = fragment["salt"]
    collection.sealed_pkg_wrapped = fragment["wrapped_dek"]
    collection.sealed_pkg_kdf = fragment["kdf"]
    sealing = sealed_package_sealing(collection)
    assert sealing is not None  # all three columns were just stored above
    return sealing


def sealed_package_sealing(collection: VaultCollection) -> dict | None:
    """The manifest `sealing` object, or None when no unlock has stored one yet."""
    if not is_sealed_collection(collection):
        return None
    if not collection.sealed_pkg_salt or not collection.sealed_pkg_wrapped:
        return None
    kdf = dict(collection.sealed_pkg_kdf or {})
    if kdf.get("algorithm", "argon2id") != "argon2id":
        return None
    return {
        "version": 1,
        "kdf": {
            "algorithm": "argon2id",
            **transfer_kdf_params(),
            **{k: kdf[k] for k in ("m_cost", "t_cost", "parallelism") if k in kdf},
        },
        "salt": collection.sealed_pkg_salt,
        "wrapped_dek": collection.sealed_pkg_wrapped,
    }


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


async def data_key_for(
    collection: VaultCollection | None, unlock_token: str = "", *, touch: bool = True
) -> bytes | None:
    """This tab's key for a sealed collection, or None while it stays locked."""
    if not is_sealed_collection(collection) or collection is None:
        return None
    if not unlock_token:
        return None
    stored = await redis_client.get(collection_key(collection.id, unlock_token))
    if not stored:
        return None
    opened = _open_session_record(stored, unlock_token, collection.id)
    if opened is None:
        return None
    private_key, issued_at = opened
    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    if now - issued_at > SESSION_ABSOLUTE_TTL_SECONDS:
        # The sliding TTL kept refreshing, so Redis would otherwise hold this open
        # forever. The record dies here and the tab asks for the passphrase again.
        await redis_client.delete(collection_key(collection.id, unlock_token))
        return None
    # Reading the key is activity: push the expiry out so a tab in use stays open.
    # A background worker resolving the key is not user activity — it must not
    # keep the vault unlocked past the owner walking away, so it reads untouched.
    if touch:
        await redis_client.expire(collection_key(collection.id, unlock_token), UNLOCK_TTL_SECONDS)
    return private_key


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
        setattr(item, field, SEALED_ZEROED_FIELDS.get(field))
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

    Always the current envelope. A row that arrived under the previous one is
    upgraded the next time it is written — by an edit, a move, or being filed
    somewhere else — and untouched rows stay readable, so nothing has to walk
    the table to move it off a version nobody should still be writing.
    """
    payload = json.dumps(_payload_for(item), ensure_ascii=False, separators=(",", ":")).encode()
    write = seal_for_inbox(
        payload,
        public_key,
        collection_id=item.collection_id,
        kind="item",
        row_id=item.id,
    )
    item.sealed_payload = write.payload
    item.wrapped_key = write.wrapped_key
    _blank_sealed_columns(item)
    return item


def seal_collection_fields(collection: VaultCollection, public_key: bytes) -> VaultCollection:
    """Seal a collection's own metadata under its public inbox key.

    The same blind write an item gets: no passphrase, no session key, so a sealed
    collection can be created without ever holding its own private key in memory.
    """
    payload = {
        field: getattr(collection, field)
        for field in SEALED_COLLECTION_FIELDS
        if getattr(collection, field, None) is not None
    }
    if not payload:
        return collection
    write = seal_for_inbox(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        public_key,
        collection_id=collection.id,
        kind="collection",
        row_id=collection.id,
    )
    collection.sealed_payload = write.payload
    collection.sealed_wrapped_key = write.wrapped_key
    for field in SEALED_COLLECTION_FIELDS:
        setattr(collection, field, "" if getattr(collection, field, None) is not None else None)
    return collection


def open_collection_fields(collection: VaultCollection, private_key: bytes) -> dict:
    """Recover a sealed collection's own metadata, or nothing if it has none."""
    if not collection.sealed_payload:
        return {}
    write = SealedWrite(payload=collection.sealed_payload, wrapped_key=collection.sealed_wrapped_key)
    if not write.wrapped_key:
        return {}
    raw = open_from_inbox(
        write, private_key, collection_id=collection.id, kind="collection", row_id=collection.id
    )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}


async def open_collection_payloads(collections, unlock_token: str) -> dict[int, dict]:
    """Open every unlocked sealed collection's own metadata, in one pass.

    One key lookup for the whole sidebar rather than a round trip per row, the same
    bargain `open_items` makes for a board of cards. Session records are sealed
    under the tab token (see `_seal_session_record`), so each is opened with the
    token — a hex decode here would only ever fit the pre-seal format and treat
    every current session as malformed.
    """
    wanted = [c for c in collections if is_sealed_collection(c) and c.sealed_payload]
    if not wanted or not unlock_token:
        return {}
    keys: dict[int, bytes] = {}
    rows = await redis_client.mget([collection_key(c.id, unlock_token) for c in wanted])
    for collection, stored in zip(wanted, rows, strict=True):
        if not stored:
            continue
        opened = _open_session_record(stored, unlock_token, collection.id)
        if opened is None:
            continue
        keys[collection.id] = opened[0]
    return {
        collection.id: open_collection_fields(collection, keys[collection.id])
        for collection in wanted
        if collection.id in keys
    }


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
    raw = json.loads(
        open_from_inbox(
            write, private_key, collection_id=item.collection_id, kind="item", row_id=item.id
        ).decode()
    )
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
    session,
    item: VaultItem,
    update_in,
    private_key: bytes,
    public_key: bytes,
    *,
    file_key: bytes | None = None,
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
    if file_key is not None:
        # A blind write keeps its picture as a data URL inside the sealed
        # payload — the locked vault has no file key to store it under. The
        # first save while unlocked moves it out into storage, so the payload
        # stops carrying megabytes and the file gains the file key. Without
        # this the payload images never leave the row they arrived in.
        from app.modules.vault.images import decode_data_image, externalize_image

        if decode_data_image(item.og_image):
            old_path = item.image_path
            if externalize_image(item, key=file_key, sealed=True):
                if old_path and old_path != item.image_path:
                    try:
                        from app.core.storage import get_storage

                        get_storage().delete_file(old_path)
                    except Exception:
                        logger.debug("stale Vault image %s could not be removed", old_path, exc_info=True)
    seal_item(item, public_key)
    await session.commit()
    await session.refresh(item)
    return item


async def increment_sealed_progress(
    session,
    item: VaultItem,
    private_key: bytes,
    public_key: bytes,
    step: int = 1,
) -> VaultItem:
    """Advance a sealed card's episode counter inside its payload.

    Progress is a sealed field: the column on disk is zeroed and the real value
    lives in the envelope. Writing the column directly (what the plain path
    does) would be overwritten by the next unlock, so this opens the item,
    increments in memory with the same status transitions, and re-seals in one
    commit.
    """
    open_item(private_key, item)
    item.progress_current = (item.progress_current or 0) + step
    if item.progress_total and item.progress_current >= item.progress_total:
        item.status = "completed"
    elif item.status in (None, "planned"):
        item.status = "watching"
    item.updated_at = datetime.datetime.now(datetime.UTC)
    seal_item(item, public_key)
    await session.commit()
    await session.refresh(item)
    return item


class VaultMoveError(ValueError):
    """The card cannot change spaces without becoming unopenable."""


class VaultRekeyError(ValueError):
    """The collection's inbox key could not be replaced."""


async def rekey_collection(
    session,
    collection: VaultCollection,
    passphrase: str,
    *,
    unlock_token: str = "",
) -> dict[str, Any]:
    """Replace a collection's inbox keypair, and re-wrap everything under it.

    A separate operation, never a side effect: rotating a key because one may
    have leaked is a decision with a cost, and it must not ride along on an
    unlock or a save. What it does:

    * a fresh X25519 keypair, whose public half is published for blind writes
      and whose private half is wrapped by the passphrase, exactly as at
      creation;
    * the public key's MAC recomputed, since the old one vouched for the old key
      — leaving it would make the next unlock refuse the vault just rekeyed;
    * every sealed row re-wrapped: its item key is recovered with the old
      private key and wrapped again under the new public key. No payload is
      decrypted and no payload is rewritten — only the small wrapper around each
      item key changes, so this is minutes of work rather than a rewrite.

    What it cannot do here is the files. The file key is derived from the inbox
    private key, so a new one means every file-key file has to be re-encrypted,
    and until that runs those files open under a key this operation has stopped
    deriving. The returned count says how many are waiting: a rekey that
    silently orphaned a vault's pictures would be worse than one that refuses.

    Refuses a locked vault: without the old private key there is nothing to
    re-wrap from, and re-wrapping blind would seal every card under a key the
    owner has not proved they hold.
    """
    old_private = await data_key_for(collection, unlock_token)
    if old_private is None:
        raise VaultRekeyError("Разблокируйте Vault, чтобы сменить ключ")
    old_public = inbox_public_key(collection)
    if old_public is None:
        raise VaultRekeyError("У этого Vault нет входящего ключа")

    new_private, new_public = generate_inbox_keypair()
    wrapped = wrap_data_key(new_private, passphrase, context=context_for("collection", collection.id))
    kek, _salt = kek_for_wrapper(wrapped, passphrase)

    rows = list(
        (
            await session.execute(
                select(VaultItem).where(
                    VaultItem.collection_id == collection.id,
                    VaultItem.wrapped_key.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    rewrapped = 0
    skipped = 0
    for item in rows:
        try:
            item_key = open_inbox_key(
                SealedWrite(payload=item.sealed_payload, wrapped_key=item.wrapped_key),
                old_private,
                collection_id=collection.id,
                kind="item",
                row_id=item.id,
            )
        except ValueError:
            # A row whose wrapper is not a version we know is corruption.
            # Rewriting it would replace a diagnosable error with a row that
            # cannot be opened at all, so it is left and counted.
            skipped += 1
            continue
        item.wrapped_key = rewrap_inbox_key(
            item_key, new_public, collection_id=collection.id, kind="item", row_id=item.id
        )
        rewrapped += 1

    if collection.sealed_payload and collection.sealed_wrapped_key:
        collection_key_pair = open_inbox_key(
            SealedWrite(payload=collection.sealed_payload, wrapped_key=collection.sealed_wrapped_key),
            old_private,
            collection_id=collection.id,
            kind="collection",
            row_id=collection.id,
        )
        collection.sealed_wrapped_key = rewrap_inbox_key(
            collection_key_pair,
            new_public,
            collection_id=collection.id,
            kind="collection",
            row_id=collection.id,
        )

    collection.inbox_public_key = b64encode(new_public).decode("ascii")
    collection.inbox_pub_mac = crypto_inbox_pub_mac(kek, new_public, collection.id)
    store_wrapper(collection, wrapped)
    # The package DEK follows the inbox private key, so a rekey retires it: the
    # old wrapped copy would open nothing already re-encrypted. Same passphrase,
    # new DEK, stored in the same commit as everything else rekeyed.
    refresh_package_wrap(collection, new_private, passphrase)
    await session.commit()
    await session.refresh(collection)

    # The session this tab holds is registered under the old private key, so it
    # is dropped: the tab must unlock again, which is what proves the new pair
    # works before anyone relies on it.
    await lock_collection(collection.id, unlock_token)

    return {
        "collection_id": collection.id,
        "rewrapped_items": rewrapped,
        "skipped_items": skipped,
        "files_awaiting_reencryption": await _count_collection_files(session, collection.id),
        "inbox_fingerprint": inbox_pub_fingerprint(new_public),
    }


async def _count_collection_files(session, collection_id: int) -> int:
    """How many stored files a rekey leaves waiting for the new file key."""
    from sqlalchemy import func, or_

    result = await session.execute(
        select(func.count(VaultItem.id)).where(
            VaultItem.collection_id == collection_id,
            or_(
                VaultItem.image_path.is_not(None),
                VaultItem.media_thumbnail_path.is_not(None),
                VaultItem.media_path.is_not(None),
            ),
        )
    )
    return int(result.scalar() or 0)


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
