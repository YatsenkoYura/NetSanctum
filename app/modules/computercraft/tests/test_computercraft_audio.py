import unittest

from app.core.security import get_current_user
from app.modules.computercraft.router import router
from app.modules.computercraft.streaming import build_audio_command


class ComputerCraftAudioTests(unittest.TestCase):
    def test_builds_seekable_dfpwm_stream(self):
        command = build_audio_command("video.mp4", 12.5, "dfpwm", seekable=True)
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertIn("dfpwm", command)
        self.assertIn("48000", command)

    def test_stream_input_seeks_after_input_and_supports_mp3(self):
        command = build_audio_command("pipe:0", 4, "mp3", seekable=False)
        self.assertGreater(command.index("-ss"), command.index("-i"))
        self.assertIn("libmp3lame", command)

    def test_audio_endpoint_requires_owner_and_exposes_format_and_time(self):
        route = next(route for route in router.routes if route.path.endswith("/{item_id}/audio"))
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        query_aliases = {parameter.alias for parameter in route.dependant.query_params}
        self.assertIn(get_current_user, dependencies)
        self.assertLessEqual({"format", "time"}, query_aliases)


if __name__ == "__main__":
    unittest.main()
