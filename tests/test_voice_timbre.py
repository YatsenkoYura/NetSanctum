"""The character voice stage: wired in, and honest about what it can actually do.

These tests do not convert anything - that needs the upstream runtime, a checkpoint
and a content model, and the only way to judge converted audio is by ear. What they
pin is everything around it: that the stage is off by default, that it passes audio
through untouched when off, that it refuses loudly instead of speaking in the plain
engine's voice while a character was asked for, and that a runtime which is present
but cannot answer is reported as unusable rather than as working.
"""

import asyncio
import io
import os
import tempfile
import unittest
import wave

from app.voice.rvc import RvcRuntime, checkpoint_file, index_rate
from app.voice.timbre import Timbre, TimbreUnavailableError


def a_wav(frames: int = 2400, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


class Settings:
    def __init__(self, model_dir: str) -> None:
        self.model_dir = model_dir
        self.default_lang = "ru"
        self.max_resident = 1


class TimbreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(self.tmp.name)
        self.addCleanup(lambda: os.environ.pop("VOICE_TIMBRE", None))
        os.environ.pop("VOICE_TIMBRE", None)

    def _place(self, *names: str) -> None:
        for name in names:
            path = os.path.join(self.tmp.name, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"not really a model")

    def _with_runtime(self, runtime):
        """A timbre whose stage is on and has a runtime, without needing one."""
        os.environ["VOICE_TIMBRE"] = "1"
        timbre = Timbre(self.settings)
        timbre._runtime = runtime
        return timbre

    def test_it_is_off_unless_asked_for(self):
        self.assertEqual("off", Timbre(self.settings).status().describe())
        self.assertFalse(Timbre(self.settings).enabled)

    def test_when_off_the_audio_is_untouched(self):
        audio = a_wav()
        self.assertEqual(audio, asyncio.run(Timbre(self.settings).apply(audio)))

    def test_switching_it_on_without_weights_refuses_rather_than_faking_it(self):
        os.environ["VOICE_TIMBRE"] = "1"
        timbre = Timbre(self.settings)
        self.assertTrue(timbre.enabled)
        with self.assertRaises(TimbreUnavailableError) as caught:
            asyncio.run(timbre.apply(a_wav()))
        # The message has to say what is absent, or the operator is left guessing.
        self.assertIn("missing", str(caught.exception))
        self.assertTrue(timbre.missing_files())

    def test_what_is_missing_is_named(self):
        os.environ["VOICE_TIMBRE"] = "1"
        missing = Timbre(self.settings).missing_files()
        self.assertIn("missing", Timbre(self.settings).status().describe())
        self.assertTrue(missing)

    def test_files_on_disk_are_not_reported_as_working(self):
        # The trap this avoids: files present look installed, and a report saying
        # "active" would be a lie until something can actually convert.
        os.environ["VOICE_TIMBRE"] = "1"
        runtime = RvcRuntime(self.settings)
        self._place(checkpoint_file(), os.path.join("hubert_base", "config.json"))
        timbre = self._with_runtime(runtime)
        if not runtime.missing():
            self.skipTest("the conversion runtime is installed here, so nothing is missing")
        # The runtime itself is the only thing absent in this environment, and that is
        # still not a working stage.
        self.assertFalse(timbre.status().runtime)
        with self.assertRaises(TimbreUnavailableError):
            asyncio.run(timbre.apply(a_wav()))

    def test_a_working_runtime_is_used_and_its_audio_returned(self):
        class Runtime:
            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def missing(self):
                return ()

            def load(self):
                self.calls.append("load")

            def convert(self, audio, sample_rate=None):
                self.calls.append(("convert", sample_rate))
                return b"RIFF-converted"

        runtime = Runtime()
        timbre = self._with_runtime(runtime)
        self.assertTrue(timbre.status().runtime)
        self.assertEqual("active", timbre.status().describe())
        self.assertEqual(b"RIFF-converted", asyncio.run(timbre.apply(a_wav(), 24000)))
        self.assertEqual([("convert", 24000)], runtime.calls)

    def test_a_runtime_that_fails_becomes_a_refusal_naming_the_reason(self):
        class Runtime:
            def available(self):
                return True

            def missing(self):
                return ()

            def load(self):
                return None

            def convert(self, audio, sample_rate=None):
                raise RuntimeError("the generator fell over")

        timbre = self._with_runtime(Runtime())
        with self.assertRaises(TimbreUnavailableError) as caught:
            asyncio.run(timbre.apply(a_wav()))
        self.assertIn("the generator fell over", str(caught.exception))

    def test_warming_loads_the_runtime_only_when_it_can_convert(self):
        class Runtime:
            def __init__(self, ready: bool):
                self.ready = ready
                self.loaded = 0

            def available(self):
                return self.ready

            def missing(self):
                return ()

            def load(self):
                self.loaded += 1

        ready = Runtime(True)
        asyncio.run(self._with_runtime(ready).warm())
        self.assertEqual(1, ready.loaded)
        unready = Runtime(False)
        asyncio.run(self._with_runtime(unready).warm())
        # Loading tens of seconds of model for a stage that cannot run helps nobody.
        self.assertEqual(0, unready.loaded)


class RuntimeSettingTests(unittest.TestCase):
    """The knobs an operator sets, read where they are used."""

    def setUp(self):
        for name in ("VOICE_RVC_CHECKPOINT", "VOICE_RVC_INDEX_RATE", "VOICE_RVC_INDEX"):
            os.environ.pop(name, None)
        self.addCleanup(
            lambda: [
                os.environ.pop(name, None)
                for name in ("VOICE_RVC_CHECKPOINT", "VOICE_RVC_INDEX_RATE", "VOICE_RVC_INDEX")
            ]
        )

    def test_the_model_is_the_operators_choice(self):
        self.assertEqual("miku_default_rvc.pth", checkpoint_file())
        os.environ["VOICE_RVC_CHECKPOINT"] = "MIKU820.pth"
        self.assertEqual("MIKU820.pth", checkpoint_file())

    def test_the_retrieval_rate_is_clamped_to_something_that_means_anything(self):
        os.environ["VOICE_RVC_INDEX_RATE"] = "2.5"
        self.assertEqual(1.0, index_rate())
        os.environ["VOICE_RVC_INDEX_RATE"] = "-1"
        self.assertEqual(0.0, index_rate())
        os.environ["VOICE_RVC_INDEX_RATE"] = "not a number"
        self.assertEqual(0.75, index_rate())


if __name__ == "__main__":
    unittest.main()
