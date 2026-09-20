"""
Music module Celery tasks.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import redis
import requests
from openai import OpenAI
from sqlalchemy import select

from app.core.browser_snapshots import browser_snapshot_store
from app.core.config import get_settings
from app.core.database import SyncSessionLocal
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked
from app.core.scheduler import celery_app
from app.core.secret_values import decrypt_secret_value
from app.core.storage import file_size_sha256, get_storage
from app.core.task_dispatch import dispatch_tracked_sync
from app.core.ytdlp_pipeline import (
    YtDlpPipelineError,
    error_status,
    extract_info,
    is_youtube_single_video_url,
    is_youtube_url,
)
from app.modules.music.models import Playlist, PlaylistSong, Song
from app.modules.music.schemas import MusicModel, VideoModel
from app.modules.music.security import MUSIC_IMAGE_HOSTS, validate_music_url
from app.modules.settings.models import Setting

logger = logging.getLogger(__name__)


def _save_audio_file(storage, source_path: Path, destination: str) -> tuple[str, int, str]:
    """Persist audio without buffering it and return its exact content identity."""
    file_size, sha256 = file_size_sha256(source_path)
    file_id = storage.save_file_from_path(source_path, destination)
    return file_id, file_size, sha256


def _regenerate_playlist_cover(session, playlist_id: int) -> None:
    from app.modules.music.covers import regenerate_playlist_cover

    playlist = session.get(Playlist, playlist_id)
    if not playlist:
        return
    paths = session.scalars(
        select(Song.cover_file_id)
        .join(PlaylistSong)
        .where(PlaylistSong.playlist_id == playlist_id, Song.cover_file_id.isnot(None))
        .order_by(PlaylistSong.position, PlaylistSong.song_id)
        .limit(9)
    ).all()
    playlist.cover_path = regenerate_playlist_cover(playlist, paths)


redis_client = redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)


def _set_conversion_status(
    task_id: str,
    *,
    source_id: str,
    title: str,
    state: str,
    status: str,
    progress: int,
    **result,
) -> None:
    payload = {
        "task_id": task_id,
        "source_id": source_id,
        "title": title,
        "state": state,
        "status": status,
        "progress": f"{progress}%",
        "progress_percent": progress,
        **result,
    }
    ttl = 3600 if state in {"completed", "failed"} else 86400
    try:
        redis_client.setex(f"music_convert:{task_id}", ttl, json.dumps(payload))
    except Exception as exc:
        logger.warning("Could not update conversion status for %s: %s", task_id, exc)


def _extract_archived_audio(
    storage_path: str,
    output_path: Path,
    duration: int,
    progress_callback: Callable[[int], None],
) -> None:
    """Materialize a stored video and extract a predictable MP3 with FFmpeg."""
    storage = get_storage()
    input_suffix = Path(storage_path).suffix or ".video"
    input_path = output_path.with_name(f"source{input_suffix}")
    source = storage.get_file_stream(storage_path)
    try:
        with input_path.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
    finally:
        source.close()

    media_duration = float(duration)
    if media_duration <= 0:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(input_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            media_duration = float(probe.stdout.strip())
        except ValueError:
            media_duration = 0

    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]
    with tempfile.TemporaryFile(mode="w+t") as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            key, _, value = raw_line.strip().partition("=")
            if media_duration > 0 and key in {"out_time_us", "out_time_ms"} and value.isdigit():
                elapsed_seconds = int(value) / 1_000_000
                progress_callback(min(90, 10 + int(80 * elapsed_seconds / media_duration)))
        return_code = process.wait()
        if return_code != 0 or not output_path.is_file():
            errors.seek(0)
            detail = errors.read().strip()[-1000:]
            raise RuntimeError(detail or f"FFmpeg exited with status {return_code}")


@celery_app.task(bind=True)
def convert_archived_video_task(
    self,
    source_id: str,
    storage_path: str,
    title: str,
    author: str | None = None,
    description: str | None = None,
    source_url: str | None = None,
    thumbnail_path: str | None = None,
    duration: int = 0,
) -> str:
    """Extract an archived video's audio into the Music library."""
    task_id = self.request.id
    audio_file_id = None
    audio_file_size = None
    audio_sha256 = None
    cover_file_id = None
    source_reference = (source_url or f"archive:{source_id}")[:255]

    def update(state: str, status: str, progress: int, **result) -> None:
        _set_conversion_status(
            task_id,
            source_id=source_id,
            title=title,
            state=state,
            status=status,
            progress=progress,
            **result,
        )

    update("running", "Checking source", 2)
    storage = get_storage()
    try:
        if not storage.file_exists(storage_path):
            raise FileNotFoundError("Archived video file is unavailable")

        with SyncSessionLocal() as session:
            existing_song = session.scalar(select(Song).where(Song.youtube_url == source_reference))
            if existing_song and storage.file_exists(existing_song.audio_file_id):
                update(
                    "completed",
                    "Already available",
                    100,
                    song_id=existing_song.id,
                    audio_url=f"/music/audio/{existing_song.id}",
                )
                return f"Already available: {existing_song.title}"

        with tempfile.TemporaryDirectory(prefix="netsanctum_archive_audio_") as temp_dir:
            audio_path = Path(temp_dir) / "audio.mp3"
            update("running", "Preparing archived video", 5)
            _extract_archived_audio(
                storage_path,
                audio_path,
                duration,
                lambda progress: update("running", "Extracting audio", progress),
            )
            update("running", "Saving audio", 94)
            audio_file_id, audio_file_size, audio_sha256 = _save_audio_file(
                storage,
                audio_path,
                f"music/audio/archive-{task_id}.mp3",
            )

        if thumbnail_path and storage.file_exists(thumbnail_path):
            cover_suffix = Path(thumbnail_path).suffix.lower()
            if cover_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                cover_suffix = ".jpg"
            with storage.get_file_stream(thumbnail_path) as cover:
                cover_file_id = storage.save_file(
                    cover.read(),
                    f"music/covers/archive-{task_id}{cover_suffix}",
                )

        update("running", "Adding to Music library", 98)
        with SyncSessionLocal() as session:
            song = Song(
                title=title,
                author=author or "Unknown",
                original_artist=None,
                cover_file_id=cover_file_id,
                audio_file_id=audio_file_id,
                audio_file_size=audio_file_size,
                audio_sha256=audio_sha256,
                youtube_url=source_reference,
            )
            session.add(song)
            session.commit()
            session.refresh(song)
            song_id = song.id

        update(
            "completed",
            "Conversion completed",
            100,
            song_id=song_id,
            audio_url=f"/music/audio/{song_id}",
        )
        return f"Converted archived video: {title}"
    except Exception as exc:
        if audio_file_id:
            storage.delete_file(audio_file_id)
        if cover_file_id:
            storage.delete_file(cover_file_id)
        message = str(exc) or exc.__class__.__name__
        logger.error("Failed to convert archived video %s: %s", source_id, message)
        update("failed", f"Conversion failed: {message}", 0, error=message)
        return f"Error converting archived video: {message}"


def _get_api_keys() -> tuple[str, str]:
    """Retrieve OpenAI API key and Base URL from the database."""
    with SyncSessionLocal() as session:
        api_key = session.scalar(select(Setting.value).where(Setting.key == "openai_api_key"))
        base_url = session.scalar(select(Setting.value).where(Setting.key == "openai_base_url"))
        return decrypt_secret_value(api_key), base_url or ""


def _get_youtube_cookies() -> str | None:
    """Retrieve the encrypted global YouTube cookie jar without writing it to the task payload."""
    browser_cookies = browser_snapshot_store.cookies_for_scope_sync("youtube")
    if browser_cookies:
        return browser_cookies
    with SyncSessionLocal() as session:
        cookies = session.scalar(
            select(Setting.value).where(Setting.key == "youtube_cookies", Setting.scope == "global")
        )
    return decrypt_secret_value(cookies) or None


def _youtube_entry_url(entry: dict) -> str:
    candidate = entry.get("webpage_url") or entry.get("url")
    if candidate and str(candidate).startswith(("http://", "https://")):
        return str(candidate)
    return f"https://www.youtube.com/watch?v={entry.get('id')}"


@celery_app.task(bind=True)
def process_youtube_url_task(
    self,
    url: str,
    use_ai: bool = True,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
    playlist_id: int | None = None,
) -> str:
    """Entry point for processing any supported music URL (YouTube, Spotify, SoundCloud, etc.)."""
    task_id = self.request.id

    def update_redis_status(status_text: str, state: str = "running"):
        data = {
            "task_id": task_id,
            "url": url,
            "title": "Resolving URL...",
            "status": status_text,
            "progress": "0%",
            "state": state,
        }
        redis_client.setex(f"music_dl:{task_id}", 86400, json.dumps(data))

    update_redis_status("Fetching info...")
    try:
        validate_music_url(url)
    except ValueError as exc:
        update_redis_status(str(exc), "failed")
        return f"Error: {exc}"
    ydl_opts = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "extractor_args": {
            "soundcloud": {
                "client_id": ["iZ8g4fk7bchWS1uTXWeKwMzhf9yC68gR", "a3e059563d7fd3372b49b37f00a00bcf"]
            },
        },
    }
    cookies_text = _get_youtube_cookies()

    # Check if URL is Spotify
    is_spotify = (urlparse(url).hostname or "").lower().rstrip(".").endswith("spotify.com")
    info_dict = None
    skip_resolver = is_youtube_single_video_url(url)

    if is_spotify:
        logger.info(f"Spotify link detected ({url}), resolving metadata via oEmbed...")
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={url}"
            resp = requests.get(oembed_url, params={"url": url}, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                spotify_title = data.get("title", "")
                # Spotify oEmbed title is often "Track Title - song by Artist | Spotify" or "Track Title"
                # Let's clean it up
                clean_query = spotify_title.replace("| Spotify", "").strip()
                logger.info(f"Resolved Spotify oEmbed query: '{clean_query}'")

                search_res = extract_info(
                    redis_client,
                    f"ytsearch1:{clean_query}",
                    options=ydl_opts,
                    cookies_text=cookies_text,
                )
                entries = list(search_res.get("entries") or [])
                if entries:
                    info_dict = entries[0]
                    url = _youtube_entry_url(info_dict)
        except YtDlpPipelineError as exc:
            message = error_status(exc)
            logger.error("Spotify YouTube resolution failed: %s", message)
            update_redis_status(message, "failed")
            return f"Error: {message}"
        except Exception as oembed_err:
            logger.warning(f"Spotify oEmbed resolution failed: {oembed_err}")

    if not info_dict and not skip_resolver:
        try:
            platform = "youtube" if is_youtube_url(url) else "soundcloud"
            info_dict = extract_info(
                redis_client,
                url,
                options=ydl_opts,
                cookies_text=cookies_text if platform == "youtube" else None,
                platform=platform,
            )
        except YtDlpPipelineError as exc:
            err_msg = error_status(exc)
            if "DRM" in err_msg or is_spotify:
                logger.info(f"DRM restriction encountered for {url}, attempting YouTube fallback search...")
                try:
                    # Clean title from URL path (e.g., https://soundcloud.com/onsa-media/jailbreak -> onsa media jailbreak)
                    url_clean = url.rstrip("/").split("?")[0]
                    parts = url_clean.split("/")[-2:]
                    search_query = " ".join(parts).replace("-", " ").replace("_", " ")
                    logger.info(f"Fallback searching YouTube for: '{search_query}'")

                    search_res = extract_info(
                        redis_client,
                        f"ytsearch1:{search_query}",
                        options=ydl_opts,
                        cookies_text=cookies_text,
                    )
                    entries = list(search_res.get("entries") or [])
                    if not entries:
                        raise RuntimeError("No search results found")
                    info_dict = entries[0]
                    url = _youtube_entry_url(info_dict)
                except Exception as fb_err:
                    update_redis_status(f"Could not resolve track: {error_status(fb_err)}", "failed")
                    return f"Error: Fallback search failed ({fb_err})"
            else:
                logger.error("Error fetching info for URL %s: %s", url, err_msg)
                update_redis_status(err_msg, "failed")
                return f"Error: {err_msg}"

    if info_dict is None:
        info_dict = {}

    if "entries" in info_dict:
        # It's a playlist
        playlist_title = info_dict.get("title", "Unknown Playlist")
        playlist_description = info_dict.get("description", "")

        if playlist_id is None:
            with SyncSessionLocal() as session:
                playlist = Playlist(
                    name=playlist_title,
                    description=playlist_description,
                    source_url=url,
                )
                session.add(playlist)
                session.commit()
                session.refresh(playlist)
                playlist_id = playlist.id
        else:
            with SyncSessionLocal() as session:
                playlist = session.get(Playlist, playlist_id)
                if not playlist:
                    update_redis_status(f"Playlist {playlist_id} was not found", "failed")
                    return f"Error: Playlist {playlist_id} was not found"
                playlist.name = playlist_title
                playlist.description = playlist_description
                playlist.source_url = url
                session.commit()

        entries = list(info_dict["entries"])
        logger.info(f"Playlist detected: {playlist_title} with {len(entries)} videos.")

        with SyncSessionLocal() as session:
            changed = False
            for i, entry in enumerate(entries):
                video_url = _youtube_entry_url(entry)

                existing_song = session.scalar(select(Song).where(Song.youtube_url == video_url))
                if existing_song:
                    existing_link = session.scalar(
                        select(PlaylistSong).where(
                            PlaylistSong.playlist_id == playlist_id,
                            PlaylistSong.song_id == existing_song.id,
                        )
                    )
                    if not existing_link:
                        session.add(
                            PlaylistSong(playlist_id=playlist_id, song_id=existing_song.id, position=i)
                        )
                        session.commit()
                        changed = True
                        logger.info(f"Deduplication: Linked existing song {video_url} to playlist")
                else:
                    dispatch_tracked_sync(
                        process_song_task,
                        redis_client,
                        "music_dl",
                        {
                            "url": video_url,
                            "title": f"Song {i + 1} (Queued)",
                            "status": "Queued",
                            "progress": "0%",
                        },
                        args=(video_url, playlist_id, i, use_ai, openai_api_key, openai_base_url),
                    )
            if changed and playlist_id is not None:
                _regenerate_playlist_cover(session, playlist_id)
                session.commit()

        redis_client.delete(f"music_dl:{task_id}")
        return f"Dispatched {len(entries)} songs for playlist '{playlist_title}'"
    else:
        # Single video
        with SyncSessionLocal() as session:
            existing_song = session.scalar(select(Song).where(Song.youtube_url == url))
            if existing_song:
                if playlist_id:
                    # Link to the provided playlist
                    ps = session.scalar(
                        select(PlaylistSong).where(
                            PlaylistSong.playlist_id == playlist_id, PlaylistSong.song_id == existing_song.id
                        )
                    )
                    if not ps:
                        ps = PlaylistSong(playlist_id=playlist_id, song_id=existing_song.id, position=0)
                        session.add(ps)
                        session.commit()
                        _regenerate_playlist_cover(session, playlist_id)
                        session.commit()
                        redis_client.delete(f"music_dl:{task_id}")
                        return f"Song already existed. Linked to playlist {playlist_id}."
                redis_client.delete(f"music_dl:{task_id}")
                return "Song already exists in Library."

        dispatch_tracked_sync(
            process_song_task,
            redis_client,
            "music_dl",
            {"url": url, "title": "Resolving video...", "status": "Queued", "progress": "0%"},
            args=(url, playlist_id, 0, use_ai, openai_api_key, openai_base_url),
        )
        redis_client.delete(f"music_dl:{task_id}")
        return "Dispatched single video"


@celery_app.task(bind=True)
def process_song_task(
    self,
    url: str,
    playlist_id: int | None,
    position: int = 0,
    use_ai: bool = True,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
) -> str:
    """Download video, extract MP3, download cover, analyze with AI, and save to DB."""

    task_id = self.request.id
    display_title = "Fetching Metadata..."

    def update_redis_status(status_text: str, percent: str = "0%", state: str = "running"):
        data = {
            "task_id": task_id,
            "url": url,
            "title": display_title,
            "status": status_text,
            "progress": percent,
            "state": state,
        }
        redis_client.setex(f"music_dl:{task_id}", 86400, json.dumps(data))

    update_redis_status("Preparing")

    update_redis_status("Starting Download")

    def progress_hook(d):
        if d["status"] == "downloading":
            percent = d.get("_percent_str", "0%").strip()
            # Strip ANSI escape codes sometimes produced by yt-dlp
            import re

            percent = re.sub(r"\x1b[^m]*m", "", percent)
            update_redis_status("Downloading", percent)
        elif d["status"] == "finished":
            update_redis_status("Extracting Audio", "100%")

    # A single yt-dlp invocation downloads audio and returns the metadata used below.
    import glob

    download_dir = tempfile.mkdtemp(prefix="netsanctum_music_")
    download_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "best",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "progress_hooks": [progress_hook],
        "extractor_args": {
            "soundcloud": {
                "client_id": ["iZ8g4fk7bchWS1uTXWeKwMzhf9yC68gR", "a3e059563d7fd3372b49b37f00a00bcf"]
            },
        },
        "outtmpl": f"{download_dir}/%(id)s.%(ext)s",
        "noplaylist": True,
        "check_formats": "selected",
    }

    audio_file_id = None
    audio_file_size = None
    audio_sha256 = None
    try:
        youtube = is_youtube_url(url)
        info_dict = extract_info(
            redis_client,
            url,
            options=download_opts,
            download=True,
            cookies_text=_get_youtube_cookies() if youtube else None,
            platform="youtube" if youtube else "soundcloud",
        )
        video_id = str(info_dict.get("id") or "")
        if not video_id:
            raise RuntimeError("yt-dlp did not return a media id")

        downloaded_files = glob.glob(os.path.join(download_dir, f"{video_id}.*"))
        audio_exts = {".mp3", ".m4a", ".webm", ".opus", ".aac", ".flac", ".wav", ".ogg"}
        audio_filepath = next(
            (path for path in downloaded_files if os.path.splitext(path)[1].lower() in audio_exts),
            None,
        )
        if not audio_filepath:
            raise FileNotFoundError("No valid audio file found")
        ext = os.path.splitext(audio_filepath)[1].lower()
        audio_file_id, audio_file_size, audio_sha256 = _save_audio_file(
            get_storage(),
            Path(audio_filepath),
            f"music/audio/{video_id}{ext}",
        )
        os.remove(audio_filepath)
    except Exception as exc:
        message = error_status(exc)
        logger.error("Failed to download audio for %s: %s", url, message)
        update_redis_status(message, state="failed")
        return f"Error downloading audio: {message}"
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)

    video_data = VideoModel(
        title=info_dict.get("title") or "Unknown",
        description=info_dict.get("description") or "",
    )
    display_title = video_data.title

    update_redis_status("Analyzing AI (Optional)")
    db_api_key, db_base_url = _get_api_keys()
    api_key = openai_api_key or db_api_key
    base_url = openai_base_url if openai_api_key and openai_base_url else db_base_url

    music_info = None
    if use_ai and api_key and base_url:
        try:
            client = OpenAI(base_url=base_url, api_key=api_key)
            context_text = (
                f"НАЗВАНИЕ ВИДЕО:\n{video_data.title}\n\nОПИСАНИЕ ВИДЕО:\n{video_data.description}\n"
            )
            system_prompt = (
                "Ты — музыкальный AI-агент, эксперт по анализу метаданных. "
                "Извлеки информацию о песне строго по запрошенной схеме. "
                "Особое внимание удели разнице между авторами кавера и оригинальными исполнителями."
            )
            completion = client.beta.chat.completions.parse(
                model="gemini-3-flash-preview",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": context_text},
                ],
                response_format=MusicModel,
                temperature=0.1,
            )
            music_info = completion.choices[0].message.parsed
        except Exception as exc:
            logger.error("AI analysis failed: %s", exc)

    if not music_info:
        music_info = MusicModel(
            title=video_data.title,
            author=info_dict.get("uploader") or "Unknown",
            original_artist=None,
        )

    # Download Cover
    cover_file_id = None
    thumbnail_url = info_dict.get("thumbnail")
    if thumbnail_url:
        try:
            content, content_type, _ = fetch_bytes_checked(
                thumbnail_url,
                allowed_hosts=MUSIC_IMAGE_HOSTS,
            )
            ext = "webp" if content_type == "image/webp" else "png" if content_type == "image/png" else "jpg"
            cover_file_id = get_storage().save_file(content, f"music/covers/{video_id}.{ext}")
        except (RemoteFetchError, OSError) as e:
            logger.warning(f"Failed to download thumbnail for {url}: {e}")

    # 5. Save to Database
    with SyncSessionLocal() as session:
        song = Song(
            title=music_info.title,
            author=music_info.author,
            original_artist=music_info.original_artist,
            cover_file_id=cover_file_id,
            audio_file_id=audio_file_id,
            audio_file_size=audio_file_size,
            audio_sha256=audio_sha256,
            youtube_url=url,
        )
        session.add(song)
        session.flush()  # flush to get song.id

        if playlist_id:
            ps = PlaylistSong(playlist_id=playlist_id, song_id=song.id, position=position)
            session.add(ps)

        session.commit()
        if playlist_id:
            _regenerate_playlist_cover(session, playlist_id)
            session.commit()
        logger.info(f"Successfully processed and saved song: {song.title}")

        # Clean up redis status
        redis_client.delete(f"music_dl:{task_id}")

    return f"Processed: {music_info.title}"
