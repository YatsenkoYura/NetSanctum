import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


class XssContractTests(unittest.TestCase):
    def test_alllib_description_is_escaped_without_breaking_reader_routes(self):
        detail = read("app/modules/alllib/templates/alllib_detail.html")
        router = read("app/modules/alllib/router.py")

        self.assertNotIn("description|safe", detail)
        self.assertIn("{{ media.description }}", detail)
        self.assertIn("media.source_url.startswith('https://')", detail)
        self.assertIn('rel="noopener noreferrer"', detail)
        self.assertIn('href="/alllib/reader/{{ media.id }}', detail)
        self.assertIn("safe_title = html_lib.escape(m.title, quote=True)", router)
        self.assertIn("sanitize_chapter_html(chapter.content_html)", router)
        self.assertIn('status_text = html_lib.escape(str(t.get("status") or ""))', router)

    def test_music_html_responses_escape_db_and_task_values(self):
        router = read("app/modules/music/router.py")

        self.assertIn("from markupsafe import escape", router)
        self.assertIn("escaped_title = escape(song.title)", router)
        self.assertIn('title = escape(d.get("title") or "")', router)
        self.assertIn('status = escape(d.get("status") or "")', router)
        self.assertIn("{escape(e.detail)}", router)
        self.assertNotIn('{d.get("title")}', router)
        self.assertNotIn("{song.title}</div>", router)

    def test_vault_dynamic_html_uses_context_aware_escaping(self):
        template = read("app/modules/vault/templates/vault_dashboard.html")

        self.assertIn("function escapeHtml(value)", template)
        self.assertIn("function safeExternalUrl(value)", template)
        self.assertIn("function safeImageUrl(value)", template)
        self.assertIn("${escapeAttr(h)}", template)
        self.assertIn("${escapeHtml(node.text || '')}", template)
        self.assertIn("${escapeHtml(displayText)}", template)
        self.assertNotIn("${item.og_image}", template)
        self.assertNotIn('href="${item.url}"', template)
        self.assertNotIn("${item.title} <span", template)
        self.assertNotIn("${node.text||''}", template)
        self.assertNotIn("'{{ c.name }}'", template)

    def test_video_dynamic_html_escapes_external_metadata_and_ids(self):
        template = read("app/modules/video_archiver/templates/video_dashboard.html")

        self.assertIn("function escapeHtml(value)", template)
        self.assertIn("function escapedJsArg(value)", template)
        self.assertIn("function pathSegment(value)", template)
        self.assertIn("messageElement.textContent", template)
        self.assertIn("${escapeHtml(c.text)}", template)
        self.assertIn("${escapeHtml(r.text)}", template)
        self.assertIn("${escapeHtml(t.title)}", template)
        self.assertIn("${escapeHtml(p.name)}", template)
        self.assertIn("${escapedJsArg(v.id)}", template)
        self.assertNotIn("${c.text}</p>", template)
        self.assertNotIn("${r.text}</p>", template)
        self.assertNotIn("${t.title}</span>", template)
        self.assertNotIn("${p.name}</h4>", template)
        self.assertNotIn("playVideoInLibrary('${", template)
        self.assertNotIn("cancelSingleDownload('${", template)


if __name__ == "__main__":
    unittest.main()
