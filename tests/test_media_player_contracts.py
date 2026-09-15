import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MediaPlayerContractTests(unittest.TestCase):
    def test_core_and_shared_layouts_load_common_player_controller(self):
        base = (ROOT / "app/core/templates/base.html").read_text()
        shared = (ROOT / "app/modules/sharing/templates/shared_base.html").read_text()
        controller = (ROOT / "app/core/templates/media_player_script.html").read_text()

        self.assertIn('{% include "media_player_script.html" %}', base)
        self.assertIn('{% include "media_player_script.html" %}', shared)
        self.assertIn("(hover: hover) and (pointer: fine)", controller)
        self.assertIn("touchstart", controller)
        self.assertIn("touchend", controller)
        self.assertIn("rawSavedVolume === null ? NaN", controller)
        self.assertIn('button, a, [role="button"]', controller)
        self.assertIn("event.ctrlKey", controller)
        self.assertIn("htmx:beforeCleanupElement", controller)
        for shortcut in ("Space", "KeyK", "KeyJ", "KeyL", "KeyM", "KeyF", "KeyI", "KeyT"):
            self.assertIn(shortcut, controller)

    def test_players_declare_autoplay_and_desktop_only_actions(self):
        youtube = (ROOT / "app/modules/youtube/templates/youtube_watch.html").read_text()
        video = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()
        anime = (ROOT / "app/modules/alllib/templates/reader_anime.html").read_text()
        anime_partial = (ROOT / "app/modules/alllib/router.py").read_text()

        self.assertIn('data-player-autoplay="youtube"', youtube)
        self.assertIn('data-player-autoplay="video-archive"', video)
        self.assertIn('data-player-autoplay="alllib-anime"', anime_partial)
        self.assertIn("NetSanctumMediaPlayer.bind", youtube)
        self.assertIn("NetSanctumMediaPlayer.bind", video)
        self.assertIn("NetSanctumMediaPlayer.bind", anime)
        self.assertIn("var VIDEO_ARCHIVE_AUTOPLAY_KEY", video)
        self.assertIn("window._animeEpisodeRequest?.abort()", anime)
        for source in (youtube, video, anime_partial):
            self.assertIn("data-desktop-player-action", source)
            self.assertIn("data-player-pip", source)

    def test_mobile_controls_and_settings_are_touch_friendly(self):
        youtube = (ROOT / "app/modules/youtube/templates/youtube_watch.html").read_text()
        video = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()
        anime = (ROOT / "app/modules/alllib/templates/reader_anime.html").read_text()

        self.assertIn("translate-y-0", youtube)
        self.assertIn("sm:group-focus-within:translate-y-0", youtube)
        self.assertIn("max-height: min(60dvh, 420px)", video)
        self.assertIn("max-height: min(60dvh, 420px)", anime)

    def test_youtube_persistence_cleans_up_bindings_and_preserves_autoplay_intent(self):
        base = (ROOT / "app/core/templates/base.html").read_text()
        youtube = (ROOT / "app/modules/youtube/templates/youtube_watch.html").read_text()
        shared_base = (ROOT / "app/modules/sharing/templates/shared_base.html").read_text()

        self.assertIn("player._youtubePageBindings?.abort()", base)
        self.assertIn("player._netSanctumPlayerBindings?.abort()", base)
        self.assertIn("youtube:autoplay-next", youtube)
        self.assertIn("signal: playerSignal", youtube)
        self.assertIn("signal: transportBindings.signal", youtube)
        self.assertIn('src="/static/htmx.min.js"', shared_base)

    def test_persistent_video_shell_and_close_actions_are_global(self):
        base = (ROOT / "app/core/templates/base.html").read_text()
        video = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()

        self.assertIn('id="global-video-player"', base)
        self.assertIn('id="persistent-video-host"', base)
        self.assertIn("window.persistentVideoPlayer.close()", base)
        self.assertIn("window.musicPlayer.close()", base)
        self.assertIn("function preserveActiveVideo()", base)
        self.assertIn("body.persistent-video-active #main-content", base)
        self.assertIn("!video.paused", base)
        self.assertIn("player.dataset.persistentModule = 'video_archiver'", video)
        self.assertIn("restorePersistentArchivePlayer()", video)
        self.assertIn("video._archivePageBindings?.abort()", video)

    def test_video_cards_keep_metadata_off_the_thumbnail(self):
        video = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()
        card_start = video.index("grid.innerHTML = videos.map(video =>")
        card_end = video.index("} catch (err)", card_start)
        card = video[card_start:card_end]

        self.assertNotIn("resolutionBadge", card)
        self.assertNotIn("platformBadge", card)
        self.assertIn("${formatDuration(video.duration)}</div>", card)


if __name__ == "__main__":
    unittest.main()
