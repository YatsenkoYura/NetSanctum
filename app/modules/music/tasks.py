"""
Music module Celery tasks.
"""

import os
import uuid
from typing import Optional

import requests
import yt_dlp
from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.storage import get_storage
from app.modules.settings.models import Setting
from app.modules.music.models import Playlist, Song, PlaylistSong
from app.modules.music.schemas import MusicModel, VideoModel

import logging
import json
import redis

logger = logging.getLogger(__name__)

redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)


import tempfile

def _get_api_keys() -> tuple[str, str]:
    """Retrieve OpenAI API key and Base URL from the database."""
    with SyncSessionLocal() as session:
        api_key = session.scalar(select(Setting.value).where(Setting.key == "openai_api_key"))
        base_url = session.scalar(select(Setting.value).where(Setting.key == "openai_base_url"))
        return api_key or "", base_url or ""

def _create_cookies_file() -> str | None:
    """Retrieve cookies from DB and write to a temporary file."""
    with SyncSessionLocal() as session:
        cookies = session.scalar(select(Setting.value).where(Setting.key == "youtube_cookies"))
    if cookies and cookies.strip():
        fd, path = tempfile.mkstemp(suffix=".txt", text=True)
        with os.fdopen(fd, "w") as f:
            f.write(cookies)
        return path
    return None


@celery_app.task(bind=True)
def process_youtube_url_task(
    self,
    url: str,
    use_ai: bool = True,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
    playlist_id: int | None = None
) -> str:
    """Entry point for processing a YouTube URL (video or playlist)."""
    task_id = self.request.id
    
    def update_redis_status(status_text: str):
        data = {
            "task_id": task_id,
            "url": url,
            "title": "Resolving URL...",
            "status": status_text,
            "progress": "0%"
        }
        redis_client.setex(f"music_dl:{task_id}", 86400, json.dumps(data))
        
    update_redis_status("Fetching info...")
    ydl_opts = {
        'quiet': True,
        'extract_flat': 'in_playlist',
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'client': ['android', 'web']
            }
        },
        'js_runtimes': {'node': {}},
        'remote_components': {'ejs:github': {}}
    }
    
    cookie_path = _create_cookies_file()
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error(f"Error fetching info for URL {url}: {e}")
        update_redis_status(f"Error: {str(e)[:50]}...")
        return f"Error: {e}"
    finally:
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)

    if 'entries' in info_dict:
        # It's a playlist
        playlist_title = info_dict.get('title', 'Unknown Playlist')
        playlist_description = info_dict.get('description', '')

        if playlist_id is None:
            with SyncSessionLocal() as session:
                playlist = Playlist(name=playlist_title, description=playlist_description)
                session.add(playlist)
                session.commit()
                session.refresh(playlist)
                playlist_id = playlist.id

        entries = list(info_dict['entries'])
        logger.info(f"Playlist detected: {playlist_title} with {len(entries)} videos.")

        with SyncSessionLocal() as session:
            for i, entry in enumerate(entries):
                video_url = entry.get('url')
                if not video_url:
                    video_url = f"https://www.youtube.com/watch?v={entry.get('id')}"
                
                existing_song = session.scalar(select(Song).where(Song.youtube_url == video_url))
                if existing_song:
                    ps = PlaylistSong(playlist_id=playlist_id, song_id=existing_song.id, position=i)
                    session.add(ps)
                    session.commit()
                    logger.info(f"Deduplication: Linked existing song {video_url} to playlist")
                else:
                    task = process_song_task.delay(video_url, playlist_id, i, use_ai, openai_api_key, openai_base_url)
                    data = {
                        "task_id": task.id,
                        "url": video_url,
                        "title": f"Song {i+1} (Queued)",
                        "status": "Queued",
                        "progress": "0%"
                    }
                    redis_client.setex(f"music_dl:{task.id}", 86400, json.dumps(data))

        redis_client.delete(f"music_dl:{task_id}")
        return f"Dispatched {len(entries)} songs for playlist '{playlist_title}'"
    else:
        # Single video
        with SyncSessionLocal() as session:
            existing_song = session.scalar(select(Song).where(Song.youtube_url == url))
            if existing_song:
                if playlist_id:
                    # Link to the provided playlist
                    ps = session.scalar(select(PlaylistSong).where(PlaylistSong.playlist_id == playlist_id, PlaylistSong.song_id == existing_song.id))
                    if not ps:
                        ps = PlaylistSong(playlist_id=playlist_id, song_id=existing_song.id, position=0)
                        session.add(ps)
                        session.commit()
                        redis_client.delete(f"music_dl:{task_id}")
                        return f"Song already existed. Linked to playlist {playlist_id}."
                redis_client.delete(f"music_dl:{task_id}")
                return "Song already exists in Library."
        
        task = process_song_task.delay(url, playlist_id, 0, use_ai, openai_api_key, openai_base_url)
        data = {
            "task_id": task.id,
            "url": url,
            "title": "Resolving video...",
            "status": "Queued",
            "progress": "0%"
        }
        redis_client.setex(f"music_dl:{task.id}", 86400, json.dumps(data))
        redis_client.delete(f"music_dl:{task_id}")
        return "Dispatched single video"


@celery_app.task(bind=True)
def process_song_task(
    self,
    url: str,
    playlist_id: Optional[int],
    position: int = 0,
    use_ai: bool = True,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None
) -> str:
    """Download video, extract MP3, download cover, analyze with AI, and save to DB."""
    
    task_id = self.request.id
    
    def update_redis_status(status_text: str, percent: str = "0%"):
        data = {
            "task_id": task_id,
            "url": url,
            "title": "Fetching Metadata...",
            "status": status_text,
            "progress": percent
        }
        redis_client.setex(f"music_dl:{task_id}", 86400, json.dumps(data))
        
    update_redis_status("Preparing")
    
    # 1. Fetch metadata and comments for AI
    ydl_opts_meta = {
        'quiet': True,
        'extract_flat': False,
        'getcomments': use_ai,
        'max_comments': 50,
        'extractor_args': {
            'youtube': {
                'comment_sort': ['top'],
                'player_client': ['android', 'web'],
                'client': ['android', 'web']
            }
        },
        'js_runtimes': {'node': {}},
        'remote_components': {'ejs:github': {}}
    }
    
    cookie_path = _create_cookies_file()
    if cookie_path:
        ydl_opts_meta['cookiefile'] = cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts_meta) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            comments_data = info_dict.get('comments') or []
    except Exception as e:
        logger.error(f"Failed to fetch metadata for {url}: {e}")
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)
        update_redis_status(f"Error: {str(e)[:50]}...")
        return f"Error: {e}"

    all_comments = []
    if use_ai:
        for c in comments_data:
            text = c.get('text', '')
            if len(text) > 200:
                all_comments.append(text)

    video_data = VideoModel(
        title=info_dict.get('title', 'Unknown'),
        description=info_dict.get('description', ''),
        comments=all_comments
    )
    
    # Update redis with actual title
    def update_redis_status(status_text: str, percent: str = "0%"):
        data = {
            "task_id": task_id,
            "url": url,
            "title": video_data.title,
            "status": status_text,
            "progress": percent
        }
        redis_client.setex(f"music_dl:{task_id}", 86400, json.dumps(data))
        
    update_redis_status("Analyzing AI (Optional)")

    # 2. Analyze with AI
    db_api_key, db_base_url = _get_api_keys()
    
    api_key = openai_api_key or db_api_key
    base_url = openai_base_url or db_base_url
    
    music_info = None
    if use_ai and api_key and base_url:
        try:
            client = OpenAI(base_url=base_url, api_key=api_key)
            context_text = f"НАЗВАНИЕ ВИДЕО:\n{video_data.title}\n\n"
            context_text += f"ОПИСАНИЕ ВИДЕО:\n{video_data.description}\n\n"
            context_text += "КОММЕНТАРИИ:\n"
            context_text += "\n---\n".join(video_data.comments)

            system_prompt = (
                "Ты — музыкальный AI-агент, эксперт по анализу метаданных. "
                "Пользователь передаст тебе название, описание и комментарии из видео на YouTube. "
                "Твоя задача — тщательно изучить этот текст и извлечь информацию о песне строго по запрошенной схеме. "
                "Особое внимание удели разнице между авторами кавера и оригинальными исполнителями. "
                "Не выдумывай текст песни, если его нет в предоставленных данных."
            )

            completion = client.beta.chat.completions.parse(
                model="gemini-3-flash-preview", # Can be configured
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": context_text}
                ],
                response_format=MusicModel,
                temperature=0.1
            )
            music_info = completion.choices[0].message.parsed
        except Exception as e:
            logger.error(f"AI Analysis failed: {e}")

    # Fallback if AI fails or no keys
    if not music_info:
        music_info = MusicModel(
            title=video_data.title,
            author=info_dict.get('uploader', 'Unknown'),
            original_artist=None
        )

    update_redis_status("Starting Download")
    
    def progress_hook(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            # Strip ANSI escape codes sometimes produced by yt-dlp
            import re
            percent = re.sub(r'\x1b[^m]*m', '', percent)
            update_redis_status("Downloading", percent)
        elif d['status'] == 'finished':
            update_redis_status("Extracting Audio", "100%")

    # 3. Download Audio
    import glob
    download_dir = "/tmp"
    download_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'best',
            'preferredquality': '192',
        }],
        'writethumbnail': True,
        'quiet': True,
        'progress_hooks': [progress_hook],
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'client': ['android', 'web']
            }
        },
        'js_runtimes': {'node': {}},
        'remote_components': {'ejs:github': {}},
        'outtmpl': f'{download_dir}/%(id)s.%(ext)s',
        'noplaylist': True,
    }
    
    if cookie_path:
        download_opts['cookiefile'] = cookie_path

    audio_file_id = None
    try:
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info['id']
            
            # Find the extracted audio file
            downloaded_files = glob.glob(os.path.join(download_dir, f"{video_id}.*"))
            audio_exts = {'.mp3', '.m4a', '.webm', '.opus', '.aac', '.flac', '.wav', '.ogg'}
            audio_filepath = None
            for f in downloaded_files:
                if os.path.splitext(f)[1].lower() in audio_exts:
                    audio_filepath = f
                    break
            
            if audio_filepath:
                with open(audio_filepath, "rb") as f:
                    audio_data = f.read()
                ext = os.path.splitext(audio_filepath)[1].lower()
                audio_file_id = get_storage().save_file(audio_data, f"music/audio/{video_id}{ext}")
                os.remove(audio_filepath)
            else:
                logger.error(f"No valid audio file found for {url}")
                update_redis_status("Error: No valid audio file found")
                return "Error: No valid audio file found"
    except Exception as e:
        logger.error(f"Failed to download audio for {url}: {e}")
        update_redis_status(f"Error: {str(e)[:50]}...")
        return f"Error downloading audio: {e}"
    finally:
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)

    # 4. Download Cover
    cover_file_id = None
    thumbnail_url = info_dict.get('thumbnail')
    if thumbnail_url:
        try:
            resp = requests.get(thumbnail_url, timeout=10)
            if resp.status_code == 200:
                ext = "jpg"
                if "webp" in resp.headers.get("Content-Type", ""):
                    ext = "webp"
                
                cover_file_id = get_storage().save_file(
                    resp.content, f"music/covers/{video_id}.{ext}"
                )
        except Exception as e:
            logger.warning(f"Failed to download thumbnail for {url}: {e}")

    # 5. Save to Database
    with SyncSessionLocal() as session:
        song = Song(
            title=music_info.title,
            author=music_info.author,
            original_artist=music_info.original_artist,
            cover_file_id=cover_file_id,
            audio_file_id=audio_file_id,
            youtube_url=url
        )
        session.add(song)
        session.flush() # flush to get song.id
        
        if playlist_id:
            ps = PlaylistSong(playlist_id=playlist_id, song_id=song.id, position=position)
            session.add(ps)
            
        session.commit()
        logger.info(f"Successfully processed and saved song: {song.title}")
        
        # Clean up redis status
        redis_client.delete(f"music_dl:{task_id}")

    return f"Processed: {music_info.title}"
