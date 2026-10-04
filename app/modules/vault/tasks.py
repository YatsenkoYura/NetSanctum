"""Video downloads that land in Vault's own storage.

A media card owns its file. Keeping the bytes here means the card keeps working
when the page it came from is gone, and it means Vault is not holding a record
whose only link is another module's row.
"""

import asyncio
import logging
import os
import secrets
from enum import StrEnum
from pathlib import Path

from app.core.config import get_settings
from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.staging import staging_workdir
from app.core.storage import get_storage
from app.core.ytdlp_pipeline import YtDlpErrorKind, YtDlpPipelineError, extract_info
from app.modules.vault.models import VaultCollection, VaultItem
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
                if item is not None and item.node_type == "video":
                    item.media_status = message
                    session.commit()
        except Exception:
            logger.debug("could not record vault media status", exc_info=True)

    return report


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
    """
    item.media_path = media_path
    item.media_size = size
    # Structural columns, one per fact. See the note in `models.py`: the worker has
    # no vault key, so anything it writes has to be readable.
    item.media_status = "completed"
    item.media_thumbnail_path = thumbnail_path
    duration = info.get("duration")
    item.media_duration = float(duration) if isinstance(duration, (int, float)) else None
    width, height = info.get("width"), info.get("height")
    item.media_width = int(width) if isinstance(width, (int, float)) else None
    item.media_height = int(height) if isinstance(height, (int, float)) else None
    if sealed:
        return
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
            sealed_for_name = bool(getattr(parent, "sealed_payload", None)) or _collection_is_sealed(
                session, parent
            )
        stem, ext = video_storage_name(
            sealed=sealed_for_name,
            info=info,
            url=url,
            ext=(video_file.suffix or ".mp4").lower()[:6],
        )
        destination = _storage_root() / STORAGE_PREFIX / f"{item_id}-{stem}{ext}"
        if not _within_root(destination, root=_storage_root()):
            report(f"error: {MediaError.SOURCE}")
            return "Error: refused storage path"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with SyncSessionLocal() as session:
            parent = session.get(VaultItem, item_id)
            sealed = bool(getattr(parent, "sealed_payload", None)) or _collection_is_sealed(session, parent)

        if sealed:
            # The file key when the queueing tab lent its token through the
            # handoff, else None — resolved once, up front, so no TTL matters
            # after this point. Without it the application key applies, exactly
            # as before, and the lock check on the endpoints is the protection.
            from app.modules.vault.services import resolve_download_file_key

            file_key = resolve_download_file_key(handoff, handoff_data, item_id) if handoff_data else None
        else:
            file_key = None

        if sealed:
            # A video in a sealed Vault has to be seekable from the player, which
            # rules out a single AES-GCM blob: GCM authenticates the whole
            # ciphertext, so any range would mean decrypting from byte zero.
            destination = destination.with_suffix(destination.suffix + ".enc")
            # The size comes from the file we just wrote, so the envelope is
            # sealed straight from the download: no second plaintext copy.
            with video_file.open("rb") as stream:
                storage.save_file_encrypted_seekable(
                    stream,
                    str(destination.relative_to(_storage_root())),
                    key=file_key,
                    length=size,
                )
        else:
            with video_file.open("rb") as stream:
                storage.save_stream(stream, str(destination.relative_to(_storage_root())))

        thumbnail_path = _store_thumbnail(storage, info, item_id, key=file_key)

        with SyncSessionLocal() as session:
            item = session.get(VaultItem, item_id)
            if item is None:
                return "Error: the Vault item is gone"
            attach_downloaded_media(
                item,
                sealed=sealed,
                media_path=str(destination.relative_to(_storage_root())),
                thumbnail_path=thumbnail_path,
                size=size,
                mime="video/mp4" if ext == ".mp4" else f"video/{ext.lstrip('.')}",
                title=info.get("title") or title,
                info=info,
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


def _store_thumbnail(storage, info: dict, item_id: int, *, key: bytes | None = None) -> str | None:
    """Best-effort poster for the card. Its absence must not fail the download.

    Under the collection's file key when the queueing tab lent one, else under
    the application key like the video next to it. It used to be written in the
    clear, which meant a sealed collection held an encrypted video beside a
    readable poster.
    """
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
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
        destination = _storage_root() / THUMBNAIL_PREFIX / f"{item_id}{suffix}.enc"
        destination.parent.mkdir(parents=True, exist_ok=True)
        storage.save_file_encrypted(content, str(destination.relative_to(_storage_root())), key=key)
        return str(destination.relative_to(_storage_root()))
    except Exception:
        logger.debug("no thumbnail stored for vault item %s", item_id, exc_info=True)
        return None
