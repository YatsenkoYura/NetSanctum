import datetime
import hashlib
import json
import logging
import math
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import or_, select, update

from app.core.config import get_settings
from app.core.database import SyncSessionLocal
from app.core.storage import LocalStorage, StorageInterface, get_storage
from app.modules.video_archiver.models import ArchivedVideo

logger = logging.getLogger(__name__)

COMPRESSION_PROFILE = "h264-crf27-slow-aac128-v1"
COMPRESSION_LOCK_KEY = "video_compress_lock"
COMPRESSION_LOCK_TTL = 3600
COMPRESSION_TRACKER_TTL = COMPRESSION_LOCK_TTL
COMPLETED_COMPRESSION_STATES = ("completed", "skipped")


class CompressionCancelledError(RuntimeError):
    pass


def compression_candidate_ids(session) -> list[str]:
    statement = (
        select(ArchivedVideo.id)
        .where(
            ArchivedVideo.file_path.isnot(None),
            or_(
                ArchivedVideo.compression_profile.is_(None),
                ArchivedVideo.compression_profile != COMPRESSION_PROFILE,
                ArchivedVideo.compression_status.is_(None),
                ArchivedVideo.compression_status.notin_(COMPLETED_COMPRESSION_STATES),
            ),
        )
        .order_by(ArchivedVideo.archived_at, ArchivedVideo.id)
    )
    return list(session.scalars(statement).all())


def _probe_media(path: Path) -> tuple[float, bool]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    raw_duration = (payload.get("format") or {}).get("duration")
    if raw_duration is None:
        raw_duration = next((stream.get("duration") for stream in streams if stream.get("duration")), None)
    duration = float(raw_duration or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("ffprobe returned an invalid duration")
    return duration, has_video


def run_ffmpeg_compression(input_path: Path, output_path: Path, should_cancel: Callable[[], bool]) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-crf",
        "27",
        "-preset",
        "slow",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    log_path = output_path.with_suffix(".ffmpeg.log")
    with log_path.open("w+b") as error_log:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=error_log)
        while process.poll() is None:
            if should_cancel():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise CompressionCancelledError("Оптимизация отменена")
            time.sleep(1)
        if process.returncode:
            error_log.flush()
            error_log.seek(max(0, error_log.tell() - 4096))
            detail = error_log.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg завершился с кодом {process.returncode}: {detail[-2000:]}")


def _hash_file(path: Path, should_cancel: Callable[[], bool]) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if should_cancel():
                raise CompressionCancelledError("Оптимизация отменена")
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _copy_storage_file(
    storage: StorageInterface,
    storage_path: str,
    target: Path,
    should_cancel: Callable[[], bool],
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with storage.get_file_stream(storage_path) as source, target.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            if should_cancel():
                raise CompressionCancelledError("Оптимизация отменена")
            output.write(chunk)
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _set_result(
    video_id: str,
    expected_path: str,
    *,
    status: str,
    file_size: int | None,
    sha256: str | None,
    error: str | None = None,
    new_path: str | None = None,
) -> bool:
    values = {
        "file_size": file_size,
        "sha256": sha256,
        "compression_status": status,
        "compression_profile": COMPRESSION_PROFILE,
        "compressed_at": (
            datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            if status in COMPLETED_COMPRESSION_STATES
            else None
        ),
        "compression_error": error[:2000] if error else None,
    }
    if new_path is not None:
        values["file_path"] = new_path
    with SyncSessionLocal() as session:
        result = session.execute(
            update(ArchivedVideo)
            .where(
                ArchivedVideo.id == video_id,
                ArchivedVideo.file_path == expected_path,
                ArchivedVideo.compression_profile == COMPRESSION_PROFILE,
                ArchivedVideo.compression_status == "running",
            )
            .values(**values)
        )
        if result.rowcount != 1:
            session.rollback()
            return False
        session.commit()
        return True


def _finish_switch(video_id: str, new_path: str, old_path: str, storage: StorageInterface) -> str:
    if not storage.file_exists(new_path):
        with SyncSessionLocal() as session:
            result = session.execute(
                update(ArchivedVideo)
                .where(
                    ArchivedVideo.id == video_id,
                    ArchivedVideo.file_path == new_path,
                    ArchivedVideo.compression_profile == COMPRESSION_PROFILE,
                    ArchivedVideo.compression_status == "switching",
                )
                .values(
                    file_path=old_path,
                    file_size=None,
                    sha256=None,
                    compression_status="failed",
                    compressed_at=None,
                    compression_error="Новый объект оптимизации отсутствует; восстановлен исходный путь",
                )
            )
            if result.rowcount == 1:
                session.commit()
                return "failed"
            session.rollback()
            return "stale"

    with SyncSessionLocal() as session:
        result = session.execute(
            update(ArchivedVideo)
            .where(
                ArchivedVideo.id == video_id,
                ArchivedVideo.file_path == new_path,
                ArchivedVideo.compression_profile == COMPRESSION_PROFILE,
                ArchivedVideo.compression_status == "switching",
            )
            .values(
                compression_status="completed",
                compressed_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
                compression_error=None,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return "stale"
        session.commit()
    if old_path != new_path:
        try:
            storage.delete_file(old_path)
        except Exception:
            logger.exception("Could not delete superseded video object %s", old_path)
    return "completed"


def optimize_video(
    video_id: str,
    *,
    storage: StorageInterface | None = None,
    should_cancel: Callable[[], bool] = lambda: False,
) -> str:
    storage = storage or get_storage()
    pending_switch = None
    expected_path = None
    with SyncSessionLocal() as session:
        video = session.get(ArchivedVideo, video_id)
        if not video or not video.file_path:
            return "stale"
        if (
            video.compression_profile == COMPRESSION_PROFILE
            and video.compression_status == "switching"
            and video.compression_error
            and video.compression_error.startswith("old_path:")
        ):
            pending_switch = (
                video.file_path,
                video.compression_error.removeprefix("old_path:"),
            )
        else:
            expected_path = video.file_path
            claimed = session.execute(
                update(ArchivedVideo)
                .where(ArchivedVideo.id == video_id, ArchivedVideo.file_path == expected_path)
                .values(
                    compression_status="running",
                    compression_profile=COMPRESSION_PROFILE,
                    compressed_at=None,
                    compression_error=None,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                return "stale"
            session.commit()

    if pending_switch:
        try:
            return _finish_switch(video_id, pending_switch[0], pending_switch[1], storage)
        except Exception:
            logger.exception("Could not finalize pending video switch for %s", video_id)
            return "failed"
    assert expected_path is not None

    original_size = None
    original_sha256 = None
    new_storage_path = None
    new_object_owned_by_db = False
    try:
        configured_workdir = get_settings().VIDEO_COMPRESSION_WORKDIR.strip()
        if configured_workdir:
            workdir = Path(configured_workdir)
        elif isinstance(storage, LocalStorage):
            workdir = storage.local_path(".work/video-compression")
        else:
            raise RuntimeError("VIDEO_COMPRESSION_WORKDIR is required for remote video storage")
        workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="netsanctum-video-", dir=workdir) as directory:
            directory_path = Path(directory)
            if isinstance(storage, LocalStorage):
                input_path = storage.local_path(expected_path)
                if not input_path.is_file():
                    raise FileNotFoundError(f"Video object is missing: {expected_path}")
                original_size, original_sha256 = _hash_file(input_path, should_cancel)
            else:
                suffix = Path(expected_path).suffix or ".video"
                input_path = directory_path / f"input{suffix}"
                original_size, original_sha256 = _copy_storage_file(
                    storage, expected_path, input_path, should_cancel
                )

            if should_cancel():
                raise CompressionCancelledError("Оптимизация отменена")
            original_duration, original_has_video = _probe_media(input_path)
            if not original_has_video:
                raise RuntimeError("Исходный файл не содержит видеопоток")

            output_path = directory_path / "optimized.mp4"
            run_ffmpeg_compression(input_path, output_path, should_cancel)
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                raise RuntimeError("FFmpeg не создал выходной файл")
            output_duration, output_has_video = _probe_media(output_path)
            if not output_has_video:
                raise RuntimeError("Выходной файл не содержит видеопоток")
            if abs(output_duration - original_duration) > max(2.0, original_duration * 0.02):
                raise RuntimeError("Длительность выходного файла не совпадает с исходной")

            output_size, output_sha256 = _hash_file(output_path, should_cancel)
            if output_size >= original_size:
                return (
                    "skipped"
                    if _set_result(
                        video_id,
                        expected_path,
                        status="skipped",
                        file_size=original_size,
                        sha256=original_sha256,
                    )
                    else "stale"
                )

            if should_cancel():
                raise CompressionCancelledError("Оптимизация отменена")
            new_storage_path = f"video_archiver/videos/optimized/{uuid.uuid4().hex}.mp4"
            storage.save_file_from_path(output_path, new_storage_path)
            if should_cancel():
                storage.delete_file(new_storage_path)
                new_storage_path = None
                raise CompressionCancelledError("Оптимизация отменена")
            switched = _set_result(
                video_id,
                expected_path,
                status="switching",
                file_size=output_size,
                sha256=output_sha256,
                error=f"old_path:{expected_path}",
                new_path=new_storage_path,
            )
            if not switched:
                storage.delete_file(new_storage_path)
                return "stale"
            new_object_owned_by_db = True
            return _finish_switch(video_id, new_storage_path, expected_path, storage)
    except CompressionCancelledError as exc:
        _set_result(
            video_id,
            expected_path,
            status="cancelled",
            file_size=original_size,
            sha256=original_sha256,
            error=str(exc),
        )
        raise
    except Exception as exc:
        if new_object_owned_by_db:
            logger.exception("Could not finalize DB-owned video object for %s", video_id)
            return "failed"
        _set_result(
            video_id,
            expected_path,
            status="failed",
            file_size=original_size,
            sha256=original_sha256,
            error=str(exc),
        )
        logger.exception("Video optimization failed for %s", video_id)
        return "failed"


def _lock_script(action: str) -> str:
    if action == "acquire":
        return (
            "local current = redis.call('get', KEYS[1]); "
            "if current == ARGV[1] or not current then "
            "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1 else return 0 end"
        )
    if action == "refresh":
        return (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "redis.call('expire', KEYS[1], ARGV[2]); redis.call('expire', KEYS[2], ARGV[2]); "
            "return 1 else return 0 end"
        )
    return "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def _acquire_compression_lease(task_id: str, redis_client) -> str | None:
    lease_token = f"{task_id}:{uuid.uuid4().hex}"
    acquired = redis_client.eval(
        _lock_script("acquire"),
        1,
        COMPRESSION_LOCK_KEY,
        task_id,
        lease_token,
        COMPRESSION_LOCK_TTL,
    )
    return lease_token if acquired else None


def run_compression_batch(task_id: str, redis_client) -> str:
    tracker_key = f"video_compress:{task_id}"
    cancel_key = f"video_compress_cancel:{task_id}"
    lease_token = _acquire_compression_lease(task_id, redis_client)
    if not lease_token:
        return "Another delivery already owns the video optimization lease."
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(30):
            try:
                redis_client.eval(
                    _lock_script("refresh"),
                    2,
                    COMPRESSION_LOCK_KEY,
                    tracker_key,
                    lease_token,
                    COMPRESSION_LOCK_TTL,
                )
            except Exception:
                logger.exception("Could not refresh video compression lock")

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    cancel_state = {"checked_at": 0.0, "cancelled": False}

    def cancelled() -> bool:
        now = time.monotonic()
        if not cancel_state["cancelled"] and now - cancel_state["checked_at"] >= 1:
            cancel_state["checked_at"] = now
            cancel_state["cancelled"] = bool(redis_client.exists(cancel_key))
        return bool(cancel_state["cancelled"])

    def progress(processed: int, total: int, counts: dict[str, int], current: str, state="running"):
        percent = 100 if not total else int(processed * 100 / total)
        status = (
            f"Обработано {processed} из {total}. Сжато: {counts['completed']}, "
            f"без выгоды: {counts['skipped']}, ошибок: {counts['failed']}"
        )
        redis_client.setex(
            tracker_key,
            COMPRESSION_TRACKER_TTL,
            json.dumps(
                {
                    "task_id": task_id,
                    "title": "Оптимизация видео",
                    "status": status,
                    "progress": f"{percent}%",
                    "state": state,
                    "type": "video_compress",
                    "cancel_mode": "cooperative",
                    "current_video": current,
                    "total_count": total,
                    **{f"{key}_count": value for key, value in counts.items()},
                }
            ),
        )

    counts = {"completed": 0, "skipped": 0, "failed": 0, "stale": 0}
    try:
        with SyncSessionLocal() as session:
            video_ids = compression_candidate_ids(session)
        total = len(video_ids)
        progress(0, total, counts, "")
        for index, video_id in enumerate(video_ids, 1):
            if cancelled():
                progress(index - 1, total, counts, "", state="cancelled")
                return "Video optimization cancelled."
            try:
                result = optimize_video(video_id, should_cancel=cancelled)
            except CompressionCancelledError:
                progress(index - 1, total, counts, video_id, state="cancelled")
                return "Video optimization cancelled."
            counts[result] += 1
            progress(index, total, counts, video_id)
        progress(total, total, counts, "", state="completed")
        return (
            f"Processed {total} videos. Compressed: {counts['completed']}, "
            f"skipped: {counts['skipped']}, failed: {counts['failed']}."
        )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        redis_client.delete(cancel_key)
        redis_client.eval(_lock_script("release"), 1, COMPRESSION_LOCK_KEY, lease_token)
