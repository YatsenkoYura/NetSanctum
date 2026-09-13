import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class YouTubeNavigationTemplateTests(unittest.TestCase):
    def test_watch_page_reattaches_the_persistent_video_element(self):
        watch = (ROOT / "app/modules/youtube/templates/youtube_watch.html").read_text()

        self.assertIn('id="youtube-watch-player-slot"', watch)
        self.assertIn("const retainedPlayer = miniPlayerHost.querySelector('#youtube-watch-player');", watch)
        self.assertIn("if (retainedPlayer) playerSlot.replaceChildren(player);", watch)
        self.assertIn("player.dataset.youtubeVideoId !== videoId", watch)

    def test_dashboard_uses_the_whitelisted_navigator_for_watch_pages(self):
        dashboard = (ROOT / "app/modules/youtube/templates/youtube_dashboard.html").read_text()

        self.assertIn("window.netSanctumNavigate?.(url)", dashboard)


if __name__ == "__main__":
    unittest.main()
