"""Video downloads that land in Vault's own storage.

A media card owns its file. Keeping the bytes here means the card keeps working
when the page it came from is gone, and it means Vault is not holding a record
whose only link is another module's row.
"""

import logging
import os
import re
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.storage import get_storage
from app.core.ytdlp_pipeline import YtDlpPipelineError, error_status, extract_info
from app.modules.vault.models import VaultCollection, VaultItem

logger = logging.getLogger(__name__)

MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
STORAGE_PREFIX = "vault/videos"
THUMBNAIL_PREFIX = "vault/thumbnails"
SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9._-]+")


def _storage_root() -> Path:
    return Path(get_settings().LOCAL_STORAGE_ROOT)


def _safe_segment(value: str, fallback: str = "video") -> str:
    cleaned = SAFE_SEGMENT.sub("-", str(value or "")).strip("-.")
    return (cleaned or fallback)[:80]


def _within_root(candidate: Path) -> bool:
    root = _storage_root().resolve()
    resolved = candidate.resolve()
    return resolved.is_relative_to(root)


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
    """Write download progress onto the item so the card can show it."""

    def report(message: str) -> None:
        try:
            with SyncSessionLocal() as session:
                item = session.get(VaultItem, item_id)
                if item is not None and item.node_type == "video":
                    item.canvas_data = {**(item.canvas_data or {}), "media_status": message}
                    session.commit()
        except Exception:
            logger.debug("could not record vault media status", exc_info=True)

    return report


@celery_app.task(bind=True)
def _collection_is_sealed(session, item) -> bool:
    """Whether the item lives in a collection that must not store media in the clear."""
    collection = session.get(VaultCollection, item.collection_id) if item is not None else None
    return bool(getattr(collection, "is_encrypted", False))


def download_vault_video_task(
    self,
    item_id: int,
    url: str,
    quality: str = "720",
    title: str | None = None,
) -> str:
    """Download a captured video into Vault storage and attach it to the item."""
    report = _record_status(item_id, "")
    storage = get_storage()
    workdir = Path(tempfile.mkdtemp(prefix="vault_video_"))

    def hook(download: dict):
        total = download.get("total_bytes") or download.get("total_bytes_estimate") or 0
        if total and total > MAX_VIDEO_BYTES:
            raise RuntimeError("The video is larger than the Vault media limit")
        done = download.get("downloaded_bytes") or 0
        if done > MAX_VIDEO_BYTES:
            raise RuntimeError("The video is larger than the Vault media limit")
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
                report(f"error: {error_status(exc)}")
                return f"Error: {error_status(exc)}"

        files = sorted(p for p in workdir.iterdir() if p.is_file())
        if not files:
            report("error: nothing was downloaded")
            return "Error: nothing was downloaded"
        video_file = max(files, key=lambda p: p.stat().st_size)
        size = video_file.stat().st_size
        if size > MAX_VIDEO_BYTES:
            report("error: too large")
            return "Error: The video is larger than the Vault media limit"

        stem = _safe_segment((info.get("id") or "") or Path(url).stem)
        ext = (video_file.suffix or ".mp4").lower()[:6]
        destination = _storage_root() / STORAGE_PREFIX / f"{item_id}-{stem}{ext}"
        if not _within_root(destination):
            report("error: refused path")
            return "Error: refused storage path"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with SyncSessionLocal() as session:
            parent = session.get(VaultItem, item_id)
            sealed = bool(getattr(parent, "sealed_payload", None)) or _collection_is_sealed(session, parent)

        if sealed:
            # A video in a sealed Vault has to be seekable from the player, which
            # rules out a single AES-GCM blob: GCM authenticates the whole
            # ciphertext, so any range would mean decrypting from byte zero.
            destination = destination.with_suffix(destination.suffix + ".enc")
            with video_file.open("rb") as stream:
                storage.save_file_encrypted_seekable(stream, str(destination.relative_to(_storage_root())))
        else:
            with video_file.open("rb") as stream:
                storage.save_stream(stream, str(destination.relative_to(_storage_root())))

        thumbnail_path = _store_thumbnail(storage, info, item_id)

        with SyncSessionLocal() as session:
            item = session.get(VaultItem, item_id)
            if item is None:
                return "Error: the Vault item is gone"
            item.media_path = str(destination.relative_to(_storage_root()))
            item.media_mime = "video/mp4" if ext == ".mp4" else f"video/{ext.lstrip('.')}"
            item.media_size = size
            item.canvas_data = {
                **(item.canvas_data or {}),
                "media_status": "completed",
                "media_title": info.get("title") or title,
                "media_duration": info.get("duration"),
                "media_thumbnail_path": thumbnail_path,
                "media_width": info.get("width"),
                "media_height": info.get("height"),
            }
            if not item.title and info.get("title"):
                item.title = str(info["title"])[:1000]
            session.commit()
        return f"Saved {size} bytes to Vault"
    except Exception as exc:
        logger.warning("Vault video download failed for %s: %s", url, exc)
        report(f"error: {exc}")
        return f"Error: {exc}"
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


def _store_thumbnail(storage, info: dict, item_id: int) -> str | None:
    """Best-effort poster for the card. Its absence must not fail the download."""
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
        destination = _storage_root() / THUMBNAIL_PREFIX / f"{item_id}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        storage.save_file(content, str(destination.relative_to(_storage_root())))
        return str(destination.relative_to(_storage_root()))
    except Exception:
        logger.debug("no thumbnail stored for vault item %s", item_id, exc_info=True)
        return None
