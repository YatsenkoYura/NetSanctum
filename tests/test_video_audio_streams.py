import unittest

from app.core.security import get_current_user
from app.modules.video_archiver.audio_streams import build_audio_command
from app.modules.video_archiver.router import router


class VideoAudioStreamTests(unittest.TestCase):
    def test_builds_seekable_dfpwm_stream_for_computercraft(self):
        command = build_audio_command("video.mp4", 12.5, "dfpwm", seekable=True)

        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertIn("dfpwm", command)
        self.assertIn("48000", command)
        self.assertIn("1", command)

    def test_stream_input_seeks_after_input_and_mp3_stays_default(self):
        command = build_audio_command("pipe:0", 4, "mp3", seekable=False)

        self.assertGreater(command.index("-ss"), command.index("-i"))
        self.assertIn("libmp3lame", command)
        self.assertNotIn("dfpwm", command)

    def test_audio_endpoint_requires_owner_and_exposes_format_and_time(self):
        route = next(route for route in router.routes if route.path.endswith("/{video_id}/audio"))
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        query_aliases = {parameter.alias for parameter in route.dependant.query_params}

        self.assertIn(get_current_user, dependencies)
        self.assertIn("format", query_aliases)
        self.assertIn("time", query_aliases)


if __name__ == "__main__":
    unittest.main()
