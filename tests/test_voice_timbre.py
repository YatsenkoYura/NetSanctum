"""The character voice stage: present in the pipeline, honest about what it can do.

The stage is the one part of the pipeline with no implementation, and the tests
are about that rather than about audio: that it is off by default, that it passes
audio through untouched when off, and that it refuses loudly rather than speaking
in the plain engine's voice while a character was asked for.
"""

import io
import os
import tempfile
import unittest
import wave

from app.voice.timbre import (
    HUBERT_FILE,
    PITCH_FILE,
    VOICE_FILE,
    Timbre,
    TimbreUnavailableError,
    wav_info,
)


def a_wav(frames: int = 2400, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


class TimbreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = type("S", (), {"model_dir": self.tmp.name, "default_lang": "ru", "max_resident": 1})()
        self.addCleanup(lambda: os.environ.pop("VOICE_TIMBRE", None))
        os.environ.pop("VOICE_TIMBRE", None)

    def _place(self, *names: str) -> None:
        for name in names:
            with open(os.path.join(self.tmp.name, name), "wb") as handle:
                handle.write(b"PK\x03\x04")

    def test_it_is_off_unless_asked_for(self):
        self.assertEqual("off", Timbre(self.settings).status().describe())
        self.assertFalse(Timbre(self.settings).enabled)

    def test_when_off_the_audio_is_untouched(self):
        audio = a_wav()
        self.assertEqual(audio, Timbre(self.settings).apply(audio))

    def test_switching_it_on_without_weights_refuses_rather_than_faking_it(self):
        os.environ["VOICE_TIMBRE"] = "1"
        timbre = Timbre(self.settings)
        self.assertTrue(timbre.enabled)
        with self.assertRaises(TimbreUnavailableError) as caught:
            timbre.apply(a_wav())
        # The message has to say what is absent, or the operator is left guessing.
        self.assertIn(VOICE_FILE, str(caught.exception))
        self.assertIn("missing", str(caught.exception))

    def test_all_three_files_are_named_as_absent(self):
        self.assertEqual(
            (VOICE_FILE, HUBERT_FILE, PITCH_FILE),
            Timbre(self.settings).missing_files(),
        )

    def test_presence_of_weights_is_not_reported_as_working(self):
        # The trap this avoids: files on disk look installed, and a report saying
        # "active" would be a lie until a runtime can actually convert.
        os.environ["VOICE_TIMBRE"] = "1"
        self._place(VOICE_FILE, HUBERT_FILE, PITCH_FILE)
        status = Timbre(self.settings).status()
        self.assertEqual((), status.missing)
        self.assertFalse(status.runtime)
        self.assertEqual("no runtime available", status.describe())
        with self.assertRaises(TimbreUnavailableError):
            Timbre(self.settings).apply(a_wav())

    def test_a_partially_installed_voice_still_reports_what_is_missing(self):
        os.environ["VOICE_TIMBRE"] = "1"
        self._place(HUBERT_FILE)
        self.assertEqual((VOICE_FILE, PITCH_FILE), Timbre(self.settings).missing_files())
        self.assertIn("missing", Timbre(self.settings).status().describe())

    def test_the_wav_shape_a_conversion_must_preserve_is_readable(self):
        channels, rate, frames = wav_info(a_wav(frames=1200, rate=24000))
        self.assertEqual((1, 24000, 1200), (channels, rate, frames))


if __name__ == "__main__":
    unittest.main()
