"""
Asynchronous Celery tasks for unified Lib Network downloader.
"""

import json
import logging
import os

import redis
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.storage import get_storage
from app.modules.alllib.api import LibAPI, LibParser
from app.modules.alllib.models import LibChapter, LibMedia

logger = logging.getLogger(__name__)
settings = get_settings()
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def parse_float(val: str) -> float:
    try:
        val = val.replace(",", ".")
        cleaned = "".join(c for c in val if c.isdigit() or c in (".", "-"))
        return float(cleaned)
    except Exception:
        return 0.0


def parse_int(val: str) -> int:
    try:
        cleaned = "".join(c for c in val if c.isdigit())
        return int(cleaned)
    except Exception:
        return 0


def select_best_branch(chapters_data: list) -> str:
    """Find translation branch with maximum chapter count."""
    branch_counts = {}
    for ch in chapters_data:
        for branch in ch.get("branches", []):
            if isinstance(branch, dict):
                b_val = branch.get("branch_id")
                b_id = str(b_val if b_val is not None else "0")
            elif branch is not None:
                b_id = str(branch)
            else:
                b_id = "0"
            branch_counts[b_id] = branch_counts.get(b_id, 0) + 1

    if not branch_counts:
        return "0"
    return max(branch_counts, key=branch_counts.get)


def parse_range_string(range_str: str) -> set[float]:
    """Parse a range string like '1-12, 14, 16.5' into a set of floats."""
    nums = set()
    if not range_str or range_str.lower().strip() == "all":
        return nums
    for part in range_str.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-")
                for i in range(int(start), int(end) + 1):
                    nums.add(float(i))
            except Exception:
                pass
        else:
            try:
                nums.add(float(part))
            except Exception:
                pass
    return nums


def _make_storage_key(slug: str, site_id: int) -> str:
    """Return an opaque storage key for sensitive sites (HentaiLib=4, SlashLib=2).

    For 18+ content we hash the slug so the filesystem reveals nothing about
    what is stored.  For normal content the slug is used as-is (readable paths).
    """
    if site_id in (2, 4):
        import hashlib

        return hashlib.sha256(slug.encode()).hexdigest()[:24]
    return slug


def localize_novel_images(html_content: str, slug: str, site_id: int, api, storage) -> str:
    """Download external images in novel chapter content and save them to local storage,
    rewriting image sources to point to local page endpoints.
    """
    import re
    import hashlib
    import urllib.parse

    img_tag_pattern = re.compile(r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
    urls = img_tag_pattern.findall(html_content)
    if not urls:
        return html_content

    is_sensitive = site_id in (2, 4)
    storage_key = _make_storage_key(slug, site_id)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://ranobelib.me/",
    }
    if api._auth_token:
        headers["Authorization"] = f"Bearer {api._auth_token}"

    for src in set(urls):
        img_url = src
        if "/alllib/api/proxy-image?url=" in src:
            try:
                parsed = urllib.parse.urlparse(src)
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("url"):
                    img_url = query["url"][0]
            except Exception:
                pass

        if not (img_url.startswith("http://") or img_url.startswith("https://")):
            continue

        url_hash = hashlib.md5(img_url.encode("utf-8")).hexdigest()
        ext = "jpg"
        if ".png" in img_url.lower():
            ext = "png"
        elif ".gif" in img_url.lower():
            ext = "gif"
        
        filename = f"{url_hash}.{ext}"
        if is_sensitive:
            page_storage_path = f"alllib/novel/{storage_key}/images/{filename}.enc"
        else:
            page_storage_path = f"alllib/novel/{storage_key}/images/{filename}"

        if not storage.file_exists(page_storage_path):
            try:
                resp = api.session.get(img_url, headers=headers, timeout=15)
                if resp.status_code == 200 and len(resp.content) > 100:
                    if is_sensitive:
                        storage.save_file_encrypted(resp.content, page_storage_path)
                    else:
                        storage.save_file(resp.content, page_storage_path)
                else:
                    logger.warning(f"Failed to download novel image {img_url}: status {resp.status_code}")
                    continue
            except Exception as e:
                logger.warning(f"Failed to download novel image {img_url}: {e}")
                continue

        local_src = f"/alllib/api/page?path={urllib.parse.quote(page_storage_path)}"
        html_content = html_content.replace(f'src="{src}"', f'src="{local_src}"')
        html_content = html_content.replace(f"src='{src}'", f"src='{local_src}'")

    return html_content


@celery_app.task(bind=True)
def download_lib_task(
    self,
    url: str,
    auth_token: str | None = None,
    seasons: list[str] | None = None,
    episodes_range: str | None = None,
    translation_team: str | None = None,
    sync_only: bool = False,
) -> str:
    """Download chapters for any Lib Network title (Novel, Manga, Hentai, etc.)."""
    task_id = self.request.id

    def update_redis(status: str, progress: str = "0%", media_title: str = "Resolving..."):
        data = {
            "task_id": task_id,
            "url": url,
            "title": media_title,
            "status": status,
            "progress": progress,
        }
        redis_client.setex(f"alllib_dl:{task_id}", 86400, json.dumps(data))

    update_redis("Initializing Lib Network Client...", "5%")

    api = LibAPI(auth_token=auth_token)
    site_id, domain = api.get_site_info_from_url(url)

    # Auto-detect media type
    if site_id == 3:
        media_type = "novel"
    elif site_id == 6:
        media_type = "anime"
    else:
        media_type = "manga"

    slug = api.extract_slug_from_url(url)
    if not slug:
        update_redis("Failed: Invalid URL format", "Error")
        return f"Error: Failed to extract slug from URL: {url}"

    try:
        update_redis("Fetching media metadata...", "10%")
        info = api.get_novel_info(slug, site_id, domain)
        if not info:
            raise Exception("Failed to fetch media metadata from API")

        media_title = info.get("rus_name") or info.get("name") or slug
        rus_name = info.get("rus_name")
        eng_name = info.get("eng_name")
        desc = info.get("summary") or info.get("description") or ""

        # For sensitive sites, use a neutral label in task status / logs
        task_display_title = (
            media_title if site_id not in (2, 4) else f"[Content #{_make_storage_key(slug, site_id)[:8]}]"
        )

        # Handle Tiptap document description
        if isinstance(desc, dict):
            if desc.get("type") == "doc":
                try:
                    desc = LibParser().json_to_html(desc.get("content", []), [])
                except Exception as parse_err:
                    logger.warning(f"Failed to parse Prosemirror description: {parse_err}")
                    paragraphs = []
                    for node in desc.get("content", []):
                        if node.get("type") == "paragraph":
                            text = "".join(t.get("text", "") for t in node.get("content", []))
                            paragraphs.append(f"<p>{text}</p>")
                    desc = "".join(paragraphs)
            else:
                desc = json.dumps(desc)
        elif not isinstance(desc, str):
            desc = ""

        # Extract extra metadata for the info tab
        meta = {}
        if info.get("genres"):
            meta["genres"] = [g.get("name") for g in info["genres"] if isinstance(g, dict) and g.get("name")]
        if info.get("tags"):
            meta["tags"] = [t.get("name") for t in info["tags"] if isinstance(t, dict) and t.get("name")]
        if info.get("authors"):
            meta["authors"] = [
                a.get("name") for a in info["authors"] if isinstance(a, dict) and a.get("name")
            ]
        if info.get("artists"):
            meta["artists"] = [
                a.get("name") for a in info["artists"] if isinstance(a, dict) and a.get("name")
            ]
        if info.get("teams"):
            meta["teams"] = [t.get("name") for t in info["teams"] if isinstance(t, dict) and t.get("name")]
        if info.get("format"):
            if isinstance(info["format"], dict):
                meta["format"] = info["format"].get("name")
            else:
                meta["format"] = str(info["format"])
        if info.get("status"):
            if isinstance(info["status"], dict):
                meta["status"] = info["status"].get("name")
            else:
                meta["status"] = str(info["status"])

        rating_data = info.get("rating") or info.get("rate")
        if rating_data:
            if isinstance(rating_data, dict):
                meta["rating"] = rating_data.get("average") or rating_data.get("score")
            else:
                meta["rating"] = rating_data

        update_redis("Fetching chapter list...", "15%", media_title=task_display_title)
        chapters_data = api.get_novel_chapters(slug, site_id, domain)
        if not chapters_data:
            # Check if the chapters endpoint returned an auth requirement
            raise Exception(
                "No chapters found. This title may require account authentication (18+ content on HentaiLib/SlashLib requires login)."
            )

        branch_id = select_best_branch(chapters_data)

        # Download Cover
        cover_storage_path = None
        cover_data = info.get("cover")
        cover_url = None
        if cover_data:
            if isinstance(cover_data, dict):
                cover_url = cover_data.get("default") or cover_data.get("url")
            elif isinstance(cover_data, str):
                cover_url = cover_data

        if cover_url:
            if not cover_url.startswith("http"):
                if cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                else:
                    cover_url = f"https://{domain}" + cover_url

            try:
                cover_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": f"https://{domain}/",
                    "Origin": f"https://{domain}",
                }
                if api._auth_token:
                    cover_headers["Authorization"] = f"Bearer {api._auth_token}"

                resp = api.session.get(cover_url, headers=cover_headers, timeout=10)
                if resp.status_code == 200 and len(resp.content) > 200:
                    storage = get_storage()
                    ext = cover_url.split("?")[0].split(".")[-1] or "jpg"
                    if ext.lower() not in ("jpg", "jpeg", "png", "webp"):
                        ext = "jpg"
                    is_sensitive = site_id in (2, 4)
                    storage_key = _make_storage_key(slug, site_id)
                    if is_sensitive:
                        cover_storage_path = f"alllib/covers/{storage_key}.{ext}.enc"
                        storage.save_file_encrypted(resp.content, cover_storage_path)
                    else:
                        cover_storage_path = f"alllib/covers/{storage_key}.{ext}"
                        storage.save_file(resp.content, cover_storage_path)
            except Exception as cover_err:
                logger.warning(f"Failed to fetch cover: {cover_err}")

        # Filter & Sort
        existing_keys = set()
        if sync_only:
            with SyncSessionLocal() as session:
                stmt = select(LibMedia).where(LibMedia.slug == slug)
                db_media = session.execute(stmt).scalar_one_or_none()
                if db_media:
                    existing_keys = {(ch.volume, ch.number) for ch in db_media.chapters}

        filtered_chapters = []
        for ch in chapters_data:
            for branch in ch.get("branches", []):
                branch_id_str = "0"
                if isinstance(branch, dict):
                    branch_id_val = branch.get("branch_id")
                    branch_id_str = str(branch_id_val if branch_id_val is not None else "0")
                elif branch is not None:
                    branch_id_str = str(branch)

                if branch_id_str == branch_id:
                    ch_vol = str(ch.get("volume", "0"))
                    ch_num = str(ch.get("number", "0"))
                    if not sync_only or (ch_vol, ch_num) in existing_keys:
                        filtered_chapters.append((ch, branch_id_str))
                    break

        filtered_chapters.sort(
            key=lambda x: (parse_int(x[0].get("volume", "0")), parse_float(x[0].get("number", "0")))
        )

        if media_type == "anime":
            if seasons:
                filtered_chapters = [x for x in filtered_chapters if str(x[0].get("volume", "1")) in seasons]
            if episodes_range and episodes_range.lower().strip() != "all":
                allowed_numbers = parse_range_string(episodes_range)
                if allowed_numbers:
                    filtered_chapters = [
                        x
                        for x in filtered_chapters
                        if parse_float(x[0].get("number", "0")) in allowed_numbers
                    ]

        total_chapters = len(filtered_chapters)
        if total_chapters == 0 and not sync_only:
            raise Exception("No chapters match the selected branch")

        update_redis("Saving library configuration...", "20%", media_title=task_display_title)

        # Sync Database
        with SyncSessionLocal() as session:
            stmt = select(LibMedia).where(LibMedia.slug == slug)
            db_media = session.execute(stmt).scalar_one_or_none()

            if not db_media:
                db_media = LibMedia(
                    title=media_title,
                    rus_name=rus_name,
                    eng_name=eng_name,
                    slug=slug,
                    description=desc,
                    cover_path=cover_storage_path,
                    source_url=url,
                    media_type=media_type,
                    site_id=site_id,
                    metadata_json=meta,
                )
                session.add(db_media)
                session.flush()
            else:
                db_media.title = media_title
                db_media.rus_name = rus_name
                db_media.eng_name = eng_name
                db_media.description = desc
                db_media.media_type = media_type
                db_media.site_id = site_id
                db_media.metadata_json = meta
                if cover_storage_path:
                    db_media.cover_path = cover_storage_path

            existing_chapters = {(ch.volume, ch.number): ch for ch in db_media.chapters}
            media_db_id = db_media.id
            storage = get_storage()

            if media_type == "novel":
                parser = LibParser()
                for idx, (ch_info, b_id) in enumerate(filtered_chapters):
                    # Check for cancellation
                    if not redis_client.exists(f"alllib_dl:{task_id}"):
                        logger.info(f"Task {task_id} cancelled. Stopping.")
                        return "Cancelled"

                    vol = str(ch_info.get("volume", "0"))
                    num = str(ch_info.get("number", "0"))
                    ch_name = ch_info.get("name")

                    progress_pct = int(20 + (idx / total_chapters) * 75)
                    update_redis(
                        f"Downloading Chapter {num} (Vol {vol})",
                        f"{progress_pct}%",
                        media_title=task_display_title,
                    )

                    if (vol, num) in existing_chapters and existing_chapters[(vol, num)].content_html:
                        continue

                    try:
                        chapter_data = api.get_chapter_content(slug, vol, num, b_id, site_id, domain)
                        content = chapter_data.get("content")
                        html = ""
                        if content:
                            if (
                                isinstance(content, dict)
                                and content.get("type") == "doc"
                                and content.get("content")
                            ):
                                html = parser.json_to_html(
                                    content.get("content", []), chapter_data.get("attachments", [])
                                )
                            elif isinstance(content, str):
                                html = content
                            
                            # Localize external images to local storage
                            if html:
                                html = localize_novel_images(html, slug, site_id, api, storage)

                        stmt_ch = select(LibChapter).where(
                            (LibChapter.media_id == media_db_id)
                            & (LibChapter.volume == vol)
                            & (LibChapter.number == num)
                        )
                        db_chapter = session.execute(stmt_ch).scalar_one_or_none()

                        if db_chapter:
                            db_chapter.content_html = html
                            db_chapter.name = ch_name
                        else:
                            new_chapter = LibChapter(
                                media_id=media_db_id,
                                volume=vol,
                                number=num,
                                volume_int=parse_int(vol),
                                number_float=parse_float(num),
                                name=ch_name,
                                content_html=html,
                            )
                            session.add(new_chapter)
                        session.commit()
                    except Exception as ch_err:
                        logger.error(f"Failed to download chapter {num} (Vol {vol}): {ch_err}")
                        continue

            elif media_type == "manga":
                image_servers = api.get_image_servers(site_id=site_id, domain=domain)
                for idx, (ch_info, b_id) in enumerate(filtered_chapters):
                    # Check for cancellation
                    if not redis_client.exists(f"alllib_dl:{task_id}"):
                        logger.info(f"Task {task_id} cancelled. Stopping.")
                        return "Cancelled"

                    vol = str(ch_info.get("volume", "0"))
                    num = str(ch_info.get("number", "0"))
                    ch_name = ch_info.get("name")

                    progress_pct = int(20 + (idx / total_chapters) * 75)
                    update_redis(
                        f"Downloading Chapter {num} (Vol {vol})",
                        f"{progress_pct}%",
                        media_title=task_display_title,
                    )

                    if (vol, num) in existing_chapters and existing_chapters[(vol, num)].pages_list:
                        continue

                    try:
                        chapter_data = api.get_chapter_content(slug, vol, num, b_id, site_id, domain)
                        pages = chapter_data.get("pages", [])
                        if not pages:
                            logger.warning(f"No pages found for Vol {vol} Chapter {num}")
                            continue

                        downloaded_pages_paths = []
                        for page_idx, page in enumerate(pages):
                            # Check for cancellation
                            if not redis_client.exists(f"alllib_dl:{task_id}"):
                                logger.info(f"Task {task_id} cancelled. Stopping.")
                                return "Cancelled"

                            page_url_path = page.get("url")
                            if not page_url_path:
                                continue

                            rel_path = page_url_path.lstrip("/")
                            page_filename = "".join(
                                c
                                for c in page.get("image", f"page_{page_idx}.jpg")
                                if c.isalnum() or c in (".", "_", "-")
                            )

                            success = False
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": f"https://{domain}/",
                                "Origin": f"https://{domain}",
                            }
                            if api._auth_token:
                                headers["Authorization"] = f"Bearer {api._auth_token}"

                            for server in image_servers:
                                image_url = f"{server}/{rel_path}"
                                try:
                                    resp = api.session.get(image_url, headers=headers, timeout=15)
                                    if resp.status_code == 200 and len(resp.content) > 200:
                                        is_sensitive = site_id in (2, 4)
                                        storage_key = _make_storage_key(slug, site_id)
                                        if is_sensitive:
                                            page_storage_path = (
                                                f"alllib/manga/{storage_key}/{vol}_{num}/{page_filename}.enc"
                                            )
                                            storage.save_file_encrypted(resp.content, page_storage_path)
                                        else:
                                            page_storage_path = (
                                                f"alllib/manga/{storage_key}/{vol}_{num}/{page_filename}"
                                            )
                                            storage.save_file(resp.content, page_storage_path)
                                        downloaded_pages_paths.append(page_storage_path)
                                        success = True
                                        break
                                    else:
                                        logger.debug(
                                            f"CDN {server} returned status {resp.status_code} or small/empty content ({len(resp.content)} bytes)"
                                        )
                                except Exception as e:
                                    logger.debug(f"CDN {server} failed: {e}")
                                    continue

                            if not success:
                                raise Exception(f"Failed to download page {page_idx} from any CDN")

                        stmt_ch = select(LibChapter).where(
                            (LibChapter.media_id == media_db_id)
                            & (LibChapter.volume == vol)
                            & (LibChapter.number == num)
                        )
                        db_chapter = session.execute(stmt_ch).scalar_one_or_none()

                        if db_chapter:
                            db_chapter.pages_list = downloaded_pages_paths
                            db_chapter.name = ch_name
                        else:
                            new_chapter = LibChapter(
                                media_id=media_db_id,
                                volume=vol,
                                number=num,
                                volume_int=parse_int(vol),
                                number_float=parse_float(num),
                                name=ch_name,
                                pages_list=downloaded_pages_paths,
                            )
                            session.add(new_chapter)
                        session.commit()
                    except Exception as ch_err:
                        logger.error(f"Failed to download chapter {num} (Vol {vol}): {ch_err}")
                        continue

            elif media_type == "anime":
                import subprocess
                import tempfile

                from anicli_api.player.kodik import Kodik

                for idx, (ch_info, _) in enumerate(filtered_chapters):
                    # Check for cancellation
                    if not redis_client.exists(f"alllib_dl:{task_id}"):
                        logger.info(f"Task {task_id} cancelled. Stopping.")
                        return "Cancelled"

                    vol = str(ch_info.get("volume", "1"))
                    num = str(ch_info.get("number", "1"))
                    ch_name = ch_info.get("name")
                    episode_id = ch_info.get("id")

                    progress_pct = int(20 + (idx / total_chapters) * 75)
                    update_redis(
                        f"Downloading Episode {num} (Vol {vol})",
                        f"{progress_pct}%",
                        media_title=task_display_title,
                    )

                    # Check if already downloaded
                    if (vol, num) in existing_chapters and existing_chapters[(vol, num)].video_path:
                        if storage.file_exists(existing_chapters[(vol, num)].video_path):
                            continue

                    try:
                        players = api.get_episode_players(episode_id, site_id, domain)
                        if not players:
                            logger.warning(f"No players found for Vol {vol} Episode {num}")
                            continue

                        # Select the player based on translation_team
                        selected_player = None
                        if translation_team and translation_team.lower() != "any":
                            for pl in players:
                                team_name = pl.get("team", {}).get("name") or ""
                                if translation_team.lower() in team_name.lower():
                                    selected_player = pl
                                    break

                        if not selected_player:
                            for pl in players:
                                if pl.get("player", "").lower() in ("animelib", "kodik"):
                                    selected_player = pl
                                    break
                            if not selected_player and players:
                                selected_player = players[0]

                        if not selected_player:
                            logger.warning(f"No valid player for Vol {vol} Episode {num}")
                            continue

                        player_name = selected_player.get("player", "").lower()

                        with tempfile.TemporaryDirectory() as temp_dir:
                            temp_output_path = os.path.join(temp_dir, f"temp_ep_{num}.mp4")
                            success = False

                            if player_name == "animelib":
                                video_qualities = selected_player.get("video", {}).get("quality", [])
                                if video_qualities:
                                    video_qualities.sort(
                                        key=lambda x: (
                                            int(x.get("quality", 0)) if str(x.get("quality")).isdigit() else 0
                                        ),
                                        reverse=True,
                                    )
                                    best_qual = video_qualities[0]
                                    href = best_qual.get("href")
                                    if href:
                                        direct_url = "https://video1.cdnlibs.org/.%D0%B0s/" + href.lstrip("/")
                                        headers = {
                                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                            "Referer": "https://v3.animelib.org",
                                        }
                                        resp = api.session.get(
                                            direct_url, headers=headers, stream=True, timeout=30
                                        )
                                        if resp.status_code == 200:
                                            with open(temp_output_path, "wb") as f:
                                                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                                    if not redis_client.exists(f"alllib_dl:{task_id}"):
                                                        logger.info(f"Task {task_id} cancelled. Stopping.")
                                                        return "Cancelled"
                                                    if chunk:
                                                        f.write(chunk)
                                            success = True

                            elif player_name == "kodik":
                                src_url = selected_player.get("src") or selected_player.get("url")
                                if src_url:
                                    if src_url.startswith("//"):
                                        src_url = "https:" + src_url
                                    hls_videos = Kodik().parse(src_url)
                                    if hls_videos:
                                        hls_videos.sort(key=lambda x: x.quality, reverse=True)
                                        m3u8_url = hls_videos[0].url
                                        cmd = [
                                            "ffmpeg",
                                            "-y",
                                            "-i",
                                            m3u8_url,
                                            "-c",
                                            "copy",
                                            "-bsf:a",
                                            "aac_adtstoasc",
                                            temp_output_path,
                                        ]
                                        res = subprocess.run(cmd, capture_output=True, text=True)
                                        if (
                                            res.returncode == 0
                                            and os.path.exists(temp_output_path)
                                            and os.path.getsize(temp_output_path) > 0
                                        ):
                                            success = True

                            if (
                                not success
                                or not os.path.exists(temp_output_path)
                                or os.path.getsize(temp_output_path) == 0
                            ):
                                raise Exception("Failed to retrieve episode video content.")

                            is_sensitive = site_id in (2, 4)
                            storage_key = _make_storage_key(slug, site_id)
                            video_storage_path = f"alllib/anime/{storage_key}/season_{vol}_episode_{num}.mp4"
                            if is_sensitive:
                                video_storage_path += ".enc"
                                with open(temp_output_path, "rb") as f:
                                    storage.save_file_encrypted(f.read(), video_storage_path)
                            else:
                                with open(temp_output_path, "rb") as f:
                                    storage.save_file(f.read(), video_storage_path)

                            stmt_ch = select(LibChapter).where(
                                (LibChapter.media_id == media_db_id)
                                & (LibChapter.volume == vol)
                                & (LibChapter.number == num)
                            )
                            db_chapter = session.execute(stmt_ch).scalar_one_or_none()

                            if db_chapter:
                                db_chapter.video_path = video_storage_path
                                db_chapter.name = ch_name
                            else:
                                new_chapter = LibChapter(
                                    media_id=media_db_id,
                                    volume=vol,
                                    number=num,
                                    volume_int=parse_int(vol),
                                    number_float=parse_float(num),
                                    name=ch_name,
                                    video_path=video_storage_path,
                                )
                                session.add(new_chapter)
                            session.commit()
                    except Exception as ch_err:
                        logger.error(f"Failed to download episode {num} (Vol {vol}): {ch_err}")
                        continue

        update_redis("Completed", "100%", media_title=task_display_title)
        redis_client.delete(f"alllib_dl:{task_id}")
        return f"Successfully completed downloading {media_title}."

    except Exception as e:
        logger.error(f"Failed task for URL {url}: {e}")
        update_redis(f"Failed: {str(e)[:50]}...", "Error")
        return f"Error downloading title: {e}"
