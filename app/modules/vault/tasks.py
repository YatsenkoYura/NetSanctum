"""Video downloads that land in Vault's own storage.

A media card owns its file. Keeping the bytes here means the card keeps working
when the page it came from is gone, and it means Vault is not holding a record
whose only link is another module's row.
"""

import asyncio
import base64
import logging
import os
import secrets
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto.asymmetric import rewrap_inbox_key
from app.core.crypto.kdf import new_data_key
from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.staging import staging_workdir
from app.core.storage import get_storage
from app.core.ytdlp_pipeline import YtDlpErrorKind, YtDlpPipelineError, extract_info
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.node_types import NodeType
from app.modules.vault.paths import (
    safe_segment as _sanitize_segment,
    storage_root as _storage_root,
    within_root as _within_root,
)

logger = logging.getLogger(__name__)


def _safe_segment(value: str, fallback: str = "video") -> str:
    """Sanitize a video filename segment (shared impl, Vault-flavoured default)."""
    return _sanitize_segment(value, fallback)


class MediaError(StrEnum):
    """What a failed download says in the clear.

    `media_status` is a structural column: it stays readable for a card in a
    locked vault, because the card has to show the download's progress while it
    is sealed. That makes it a place where the exception text used to leak — and
    yt-dlp puts the source address, sometimes the title, into almost every
    message it raises. So the column carries a code from this enum and nothing
    else, and the same code is what the card renders.
    """

    EXPIRED = "expired"
    NOTHING = "nothing"
    TOO_LARGE = "too large"
    SOURCE = "source"
    NETWORK = "network"
    GEO_BLOCKED = "geo blocked"
    AUTH_REQUIRED = "auth required"
    RATE_LIMITED = "rate limited"
    UNSUPPORTED = "unsupported"
    GONE = "card is gone"
    UNKNOWN = "unknown"


# YtDlp's own error kinds, mapped onto codes rather than passed through: the
# kinds are a closed vocabulary, and the words inside them are not.
_KIND_TO_CODE = {
    YtDlpErrorKind.AUTH_REQUIRED: MediaError.AUTH_REQUIRED,
    YtDlpErrorKind.GEO_BLOCKED: MediaError.GEO_BLOCKED,
    YtDlpErrorKind.NETWORK: MediaError.NETWORK,
    YtDlpErrorKind.RATE_LIMITED: MediaError.RATE_LIMITED,
    YtDlpErrorKind.UNAVAILABLE: MediaError.SOURCE,
    YtDlpErrorKind.UNSUPPORTED: MediaError.UNSUPPORTED,
}


def media_error_code(error: BaseException) -> MediaError:
    """The one word a failed download leaves in the clear.

    `str(error)` is never used: it is the card's content. An unrecognised
    exception becomes `unknown`, which says nothing to whoever reads the column
    and everything to whoever reads the log — where the class name goes instead.
    """
    if isinstance(error, YtDlpPipelineError):
        return _KIND_TO_CODE.get(error.kind, MediaError.UNKNOWN)
    return MediaError.UNKNOWN


def _human(size: int) -> str:
    """A limit the user can act on needs a number in it, not just a refusal."""
    if size >= 1024**3:
        return f"{size / 1024**3:.0f} GiB"
    return f"{size / 1024**2:.0f} MiB"


STORAGE_PREFIX = "vault/videos"
THUMBNAIL_PREFIX = "vault/thumbnails"

# A sealed card whose video has not been downloaded yet, because nobody lent the
# worker a vault key. It is a status and not an error: the address and the title
# are already sealed in the payload, and the download happens when the owner
# asks for it from an unlocked tab.
MEDIA_PENDING_STATUS = "pending_unlock"

# A sealed card whose video bytes are already home — stored under a random item
# key the worker minted itself, with that key wrapped under the collection's
# inbox public key (`media_key_wrap`) — but not yet re-sealed under the
# collection's file key. Unplayable until the finalize on the next unlock clears
# it, and deliberately so: the file was never under a weaker key, so there is
# nothing to re-download, only a re-encryption to wait for.
MEDIA_BLIND_STATUS = "blind"

# The inbox binding kind a blind media wrap is sealed under. The v2 envelope
# binds the collection id and this row id into both the derivation and the tag,
# so a wrap lifted from one card cannot be offered to another — and the
# finalize must open it under exactly this kind.
MEDIA_KEY_KIND = "media"


def mint_blind_media_key(
    collection_id: int, item_id: int, inbox_public_b64: str | None
) -> tuple[bytes, str] | None:
    """A random item key for one download, wrapped under the inbox public key.

    The worker that downloads into a sealed collection has no vault key, and the
    application key is not an option — so the file is encrypted under a key that
    exists only for this download, and the key itself is sealed so that only the
    passphrase holder can open it. Returns (key, wrap), or None when there is no
    usable inbox public key and the card must wait as `pending_unlock` instead.
    """
    try:
        public = base64.b64decode(inbox_public_b64 or "")
    except Exception:
        return None
    if len(public) != 32:
        return None
    blind_key = new_data_key()
    try:
        wrap = rewrap_inbox_key(
            blind_key, public, collection_id=collection_id, kind=MEDIA_KEY_KIND, row_id=item_id
        )
    except ValueError:
        return None
    return blind_key, wrap


def _discard_download(video_file: Path, workdir: Path) -> None:
    """Delete a download that cannot be stored under a vault key.

    The alternative — parking it under the application key — is what this whole
    arrangement exists to stop, and a file that cannot be encrypted properly is
    not a file worth keeping.
    """
    for path in (video_file, *workdir.glob("*")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not discard the downloaded file %s", path, exc_info=True)


def _format_selector(quality: str | None) -> str:
    """yt-dlp format selector for a height preference.

    `best` — and anything that is not a plausible height — drops the cap instead
    of interpolating into it, because `height<=best` is rejected outright as an
    invalid filter specification and fails the whole download.
    """
    text = str(quality or "").strip().lower()
    cap = f"[height<={text}]" if text.isdigit() and int(text) > 0 else ""
    return f"bestvideo{cap}[ext=mp4]+bestaudio[ext=m4a]/best{cap}[ext=mp4]/best{cap}/best"


def _record_status(item_id: int, status: str):
    """Write download progress onto the item so the card can show it.

    `media_status` is a structural column, not `canvas_data`: canvas_data is a
    sealed field, so for an item in a sealed collection a write there was both
    lost on the next unlock and visible in the clear until then.
    """

    def report(message: str) -> None:
        try:
            with SyncSessionLocal() as session:
                item = session.get(VaultItem, item_id)
                if item is not None and item.node_type == NodeType.VIDEO:
                    item.media_status = message
                    session.commit()
        except Exception:
            logger.debug("could not record vault media status", exc_info=True)

    return report


# Where a sealed collection's media lives. The name is random and the path names
# the row that owns it, so a leaked path says nothing about the file and one
# card's path cannot be offered to another: the envelope binds its own path, and
# the reader checks that the row segment agrees with the row asking.
SEALED_MEDIA_PREFIX = "vault/{collection_id}/{item_id}"


def sealed_media_path(collection_id: int, item_id: int, suffix: str = "mp4.enc") -> str:
    """A fresh, unguessable path for one sealed card's media."""
    template = SEALED_MEDIA_PREFIX.format(collection_id=collection_id, item_id=item_id)
    return f"{template}/{secrets.token_hex(16)}.{suffix}"


def sealed_media_owner(path: str) -> tuple[int, int] | None:
    """The (collection, item) a sealed media path claims, or None if it claims nothing.

    A legacy path — the old `vault/videos/{item_id}-{stem}.ext` layout, and
    anything a plain collection wrote — returns None, and the reader treats that
    as "an application-key object, served as before". That is the documented
    residual risk for videos stored before this layout existed; they are still
    readable, and they are still not under a vault key.
    """
    parts = str(path or "").split("/")
    if len(parts) != 4 or parts[0] != "vault" or not parts[3]:
        return None
    if not parts[1].isdigit() or not parts[2].isdigit():
        return None
    return int(parts[1]), int(parts[2])


def video_storage_name(*, sealed: bool, info: dict, url: str, ext: str) -> tuple[str, str]:
    """The filename stem and extension for a downloaded video.

    A plain card keeps a readable name, because there is nothing to hide. A sealed
    one does not: the source id and the format both say what the file is, and the
    name is visible to anything that can list the storage volume.
    """
    if sealed:
        return secrets.token_hex(8), ""
    return _safe_segment((info.get("id") or "") or Path(url).stem), ext


def attach_downloaded_media(
    item,
    *,
    sealed: bool,
    media_path: str,
    thumbnail_path: str | None,
    size: int,
    mime: str,
    title: str | None,
    info: dict,
    blind_wrap: str | None = None,
) -> None:
    """Record what the download learned about the card.

    The worker has no vault key, so everything it writes stays readable — which is
    exactly why it may only write *structural* facts. This used to write the video's
    real title into `media_title`, and `media_mime` on top of a card whose sealed
    payload had already been written, and fill `title` back in after sealing. All
    three put the content of a sealed card back in the clear, in the one place that
    has no key to protect it with.

    So on a sealed card the content-bearing columns are left alone: the payload
    already holds them, and the alias is what the owner sees anyway.

    A blind write additionally records the wrapped item key: the file is stored,
    but under that key rather than the collection's file key, and the card is
    unplayable until the finalize on the next unlock re-seals it.
    """
    item.media_path = media_path
    item.media_size = size
    # `media_size` and `media_status` stay readable by design: a length says
    # nothing about what the file is, and the card has to show the download's
    # state while it is sealed. The path is random and the worker is the only
    # thing that writes it, after a download that carried a vault token.
    item.media_status = MEDIA_BLIND_STATUS if blind_wrap else "completed"
    item.media_key_wrap = blind_wrap
    item.media_thumbnail_path = thumbnail_path
    if sealed:
        # Nothing else. Duration and dimensions are sealed fields now, and this
        # worker has no vault key: writing them would put them back in the clear,
        # and sealing them afterwards is not possible without the key it does not
        # have. A sealed card's video therefore reports no duration and no
        # dimensions until somebody edits it — the honest cost of not having a
        # key here, and the reason a download has to borrow one.
        return
    duration = info.get("duration")
    item.media_duration = float(duration) if isinstance(duration, (int, float)) else None
    width, height = info.get("width"), info.get("height")
    item.media_width = int(width) if isinstance(width, (int, float)) else None
    item.media_height = int(height) if isinstance(height, (int, float)) else None
    item.media_mime = mime
    item.media_title = title
    if not item.title and title:
        item.title = title[:1000]


def _collection_is_sealed(session, item) -> bool:
    """Whether the item lives in a collection that must not store media in the clear."""
    collection = session.get(VaultCollection, item.collection_id) if item is not None else None
    return bool(getattr(collection, "is_encrypted", False))


@celery_app.task(bind=True)
def download_vault_video_task(
    self,
    item_id: int,
    quality: str = "720",
    handoff: str = "",
    url: str | None = None,
    title: str | None = None,
) -> str:
    """Download a captured video into Vault storage and attach it to the item.

    The url and the title arrive through a one-shot Redis handoff rather than as
    arguments: Celery writes its arguments into the broker, and the broker is the
    same Redis that keeps an AOF on disk. `url` stays in the signature only so a
    queued task from before this change still runs.
    """
    report = _record_status(item_id, "")
    storage = get_storage()
    # The download lands here first, in the clear, because yt-dlp writes it as an
    # ordinary file. Staging is the only directory configured to be private and
    # memory-backed; the default `/tmp` is neither on most deployments.
    workdir = staging_workdir("vault_video_")

    if url is None:
        from app.modules.vault.services import take_download_handoff

        handoff_data = asyncio.run(take_download_handoff(handoff))
        url = handoff_data.get("url")
        title = title or handoff_data.get("title") or None
    else:
        handoff_data = {}
    if not url:
        report(f"error: {MediaError.EXPIRED}")
        return "Error: the download request expired before the worker picked it up"

    limit = get_settings().VAULT_MAX_VIDEO_BYTES

    def too_large() -> RuntimeError:
        return RuntimeError(f"The video is larger than the Vault media limit of {_human(limit)}")

    def hook(download: dict):
        total = download.get("total_bytes") or download.get("total_bytes_estimate") or 0
        if total and total > limit:
            raise too_large()
        done = download.get("downloaded_bytes") or 0
        if done > limit:
            raise too_large()
        if total:
            report(f"downloading {min(100, round(done * 100 / total))}%")

    try:
        report("resolving")
        with SyncSessionLocal() as session:
            options = {
                "outtmpl": str(workdir / "%(id)s.%(ext)s"),
                "quiet": True,
                "noplaylist": True,
                "merge_output_format": "mp4",
                "format": _format_selector(quality),
                "progress_hooks": [hook],
            }
            try:
                info = extract_info(
                    None,
                    url,
                    options=options,
                    download=True,
                    platform="other",
                )
            except YtDlpPipelineError as exc:
                # The code goes to the card; the exception class goes to the log.
                # Neither carries the text, which is the card's content.
                code = media_error_code(exc)
                logger.warning(
                    "Vault video download failed for item %s (%s, %s)",
                    item_id,
                    type(exc).__name__,
                    code,
                )
                report(f"error: {code}")
                return f"Error: {code}"

        files = sorted(p for p in workdir.iterdir() if p.is_file())
        if not files:
            report(f"error: {MediaError.NOTHING}")
            return "Error: nothing was downloaded"
        video_file = max(files, key=lambda p: p.stat().st_size)
        size = video_file.stat().st_size
        if size > limit:
            report(f"error: {MediaError.TOO_LARGE}")
            return f"Error: The video is larger than the Vault media limit of {_human(limit)}"

        with SyncSessionLocal() as session:
            parent = session.get(VaultItem, item_id)
            sealed = bool(getattr(parent, "sealed_payload", None)) or _collection_is_sealed(session, parent)
            collection_id = parent.collection_id if parent is not None else None
            inbox_public_b64 = None
            if sealed and parent is not None and collection_id is not None:
                collection = session.get(VaultCollection, collection_id)
                inbox_public_b64 = getattr(collection, "inbox_public_key", None)

        file_key = None
        blind_wrap = None
        if sealed:
            # A sealed collection's video is encrypted under that collection's
            # file key, which comes from a session. The queueing tab lends its
            # token through the handoff; resolved once, up front, so no TTL
            # matters after this point.
            from app.modules.vault.services import resolve_download_file_key

            file_key = resolve_download_file_key(handoff, handoff_data, item_id) if handoff_data else None
            if file_key is None:
                if collection_id is None:
                    report(MEDIA_PENDING_STATUS)
                    _discard_download(video_file, workdir)
                    return "Error: this Vault is locked; retry the download from an unlocked tab"
                # No vault key — but no waiting either. The file is stored at
                # once, under a random item key minted for this download, with
                # that key wrapped under the collection's inbox public key: a
                # blind write, exactly like a card the extension captured. The
                # next unlock re-seals the file under the collection's file key
                # and clears the wrap. Without a usable inbox key there is
                # nowhere to put this that is not the shared application key,
                # and that is exactly the arrangement this closed — so the card
                # goes back to waiting, and the bytes are deleted rather than
                # parked under a weaker key than the vault promised.
                minted = mint_blind_media_key(collection_id, item_id, inbox_public_b64)
                if minted is None:
                    report(MEDIA_PENDING_STATUS)
                    _discard_download(video_file, workdir)
                    return "Error: this Vault is locked; retry the download from an unlocked tab"
                file_key, blind_wrap = minted

        if sealed:
            # A video in a sealed Vault has to be seekable from the player, which
            # rules out a single AES-GCM blob: GCM authenticates the whole
            # ciphertext, so any range would mean decrypting from byte zero.
            # The path is fresh and names the row that owns it. `file_key` here
            # is either the collection's file key or the download's own blind
            # key — the envelope does not care, and the wrap on the row says
            # which one it was.
            ext = (video_file.suffix or ".mp4").lower()[:6]
            assert collection_id is not None  # refused as pending above when missing
            relative = sealed_media_path(collection_id, item_id, "mp4.enc")
            destination = _storage_root() / relative
            # The size comes from the file we just wrote, so the envelope is
            # sealed straight from the download: no second plaintext copy.
            with video_file.open("rb") as stream:
                storage.save_file_encrypted_seekable(stream, relative, key=file_key, length=size)
        else:
            stem, ext = video_storage_name(
                sealed=False,
                info=info,
                url=url,
                ext=(video_file.suffix or ".mp4").lower()[:6],
            )
            relative = f"{STORAGE_PREFIX}/{item_id}-{stem}{ext}"
            destination = _storage_root() / relative
            if not _within_root(destination, root=_storage_root()):
                report(f"error: {MediaError.SOURCE}")
                return "Error: refused storage path"
            with video_file.open("rb") as stream:
                storage.save_stream(stream, relative)

        thumbnail_path = _store_thumbnail(
            storage, info, item_id, key=file_key, sealed=sealed, collection_id=collection_id
        )

        with SyncSessionLocal() as session:
            item = session.get(VaultItem, item_id)
            if item is None:
                return "Error: the Vault item is gone"
            attach_downloaded_media(
                item,
                sealed=sealed,
                media_path=relative,
                thumbnail_path=thumbnail_path,
                size=size,
                mime="video/mp4" if ext == ".mp4" else f"video/{ext.lstrip('.')}",
                title=info.get("title") or title,
                info=info,
                blind_wrap=blind_wrap,
            )
            session.commit()
        return f"Saved {size} bytes to Vault"
    except Exception as exc:
        # The url is the card's content: it belongs in the sealed payload. A log
        # line and the task's own result string are the two places that would hand
        # it to anyone who can read them — and the result string lands in the
        # broker, which is a Redis with an AOF on disk. So both carry the class
        # name and a code, never the message.
        code = media_error_code(exc)
        logger.warning("Vault video download failed for item %s (%s, %s)", item_id, type(exc).__name__, code)
        report(f"error: {code}")
        return f"Error: {code}"
    finally:
        for leftover in workdir.glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass


@celery_app.task(bind=True)
def finalize_blind_media_task(self, handoff: str, collection_id: int) -> str:
    """Re-seal blind media files under the collection's file key.

    Runs after an unlock, with the unlocking tab's token lent through a one-shot
    handoff — the same arrangement downloads use, because the broker's AOF is no
    place for a passphrase-derived token either. Every blind file is decrypted
    under its own item key (opened from the row's wrap through the inbox private
    key) and re-encrypted under the collection's file key at a fresh path, so
    afterwards the row is indistinguishable from one the worker stored while
    unlocked — same envelope, same path layout, no wrap left.

    The wrap is cleared last, per row, each in its own commit: an interrupted
    run simply resumes on the next unlock, because a row that still has a wrap
    is a row still to do. A file that fails stays blind and keeps waiting rather
    than degrading to a weaker key.
    """
    from app.core.crypto.asymmetric import SealedWrite, open_inbox_key
    from app.modules.vault.crypto import derive_file_key
    from app.modules.vault.sealing import data_key_for, open_handoff_token
    from app.modules.vault.services import take_finalize_handoff

    storage = get_storage()
    handoff_data = asyncio.run(take_finalize_handoff(handoff))
    token = open_handoff_token(handoff, (handoff_data or {}).get("token_box"))
    if not token:
        return "Error: the finalize request expired before the worker picked it up"

    with SyncSessionLocal() as session:
        collection = session.get(VaultCollection, collection_id)
        if collection is None or not getattr(collection, "is_encrypted", False):
            return "Error: the Vault is not sealed"
        try:
            private_key = asyncio.run(data_key_for(collection, token, touch=False))
        except Exception:
            logger.warning("finalize for vault %s lost its session", collection_id, exc_info=True)
            return "Error: the Vault locked before the finalize ran"
        if private_key is None:
            return "Error: the Vault locked before the finalize ran"
        file_key = derive_file_key(private_key, collection.id)
        rows = (
            session.execute(
                select(VaultItem).where(
                    VaultItem.collection_id == collection_id,
                    VaultItem.media_key_wrap.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        pending = [
            (row.id, row.media_path, row.media_size, row.media_thumbnail_path, row.media_key_wrap)
            for row in rows
        ]

    done, failed = 0, 0
    for item_id, old_path, size, old_thumb, wrap in pending:
        try:
            blind_key = open_inbox_key(
                SealedWrite(payload="", wrapped_key=wrap),
                private_key,
                collection_id=collection_id,
                kind=MEDIA_KEY_KIND,
                row_id=item_id,
            )
        except Exception:
            logger.warning("finalize could not open the blind key for item %s", item_id, exc_info=True)
            failed += 1
            continue
        workdir = staging_workdir("vault_finalize_")
        try:
            new_thumb: str | None = None
            if old_path and size:
                if not storage.is_seekable_encrypted(old_path):
                    logger.warning("finalize found a non-seekable blind file for item %s", item_id)
                    failed += 1
                    continue
                relative = sealed_media_path(collection_id, item_id, "mp4.enc")
                with storage.get_file_stream_decrypted(old_path, key=blind_key) as plain:
                    storage.save_file_encrypted_seekable(plain, relative, key=file_key, length=size)
                if old_thumb:
                    new_thumb = _finalize_blind_thumbnail(
                        storage, old_thumb, blind_key, file_key, collection_id, item_id
                    )
                    if new_thumb is None:
                        logger.debug("finalize dropped the poster for item %s", item_id)
                try:
                    storage.delete_file(old_path)
                except Exception:
                    logger.debug("could not delete the blind file for item %s", item_id, exc_info=True)
            else:
                # A wrap with nothing behind it: the download never finished
                # writing, so there is nothing to re-seal and nothing to play.
                # Clear it rather than park the row in a state no job clears.
                logger.warning("finalize found a blind wrap with no file for item %s", item_id)
                relative, new_thumb = None, None
            with SyncSessionLocal() as session:
                row = session.get(VaultItem, item_id)
                if row is None:
                    failed += 1
                    continue
                if row.media_key_wrap is None or row.media_path != old_path:
                    # Another finalize (or a fresh download) got here first: the
                    # row moved on, and re-sealing our stale copy would orphan
                    # the winner's file. Skip it — the row is somebody else's
                    # completed now, which is exactly what we wanted.
                    done += 1
                    continue
                if relative is not None:
                    row.media_path = relative
                    row.media_thumbnail_path = new_thumb
                row.media_key_wrap = None
                row.media_status = "completed"
                session.commit()
            done += 1
        except Exception:
            logger.warning("finalize failed for item %s", item_id, exc_info=True)
            failed += 1
        finally:
            for leftover in workdir.glob("*"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            try:
                os.rmdir(workdir)
            except OSError:
                pass
    return f"Finalized {done} of {done + failed} blind media files"


def _finalize_blind_thumbnail(
    storage, old_thumb: str, blind_key: bytes, file_key: bytes, collection_id: int, item_id: int
) -> str | None:
    """Re-seal one blind poster under the collection's file key.

    Best-effort like the download's own poster: a video without a poster plays,
    a failed finalize without one still finalizes. None means "drop it", never
    "stop the row".
    """
    try:
        with storage.get_file_stream_decrypted(old_thumb, key=blind_key) as stream:
            content = stream.read()
    except Exception:
        return None
    stem = str(old_thumb).rsplit("/", 1)[-1].rsplit(".", 2)
    suffix = f"{stem[1]}.enc" if len(stem) == 3 and stem[2] == "enc" and stem[1] else "jpg.enc"
    relative = sealed_media_path(collection_id, item_id, suffix)
    try:
        storage.save_file_encrypted(content, relative, key=file_key)
    except Exception:
        return None
    try:
        storage.delete_file(old_thumb)
    except Exception:
        logger.debug("could not delete the blind poster for item %s", item_id, exc_info=True)
    return relative


def _store_thumbnail(
    storage,
    info: dict,
    item_id: int,
    *,
    key: bytes | None = None,
    sealed: bool = False,
    collection_id: int | None = None,
) -> str | None:
    """Best-effort poster for the card. Its absence must not fail the download.

    A sealed collection's poster follows its video: the collection's file key,
    and a fresh path under the row's own directory. There is no app-key fallback
    for it — a poster beside a vault-key video, readable by anyone who reaches
    the storage volume, is not a smaller version of the same mistake. A poster is
    only ever written when the download had a key.
    """
    if sealed and (key is None or collection_id is None):
        return None
    url = info.get("thumbnail")
    if not url or not str(url).startswith("http"):
        return None
    try:
        from app.core.remote_fetch import fetch_bytes_checked

        content, content_type, _ = fetch_bytes_checked(
            str(url),
            max_bytes=8 * 1024 * 1024,
            allowed_content_prefixes=("image/",),
            https_only=False,
        )
        suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
        if sealed:
            relative = sealed_media_path(collection_id, item_id, f"{suffix}.enc")
            destination = _storage_root() / relative
        else:
            relative = f"{THUMBNAIL_PREFIX}/{item_id}.{suffix}.enc"
            destination = _storage_root() / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        storage.save_file_encrypted(content, relative, key=key)
        return relative
    except Exception:
        logger.debug("no thumbnail stored for vault item %s", item_id, exc_info=True)
        return None
