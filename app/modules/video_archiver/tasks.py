import os
import json
import tempfile
import logging
import requests
import datetime
import yt_dlp
import redis
from sqlalchemy import select
from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.storage import get_storage
from app.core.config import get_settings
from app.modules.video_archiver.models import ArchivedVideo, VideoPlaylist

logger = logging.getLogger(__name__)
settings = get_settings()

# Initialize Redis client using dynamic REDIS_URL
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

def _create_temp_cookies_file(cookies_text: str | None) -> str | None:
    """Write cookies to a temporary file if provided."""
    if cookies_text and cookies_text.strip():
        fd, path = tempfile.mkstemp(suffix=".txt", text=True)
        with os.fdopen(fd, "w") as f:
            f.write(cookies_text)
        return path
    return None

@celery_app.task(bind=True)
def process_video_url_task(
    self,
    url: str,
    quality: str = "720",
    comments_enabled: bool = True,
    comments_type: str = "top",
    comments_limit: int = 20,
    comments_replies: bool = True,
    replies_limit: int = 5,
    auto_update: bool = False,
    cookies_text: str | None = None,
    playlist_id: int | None = None,
    compress_video: bool = False,
    download_subtitles: bool = False
) -> str:
    """
    Entry point for archiving. Identifies if the URL is a playlist or single video.
    Dispatches separate download tasks accordingly.
    """
    task_id = self.request.id
    
    def update_status(status: str, title: str = "Resolving URL..."):
        data = {
            "task_id": task_id,
            "url": url,
            "title": title,
            "status": status,
            "progress": "0%"
        }
        redis_client.setex(f"video_dl:{task_id}", 86400, json.dumps(data))
        
    update_status("Fetching info...")
    
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
    
    cookie_path = _create_temp_cookies_file(cookies_text)
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error(f"Error fetching info for URL {url}: {e}")
        update_status(f"Error: {str(e)[:50]}...", "Error")
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
                playlist = VideoPlaylist(name=playlist_title, description=playlist_description)
                session.add(playlist)
                session.commit()
                session.refresh(playlist)
                playlist_id = playlist.id

        entries = list(info_dict['entries'])
        logger.info(f"Playlist detected: {playlist_title} with {len(entries)} videos.")

        for i, entry in enumerate(entries):
            video_id = entry.get('id')
            if not video_id:
                continue
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            
            # Spawn task for each video in the playlist
            task = download_video_task.delay(
                url=video_url,
                quality=quality,
                comments_enabled=comments_enabled,
                comments_type=comments_type,
                comments_limit=comments_limit,
                comments_replies=comments_replies,
                replies_limit=replies_limit,
                auto_update=auto_update,
                cookies_text=cookies_text,
                playlist_id=playlist_id,
                compress_video=compress_video,
                download_subtitles=download_subtitles
            )
            
            data = {
                "task_id": task.id,
                "url": video_url,
                "title": entry.get('title', f"Video {video_id}"),
                "status": "Queued",
                "progress": "0%"
            }
            redis_client.setex(f"video_dl:{task.id}", 86400, json.dumps(data))

        redis_client.delete(f"video_dl:{task_id}")
        return f"Dispatched {len(entries)} videos for playlist '{playlist_title}'"
    else:
        # Single video
        video_id = info_dict.get('id')
        task = download_video_task.delay(
            url=url,
            quality=quality,
            comments_enabled=comments_enabled,
            comments_type=comments_type,
            comments_limit=comments_limit,
            comments_replies=comments_replies,
            replies_limit=replies_limit,
            auto_update=auto_update,
            cookies_text=cookies_text,
            playlist_id=playlist_id,
            compress_video=compress_video,
            download_subtitles=download_subtitles
        )
        # Delete the resolver task tracker immediately since the child downloader task has registered its own tracker
        redis_client.delete(f"video_dl:{task_id}")
        return f"Dispatched single video download task: {task.id}"


@celery_app.task(bind=True)
def download_video_task(
    self,
    url: str,
    quality: str = "720",
    comments_enabled: bool = True,
    comments_type: str = "top",
    comments_limit: int = 20,
    comments_replies: bool = True,
    replies_limit: int = 5,
    auto_update: bool = False,
    cookies_text: str | None = None,
    playlist_id: int | None = None,
    compress_video: bool = False,
    download_subtitles: bool = False
) -> str:
    """Downloads a single video, caches metadata + comments, and saves to database."""
    task_id = self.request.id
    
    def update_redis(status: str, progress: str = "0%", title: str = "Downloading..."):
        data = {
            "task_id": task_id,
            "url": url,
            "title": title,
            "status": status,
            "progress": progress
        }
        redis_client.setex(f"video_dl:{task_id}", 86400, json.dumps(data))

    # Helper hook to monitor yt-dlp progress
    def ydl_progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                pct = int((downloaded / total) * 100)
                update_redis("Downloading video stream", f"{pct}%")
        elif d['status'] == 'finished':
            update_redis("Processing video format", "99%")

    update_redis("Extracting full metadata...", "5%")

    temp_dir = tempfile.mkdtemp()
    cookie_path = _create_temp_cookies_file(cookies_text)

    # Configure yt-dlp options to download capped video
    ydl_opts = {
        'format': f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]/best[height<={quality}]',
        'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'progress_hooks': [ydl_progress_hook],
        'getcomments': comments_enabled,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'client': ['android', 'web'],
                'comment_sort': ['top' if comments_type == 'top' else 'new']
            }
        },
        'js_runtimes': {'node': {}},
        'remote_components': {'ejs:github': {}},
        'ignoreerrors': True
    }
    
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    if download_subtitles:
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = False  # Manual subtitles only (human-added, not auto-generated)
        ydl_opts['subtitleslangs'] = ['all']
        ydl_opts['subtitlesformat'] = 'vtt'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            
        if not info_dict:
            raise Exception("Failed to download media: yt-dlp extraction returned no data.")
            
        video_id = info_dict.get('id')
        title = info_dict.get('title', 'Untitled Video')
        update_redis("Archiving metadata & files", "90%", title=title)

        # Locate the downloaded file
        downloaded_file = None
        for filename in os.listdir(temp_dir):
            if filename.startswith(video_id) and filename.endswith('.mp4'):
                downloaded_file = os.path.join(temp_dir, filename)
                break
        
        if not downloaded_file:
            # Try to find any downloaded file and transcode/rename
            for filename in os.listdir(temp_dir):
                if filename.startswith(video_id):
                    downloaded_file = os.path.join(temp_dir, filename)
                    break

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise FileNotFoundError("Could not locate downloaded video file.")

        # Compress video via FFmpeg if requested
        if compress_video:
            update_redis("Compressing video (FFmpeg)", "95%", title=title)
            compressed_file = os.path.join(temp_dir, f"compressed_{video_id}.mp4")
            try:
                import subprocess
                cmd = [
                    "ffmpeg", "-y", "-i", downloaded_file,
                    "-vcodec", "libx264", "-crf", "28", "-preset", "fast",
                    "-acodec", "aac", "-b:a", "128k",
                    compressed_file
                ]
                logger.info(f"Running compression command: {' '.join(cmd)}")
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
                if os.path.exists(compressed_file) and os.path.getsize(compressed_file) > 0:
                    os.remove(downloaded_file)
                    downloaded_file = compressed_file
                    logger.info("FFmpeg compression completed successfully.")
                else:
                    logger.warning("FFmpeg compression output was missing or empty. Falling back to original file.")
            except Exception as compress_err:
                logger.error(f"FFmpeg compression failed, falling back to original video file: {compress_err}")

        # Save video to storage abstraction layer
        storage = get_storage()
        with open(downloaded_file, "rb") as f:
            video_bytes = f.read()
        
        video_storage_path = f"video_archiver/videos/{video_id}.mp4"
        storage.save_file(video_bytes, video_storage_path)
        os.remove(downloaded_file)

        # Download thumbnail
        thumbnail_storage_path = None
        thumbnail_url = info_dict.get('thumbnail')
        if thumbnail_url:
            try:
                resp = requests.get(thumbnail_url, timeout=10)
                if resp.status_code == 200:
                    ext = "jpg"
                    if "webp" in resp.headers.get("Content-Type", ""):
                        ext = "webp"
                    thumbnail_storage_path = f"video_archiver/thumbnails/{video_id}.{ext}"
                    storage.save_file(resp.content, thumbnail_storage_path)
            except Exception as thumbnail_err:
                logger.warning(f"Failed to download thumbnail: {thumbnail_err}")

        # Collect and save subtitles if downloaded
        subtitles_map = {}
        if download_subtitles:
            update_redis("Extracting subtitles", "97%", title=title)
            try:
                for filename in os.listdir(temp_dir):
                    if filename.startswith(video_id) and filename.endswith('.vtt'):
                        parts = filename.split('.')
                        if len(parts) >= 3:
                            lang = parts[-2]
                            subtitle_file = os.path.join(temp_dir, filename)
                            with open(subtitle_file, "rb") as sub_f:
                                subtitle_bytes = sub_f.read()
                            
                            sub_storage_path = f"video_archiver/subtitles/{video_id}.{lang}.vtt"
                            storage.save_file(subtitle_bytes, sub_storage_path)
                            subtitles_map[lang] = sub_storage_path
                            logger.info(f"Saved subtitle for {lang} to {sub_storage_path}")
            except Exception as sub_err:
                logger.error(f"Failed to process subtitles: {sub_err}")

        # Extract comments
        comments = []
        if comments_enabled and 'comments' in info_dict:
            raw_comments = info_dict['comments']
            # Separate root comments and replies
            roots = [c for c in raw_comments if c.get('parent', 'root') == 'root']
            replies_map = {}
            for c in raw_comments:
                p = c.get('parent')
                if p and p != 'root':
                    replies_map.setdefault(p, []).append(c)

            for c in roots[:comments_limit]:
                c_id = c.get('id')
                comment_item = {
                    'author': c.get('author', 'Unknown'),
                    'text': c.get('text', ''),
                    'likes': c.get('like_count', 0),
                    'time': c.get('time_text', ''),
                    'replies': []
                }
                if comments_replies and c_id in replies_map:
                    for r in replies_map[c_id][:replies_limit]:
                        comment_item['replies'].append({
                            'author': r.get('author', 'Unknown'),
                            'text': r.get('text', ''),
                            'likes': r.get('like_count', 0),
                            'time': r.get('time_text', '')
                        })
                comments.append(comment_item)

        # Save / Update Database
        with SyncSessionLocal() as session:
            # Check if video exists
            video = session.get(ArchivedVideo, video_id)
            if not video:
                video = ArchivedVideo(id=video_id)
                session.add(video)
            
            video.title = title
            video.description = info_dict.get('description')
            video.channel_name = info_dict.get('uploader', 'Unknown Channel')
            video.channel_id = info_dict.get('uploader_id', 'Unknown')
            video.duration = int(info_dict.get('duration') or 0)
            video.resolution = f"{quality}p"
            video.file_path = video_storage_path
            video.thumbnail_path = thumbnail_storage_path
            video.status = "completed"
            video.comments = comments
            video.subtitles = subtitles_map
            video.auto_update = auto_update
            video.is_deleted_on_youtube = False
            video.like_count = info_dict.get('like_count')
            video.view_count = info_dict.get('view_count')
            video.tags = info_dict.get('tags')
            
            publish_date_str = info_dict.get('upload_date')
            if publish_date_str:
                try:
                    video.original_publish_date = datetime.datetime.strptime(publish_date_str, "%Y%m%d")
                except ValueError:
                    pass

            if playlist_id:
                playlist = session.get(VideoPlaylist, playlist_id)
                if playlist and playlist not in video.playlists:
                    video.playlists.append(playlist)

            session.commit()

        update_redis("Completed", "100%", title=title)
        redis_client.delete(f"video_dl:{task_id}")
        return f"Successfully archived video {video_id}"

    except Exception as e:
        logger.error(f"Failed to process video {url}: {e}")
        update_redis(f"Failed: {str(e)[:50]}...", "Error")
        return f"Error downloading video: {e}"
    finally:
        # Clean up temporary directory
        if cookie_path and os.path.exists(cookie_path):
            os.remove(cookie_path)
        try:
            for root, dirs, files in os.walk(temp_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(temp_dir)
        except Exception:
            pass


@celery_app.task
def sync_video_metadata_task(video_id: str) -> str:
    """Checks if a video still exists on YouTube and refreshes description/comments."""
    with SyncSessionLocal() as session:
        video = session.get(ArchivedVideo, video_id)
        if not video:
            return "Video not found in local database."
        
        url = f"https://www.youtube.com/watch?v={video_id}"
        auto_update = video.auto_update

    # Query info only
    ydl_opts = {
        'quiet': True,
        'getcomments': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'client': ['android', 'web']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
        
        # Update database with fresh metadata
        with SyncSessionLocal() as session:
            video = session.get(ArchivedVideo, video_id)
            if video:
                video.title = info_dict.get('title', video.title)
                video.description = info_dict.get('description', video.description)
                video.is_deleted_on_youtube = False
                video.like_count = info_dict.get('like_count', video.like_count)
                video.view_count = info_dict.get('view_count', video.view_count)
                video.tags = info_dict.get('tags', video.tags)
                
                # Update comments
                comments = []
                if 'comments' in info_dict:
                    # Keep same count
                    comments_limit = len(video.comments) if video.comments else 20
                    for c in info_dict['comments'][:comments_limit]:
                        comments.append({
                            'author': c.get('author', 'Unknown'),
                            'text': c.get('text', ''),
                            'likes': c.get('like_count', 0),
                            'time': c.get('time_text', '')
                        })
                    video.comments = comments
                session.commit()
        return f"Successfully synced video {video_id}"

    except Exception as e:
        # Check if error implies video is deleted or private
        err_msg = str(e).lower()
        if "unavailable" in err_msg or "private" in err_msg or "removed" in err_msg or "404" in err_msg:
            with SyncSessionLocal() as session:
                video = session.get(ArchivedVideo, video_id)
                if video:
                    video.is_deleted_on_youtube = True
                    session.commit()
            return f"Video {video_id} is unavailable on YouTube. Marked as deleted."
        else:
            return f"Failed to sync video {video_id}: {e}"


@celery_app.task
def sync_all_videos_task() -> str:
    """Syncs metadata for all completed local videos."""
    with SyncSessionLocal() as session:
        videos = session.scalars(select(ArchivedVideo.id).where(ArchivedVideo.status == "completed")).all()
    
    for vid_id in videos:
        sync_video_metadata_task.delay(vid_id)
        
    return f"Dispatched sync tasks for {len(videos)} videos."
