import html
import io
import logging
import re
import urllib.parse
import uuid
import zipfile

import requests

from app.core.storage import get_storage
from app.modules.ranobelib.models import RanobeChapter, RanobeNovel

logger = logging.getLogger(__name__)


class EPUBBuilder:
    @staticmethod
    def build_epub(novel: RanobeNovel, chapters: list[RanobeChapter]) -> bytes:
        """
        Builds a valid EPUB 2.0 e-book archive in memory and returns the raw bytes.
        Downloads all external/proxied images and bundles them within the EPUB.
        """
        epub_io = io.BytesIO()
        book_uuid = str(uuid.uuid4())

        # We use ZIP_DEFLATED for everything except mimetype, which must be uncompressed (ZIP_STORED)
        with zipfile.ZipFile(epub_io, "w", zipfile.ZIP_DEFLATED) as epub:
            # 1. mimetype (MUST be first file, uncompressed)
            epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

            # 2. META-INF/container.xml
            container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>"""
            epub.writestr("META-INF/container.xml", container_xml)

            # 3. Read cover if exists
            has_cover = False
            cover_bytes = None
            cover_media_type = "image/jpeg"
            if novel.cover_path:
                storage = get_storage()
                try:
                    if storage.file_exists(novel.cover_path):
                        with storage.get_file_stream(novel.cover_path) as f:
                            cover_bytes = f.read()
                        has_cover = True
                        if novel.cover_path.lower().endswith(".png"):
                            cover_media_type = "image/png"
                        elif novel.cover_path.lower().endswith(".gif"):
                            cover_media_type = "image/gif"
                except Exception as e:
                    logger.warning(f"Failed to read cover for novel {novel.id}: {e}")

            if has_cover and cover_bytes:
                epub.writestr("OEBPS/cover.jpg", cover_bytes)

            # 4. Generate title/intro page (title.xhtml)
            escaped_title = html.escape(novel.title or "")
            escaped_eng = html.escape(novel.eng_name or "")
            escaped_rus = html.escape(novel.rus_name or "")
            escaped_desc = novel.description or ""  # Keep HTML safe or cleaned

            title_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{escaped_title}</title>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <style type="text/css">
    body {{ font-family: sans-serif; padding: 5%; text-align: center; }}
    h1 {{ font-family: serif; font-size: 2.5em; margin-bottom: 0.2em; }}
    h2 {{ font-size: 1.2em; color: #666; margin-bottom: 2em; }}
    .cover-img {{ max-width: 80%; max-height: 400px; border: 1px solid #ccc; margin-bottom: 2em; }}
    .description {{ text-align: left; margin-top: 2em; font-size: 0.95em; line-height: 1.6; border-top: 1px solid #eee; padding-top: 1em; }}
  </style>
</head>
<body>
  <h1>{escaped_title}</h1>
  {f"<h2>{escaped_eng} / {escaped_rus}</h2>" if escaped_eng or escaped_rus else ""}
  {'<img class="cover-img" src="cover.jpg" alt="Cover"/>' if has_cover else ""}
  <div class="description">
    <h3>Description / Описание:</h3>
    {escaped_desc}
  </div>
</body>
</html>"""
            epub.writestr("OEBPS/title.xhtml", title_xhtml)

            # 5. Generate chapters
            manifest_items = []
            spine_items = []
            nav_points = []

            # Add title to manifest/spine
            manifest_items.append('<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="title"/>')

            if has_cover:
                manifest_items.append(
                    f'<item id="cover-image" href="cover.jpg" media-type="{cover_media_type}"/>'
                )

            # We pre-compile regex to extract img tags and their src attributes
            img_tag_pattern = re.compile(r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)

            image_counter = 0
            url_to_epub_path = {}  # mapping: original_img_url -> (epub_href, media_type)

            for idx, ch in enumerate(chapters, start=1):
                ch_id = f"chapter_{idx}"
                ch_href = f"chapter_{idx}.xhtml"

                ch_title = f"Volume {ch.volume} Chapter {ch.number}"
                if ch.name:
                    ch_title += f" - {ch.name}"
                escaped_ch_title = html.escape(ch_title)

                ch_body = ch.content_html or ""

                # Internal substitution helper to download and map images
                def replace_img_tags(match):
                    nonlocal image_counter
                    original_tag = match.group(0)
                    src = match.group(1)

                    img_url = src
                    # Resolve original URL if it was rewritten to flow through local image proxy
                    if "/ranobelib/api/proxy-image?url=" in src:
                        try:
                            parsed = urllib.parse.urlparse(src)
                            query = urllib.parse.parse_qs(parsed.query)
                            if query.get("url"):
                                img_url = query["url"][0]
                        except Exception as parse_err:
                            logger.warning(f"Failed to parse proxied image url: {src}, error: {parse_err}")

                    # Check if it's a valid remote URL
                    if not (img_url.startswith("http://") or img_url.startswith("https://")):
                        return original_tag

                    if img_url in url_to_epub_path:
                        epub_href, media_type = url_to_epub_path[img_url]
                    else:
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Referer": "https://ranobelib.me/",
                        }
                        try:
                            res = requests.get(img_url, headers=headers, timeout=15)
                            if res.status_code == 200:
                                image_counter += 1
                                content_type = res.headers.get("content-type", "").lower()
                                if "png" in content_type:
                                    ext = "png"
                                    media_type = "image/png"
                                elif "gif" in content_type:
                                    ext = "gif"
                                    media_type = "image/gif"
                                else:
                                    ext = "jpg"
                                    media_type = "image/jpeg"

                                epub_href = f"images/img_{image_counter}.{ext}"
                                # Save image binary to EPUB zip
                                epub.writestr(f"OEBPS/{epub_href}", res.content)
                                # Cache
                                url_to_epub_path[img_url] = (epub_href, media_type)
                                # Add to manifest_items
                                manifest_items.append(
                                    f'<item id="img_{image_counter}" href="{epub_href}" media-type="{media_type}"/>'
                                )
                                logger.info(f"Successfully embedded image {img_url} to OEBPS/{epub_href}")
                            else:
                                logger.warning(
                                    f"Failed to download image {img_url}: status {res.status_code}"
                                )
                                return original_tag
                        except Exception as img_err:
                            logger.warning(f"Error downloading image {img_url}: {img_err}")
                            return original_tag

                    # Return compliant self-closing xhtml img tag referencing local EPUB path
                    return f'<img src="{epub_href}" alt="Image" />'

                # Process all image tags in chapter body
                ch_body_processed = img_tag_pattern.sub(replace_img_tags, ch_body)

                ch_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{escaped_ch_title}</title>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <style type="text/css">
    body {{ font-family: sans-serif; padding: 5%; }}
    h2 {{ text-align: center; font-family: serif; color: #111; margin-bottom: 1.5em; }}
    p {{ text-indent: 1.5em; margin: 0.5em 0; line-height: 1.5; text-align: justify; }}
    .content {{ font-size: 1.05em; }}
    img {{ display: block; max-width: 100%; height: auto; margin: 1em auto; border: 1px solid #ddd; padding: 4px; background: #fff; }}
  </style>
</head>
<body>
  <h2>{escaped_ch_title}</h2>
  <div class="content">
    {ch_body_processed}
  </div>
</body>
</html>"""

                # Write to zip
                epub.writestr(f"OEBPS/{ch_href}", ch_xhtml)

                # Append to metadata collections
                manifest_items.append(
                    f'<item id="{ch_id}" href="{ch_href}" media-type="application/xhtml+xml"/>'
                )
                spine_items.append(f'<itemref idref="{ch_id}"/>')

                # playOrder starts at 2 (since title page is 1)
                nav_points.append(f"""    <navPoint id="navPoint-{idx}" playOrder="{idx + 1}">
      <navLabel>
        <text>{escaped_ch_title}</text>
      </navLabel>
      <content src="{ch_href}"/>
    </navPoint>""")

            # 6. Generate toc.ncx
            toc_ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD NCX 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx-2005-1.dtd" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{book_uuid}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle>
    <text>{escaped_title}</text>
  </docTitle>
  <navMap>
    <navPoint id="navPoint-title" playOrder="1">
      <navLabel>
        <text>Cover &amp; Description / Обложка и описание</text>
      </navLabel>
      <content src="title.xhtml"/>
    </navPoint>
    {"\n".join(nav_points)}
  </navMap>
</ncx>"""
            epub.writestr("OEBPS/toc.ncx", toc_ncx)

            # 7. Generate content.opf
            cover_meta = '<meta name="cover" content="cover-image"/>' if has_cover else ""
            content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{escaped_title}</dc:title>
    <dc:creator>RanobeLib Downloader</dc:creator>
    <dc:identifier id="bookid">urn:uuid:{book_uuid}</dc:identifier>
    <dc:language>ru</dc:language>
    {cover_meta}
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    {"\n    ".join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {"\n    ".join(spine_items)}
  </spine>
</package>"""
            epub.writestr("OEBPS/content.opf", content_opf)

        return epub_io.getvalue()
