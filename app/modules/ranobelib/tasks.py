"""
Celery tasks for RanobeLib downloader module.
"""

import json
import logging
import urllib.parse
from datetime import datetime, timezone
from sqlalchemy import select
import redis
import re

from app.core.database import SyncSessionLocal
from app.core.scheduler import celery_app
from app.core.storage import get_storage
from app.core.config import get_settings
from app.modules.ranobelib.api import RanobeLibAPI, RanobeLibParser
from app.modules.ranobelib.models import RanobeNovel, RanobeChapter

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
                b_id = str(branch.get("branch_id", "0"))
            else:
                b_id = str(branch)
            branch_counts[b_id] = branch_counts.get(b_id, 0) + 1

    if not branch_counts:
        return "0"
    return max(branch_counts, key=branch_counts.get)


def proxy_html_images(html: str) -> str:
    """Rewrite absolute img URLs to flow through local image proxy using regex to bypass bs4 dependency."""
    if not html:
        return ""
    try:
        def replace_src(match):
            prefix = match.group(1)
            url = match.group(2)
            suffix = match.group(3)
            proxy_url = f"/ranobelib/api/proxy-image?url={urllib.parse.quote(url)}"
            return f"{prefix}{proxy_url}{suffix}"

        pattern = r'(src=["\'])(https?://[^"\']+)(["\'])'
        return re.sub(pattern, replace_src, html)
    except Exception:
        return html


@celery_app.task(bind=True)
def download_ranobe_task(self, url: str) -> str:
    """Asynchronously download novel info and all missing chapters from RanobeLib."""
    task_id = self.request.id

    def update_redis(status: str, progress: str = "0%", novel_title: str = "Resolving..."):
        data = {
            "task_id": task_id,
            "url": url,
            "title": novel_title,
            "status": status,
            "progress": progress,
        }
        redis_client.setex(f"ranobe_dl:{task_id}", 86400, json.dumps(data))

    update_redis("Initializing RanobeLib Client...", "5%")

    api = RanobeLibAPI()
    parser = RanobeLibParser()

    slug = api.extract_slug_from_url(url)
    if not slug:
        update_redis("Failed: Invalid RanobeLib URL", "Error")
        return f"Error: Failed to extract slug from URL: {url}"

    try:
        update_redis("Fetching novel metadata...", "10%")
        novel_info = api.get_novel_info(slug)
        if not novel_info:
            raise Exception("Failed to fetch novel metadata")

        novel_title = novel_info.get("rus_name") or novel_info.get("name") or slug
        novel_rus_name = novel_info.get("rus_name")
        novel_eng_name = novel_info.get("eng_name")
        novel_desc_raw = novel_info.get("summary")
        if isinstance(novel_desc_raw, dict) and novel_desc_raw.get("type") == "doc":
            novel_desc = parser.json_to_html(novel_desc_raw.get("content", []), [])
        elif isinstance(novel_desc_raw, str):
            novel_desc = novel_desc_raw
        else:
            novel_desc = ""

        update_redis("Fetching chapter list...", "15%", novel_title=novel_title)
        chapters_data = api.get_novel_chapters(slug)

        if not chapters_data:
            raise Exception("No chapters found or access denied")

        # Select best translation branch automatically
        branch_id = select_best_branch(chapters_data)

        # Download & save cover image
        cover_storage_path = None
        cover_data = novel_info.get("cover")
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
                    cover_url = "https://ranobelib.me" + cover_url

            try:
                # Fetch cover with correct headers
                resp = api.session.get(cover_url, timeout=10)
                if resp.status_code == 200:
                    storage = get_storage()
                    ext = cover_url.split("?")[0].split(".")[-1] or "jpg"
                    if ext.lower() not in ("jpg", "jpeg", "png", "webp"):
                        ext = "jpg"
                    cover_storage_path = f"ranobelib/covers/{slug}.{ext}"
                    storage.save_file(resp.content, cover_storage_path)
            except Exception as cover_err:
                logger.warning(f"Failed to fetch cover from {cover_url}: {cover_err}")

        # Filter and sort chapters
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
                    filtered_chapters.append((ch, branch_id_str))
                    break

        filtered_chapters.sort(key=lambda x: (
            parse_int(x[0].get("volume", "0")),
            parse_float(x[0].get("number", "0"))
        ))

        total_chapters = len(filtered_chapters)
        if total_chapters == 0:
            raise Exception("No chapters match the selected branch")

        update_redis("Saving novel configuration...", "20%", novel_title=novel_title)

        # Synchronize database
        with SyncSessionLocal() as session:
            stmt = select(RanobeNovel).where(RanobeNovel.slug == slug)
            db_novel = session.execute(stmt).scalar_one_or_none()

            if not db_novel:
                db_novel = RanobeNovel(
                    title=novel_title,
                    rus_name=novel_rus_name,
                    eng_name=novel_eng_name,
                    slug=slug,
                    description=novel_desc,
                    cover_path=cover_storage_path,
                    source_url=url,
                )
                session.add(db_novel)
                session.flush()
            else:
                db_novel.title = novel_title
                db_novel.rus_name = novel_rus_name
                db_novel.eng_name = novel_eng_name
                db_novel.description = novel_desc
                if cover_storage_path:
                    db_novel.cover_path = cover_storage_path

            # Map existing chapters to skip
            existing_chapters = {
                (ch.volume, ch.number): ch for ch in db_novel.chapters
            }

            novel_db_id = db_novel.id

            # Download chapters
            for idx, (ch_info, b_id) in enumerate(filtered_chapters):
                vol = str(ch_info.get("volume", "0"))
                num = str(ch_info.get("number", "0"))
                ch_name = ch_info.get("name")

                progress_pct = int(20 + (idx / total_chapters) * 75)
                update_redis(
                    f"Downloading Chapter {num} (Vol {vol})",
                    f"{progress_pct}%",
                    novel_title=novel_title,
                )

                # Skip if already downloaded
                if (vol, num) in existing_chapters:
                    continue

                try:
                    chapter_data = api.get_chapter_content(slug, vol, num, b_id)
                    content = chapter_data.get("content")
                    html = ""
                    if content:
                        if isinstance(content, dict) and content.get("type") == "doc" and content.get("content"):
                            html = parser.json_to_html(content["content"], chapter_data.get("attachments", []))
                        else:
                            html = str(content)

                    # Proxy absolute images in HTML to bypass CORS/hotlinking restrictions
                    html = proxy_html_images(html)

                    # Save chapter
                    new_chapter = RanobeChapter(
                        novel_id=novel_db_id,
                        volume=vol,
                        number=num,
                        volume_int=parse_int(vol),
                        number_float=parse_float(num),
                        name=ch_name,
                        content_html=html,
                    )
                    session.add(new_chapter)
                    # Commit frequently to ensure progress isn't lost if interrupted
                    session.commit()
                except Exception as ch_err:
                    logger.error(f"Failed to download Chapter {num} (Vol {vol}) of {slug}: {ch_err}")
                    # Keep downloading other chapters even if one fails
                    continue

        update_redis("Completed", "100%", novel_title=novel_title)
        redis_client.delete(f"ranobe_dl:{task_id}")
        return f"Successfully downloaded novel {novel_title} with {total_chapters} chapters."

    except Exception as e:
        logger.error(f"Failed to process RanobeLib download task for URL {url}: {e}")
        update_redis(f"Failed: {str(e)[:50]}...", "Error")
        return f"Error downloading novel: {e}"
