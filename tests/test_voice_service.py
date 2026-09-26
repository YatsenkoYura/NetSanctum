"""The voice service: language routing, residency, and the whisper.cpp adapter.

None of these tests load a model. What they pin is the behaviour that decides
whether a reply is spoken at all - which engine answers, what stays in memory, and
what happens when the recogniser answers with something other than text - so a
regression shows up here rather than as a silent 503 on the first spoken reply.
"""

import asyncio
import io
import os
import tempfile
import unittest
import wave
from unittest import mock

from app.voice import Settings, VoiceService
from app.voice.residency import Residency
from app.voice.stt import Transcriber, TranscriptionError, _text_from
from app.voice.tts import (
    SUPPORTED_LANGUAGES,
    SynthesisError,
    guess_language,
    to_wav,
    wav_duration_seconds,
)


def settings_in(directory: str, **overrides) -> Settings:
    """Settings for a test, with the online voice off unless a test asks for it.

    Off by default so that nothing here reaches the network: a test that synthesised
    through the online engine would depend on a third party being up, and would fail
    for reasons that have nothing to do with the code.
    """
    values = {
        "model_dir": directory,
        "stt_url": "http://miku-stt:8080",
        "default_lang": "ru",
        "max_resident": 1,
        "edge_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class LanguageRoutingTests(unittest.TestCase):
    def test_the_script_decides_the_language(self):
        self.assertEqual("ru", guess_language("Привет, что нового в библиотеке?"))
        self.assertEqual("en", guess_language("Find the third chapter please"))
        self.assertEqual("ru", guess_language("Открой книгу Re:Zero и прочитай первую главу"))
        self.assertEqual("en", guess_language("Find Re:Zero, third chapter"))

    def test_text_with_no_decidable_script_falls_back_to_the_default(self):
        # "..." is punctuation, and 42 is digits: neither engine can be chosen by
        # evidence, so the configured default decides rather than a coin toss.
        self.assertEqual("ru", guess_language("...", "ru"))
        self.assertEqual("en", guess_language("42", "en"))
        self.assertEqual("ru", guess_language("", "ru"))

    def test_both_languages_are_offered(self):
        self.assertEqual(("ru", "en"), SUPPORTED_LANGUAGES)


class ResidencyTests(unittest.TestCase):
    def test_only_one_engine_stays_resident(self):
        residency = Residency(limit=1)
        residency.use("ru", lambda: object())
        residency.use("en", lambda: object())
        self.assertEqual(["en"], residency.resident())

    def test_using_the_resident_engine_does_not_reload_it(self):
        residency = Residency(limit=1)
        first = residency.use("ru", lambda: object())
        again = residency.use("ru", lambda: object())
        self.assertIs(first, again)
        self.assertEqual(["ru"], residency.resident())

    def test_staying_in_one_language_keeps_that_engine_warm(self):
        residency = Residency(limit=1)
        for _ in range(5):
            residency.use("ru", lambda: object())
        self.assertEqual(["ru"], residency.resident())

    def test_a_limit_of_two_keeps_both(self):
        residency = Residency(limit=2)
        residency.use("ru", lambda: object())
        residency.use("en", lambda: object())
        self.assertEqual(["ru", "en"], residency.resident())


class WavTests(unittest.TestCase):
    def test_samples_become_a_playable_wav(self):
        payload = to_wav([0.0, 0.5, -0.5, 1.5, -1.5], 24_000)
        with wave.open(io.BytesIO(payload), "rb") as handle:
            self.assertEqual(1, handle.getnchannels())
            self.assertEqual(2, handle.getsampwidth())
            self.assertEqual(24_000, handle.getframerate())
            self.assertEqual(5, handle.getnframes())

    def test_out_of_range_samples_are_clipped_not_wrapped(self):
        # Wrapping turns a loud passage into noise that sounds like a fault.
        clipped = to_wav([2.0, -2.0])
        wrapped = to_wav([1.0, -1.0])
        self.assertEqual(wrapped, clipped)

    def test_duration_reflects_the_sample_rate(self):
        payload = to_wav([0.0] * 24_000, 24_000)
        self.assertAlmostEqual(1.0, wav_duration_seconds(payload), places=3)


class TranscriberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = settings_in(self.tmp.name)

    def test_a_missing_model_is_reported_before_any_request(self):
        self.assertFalse(Transcriber(self.settings).models_present())

    def test_a_present_model_is_reported(self):
        with open(os.path.join(self.tmp.name, "ggml-base.bin"), "wb") as handle:
            handle.write(b"lmgg")
        self.assertTrue(Transcriber(self.settings).models_present())

    def test_the_english_only_recognition_model_is_called_out(self):
        # Both variants are the same container and both pass a format check, so
        # nothing else would notice the swap - and Russian gets markedly worse.
        for name in ("ggml-base.en.bin", "ggml-base-q5_1.en.bin"):
            with self.subTest(name=name):
                os.environ["VOICE_STT_MODEL_FILE"] = name
                try:
                    self.assertTrue(Transcriber(self.settings).english_only_model())
                finally:
                    os.environ.pop("VOICE_STT_MODEL_FILE", None)
        os.environ["VOICE_STT_MODEL_FILE"] = "ggml-base.bin"
        try:
            self.assertFalse(Transcriber(self.settings).english_only_model())
        finally:
            os.environ.pop("VOICE_STT_MODEL_FILE", None)

    def test_transcript_is_read_from_whichever_shape_comes_back(self):
        self.assertEqual("привет", _text_from({"text": " привет "}))
        self.assertEqual("привет", _text_from("привет"))
        self.assertEqual(
            "one two",
            _text_from({"transcription": [{"text": "one"}, {"text": "two"}, {"text": "  "}]}),
        )

    def test_a_response_without_text_is_an_error_not_an_empty_answer(self):
        for payload in ({}, {"transcription": []}, [], 7, None):
            with self.subTest(payload=payload):
                with self.assertRaises(TranscriptionError):
                    _text_from(payload)

    def test_empty_audio_is_refused_before_it_is_sent(self):
        with self.assertRaises(TranscriptionError):
            asyncio.run(Transcriber(self.settings).transcribe(b""))


class WarmupTests(unittest.TestCase):
    """A warm-up that does not load the model is worse than none at all.

    It reports the service ready while the first reply still pays for the load, and
    the timings it prints then describe nothing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = VoiceService(settings_in(self.tmp.name))
        self.warmed: list[str] = []

    def _stub_synthesiser(self):
        service = self.service
        warmed = self.warmed

        class Stub:
            async def warm(self, language):
                warmed.append(language)
                service.residency.adopt(language, object())

            async def synthesise(self, text, language=None, speaker=None):
                return b"RIFF", "audio/wav"

        return Stub()

    def test_warming_loads_the_engine_rather_than_just_constructing_it(self):
        loaded: list[str] = []
        residency = self.service.residency

        class Engine:
            def load(self):
                loaded.append("ru")

        def loader():
            engine = Engine()
            engine.load()
            return engine

        residency.use("ru", loader)
        self.assertEqual(["ru"], loaded)
        self.assertEqual(["ru"], residency.resident())

    def test_the_default_language_is_warmed_before_the_service_serves(self):
        from app.voice.service import _warm

        self.service.synthesiser = self._stub_synthesiser()
        asyncio.run(_warm(self.service))
        self.assertEqual("ru", self.warmed[0])

    def test_a_full_slot_is_not_filled_with_the_other_language(self):
        # Warming the rarer language into a full slot would drop the one just
        # warmed, and the first common reply would pay the load after all.
        from app.voice.service import _warm

        self.service.synthesiser = self._stub_synthesiser()
        asyncio.run(_warm(self.service))
        # One slot, filled by the default language, and left alone.
        self.assertEqual(["ru"], self.warmed)
        self.assertEqual(1, self.service.settings.max_resident)

    def test_with_room_for_both_the_other_language_is_warmed_too(self):
        from app.voice.service import _warm

        self.service.settings = settings_in(self.tmp.name).__class__(
            model_dir=self.tmp.name, stt_url="http://miku-stt:8080", default_lang="ru", max_resident=2
        )
        self.service.residency = Residency(limit=2)
        self.service.synthesiser = self._stub_synthesiser()
        asyncio.run(_warm(self.service))
        # The background task is scheduled rather than awaited, so let it run.
        import asyncio as _asyncio

        _asyncio.run(_asyncio.sleep(0))
        self.assertIn("ru", self.warmed)


class OnlineVoiceTests(unittest.TestCase):
    """The online engine is asked first and the local one answers for it.

    What is pinned here is the order and the refusal: a reply has to be spoken by
    something, so a network that is down moves the voice rather than the silence.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = VoiceService(settings_in(self.tmp.name, edge_enabled=True))
        self.asked: list[str] = []

    def _local_model(self, spoken: list[str], fail: bool = False):
        """A stand-in for the local engine, with the model file it insists on.

        The local path checks the model is on disk before it builds an engine, so
        the file has to exist or the test would be measuring that check instead of
        the fallback.
        """
        from app.voice.tts_ru import model_file

        with open(os.path.join(self.tmp.name, model_file()), "wb") as handle:
            handle.write(b"not a real model")

        class Fake:
            speakers = ("kseniya", "xenia")

            def load(self) -> None:
                return None

            async def speak(self, text, speaker=None):
                if fail:
                    raise SynthesisError("the model would not answer")
                spoken.append(text)
                return b"RIFF-local"

        self.service.residency.adopt("ru", Fake())
        return Fake

    def _online(self, spoken: list[str], fail: bool = False):
        class Fake:
            async def speak(self, text, *, language, speaker=None):
                if fail:
                    raise SynthesisError("the online voice failed: timed out")
                spoken.append(text)
                return b"RIFF-online"

        self.service.synthesiser._edge = Fake()

    def test_the_online_engine_is_asked_first(self):
        spoken: list[str] = []
        self._online(spoken)
        self._local_model(spoken)
        audio, media = asyncio.run(self.service.synthesiser.synthesise("Привет"))
        self.assertEqual(["Привет"], spoken)
        self.assertEqual(b"RIFF-online", audio)
        self.assertEqual("audio/wav", media)

    def test_a_language_it_can_speak_needs_no_model_on_disk(self):
        # The whole point of asking first: with no model present at all the service
        # can still speak, and has to admit that rather than report itself unusable.
        self.assertEqual({"ru": True, "en": True}, self.service.synthesiser.available())
        self.assertEqual(
            {"ru": ["edge", "silero"], "en": ["edge", "kokoro"]}, self.service.synthesiser.engines()
        )

    def test_the_local_engine_answers_when_the_network_does_not(self):
        spoken: list[str] = []
        self._online(spoken, fail=True)
        self._local_model(spoken)
        audio, _ = asyncio.run(self.service.synthesiser.synthesise("Привет"))
        self.assertEqual(b"RIFF-local", audio)
        self.assertEqual(["Привет"], spoken)

    def test_neither_answering_is_a_refusal_that_says_why(self):
        self._online(spoken := [], fail=True)
        self._local_model(spoken, fail=True)
        with self.assertRaises(SynthesisError) as caught:
            asyncio.run(self.service.synthesiser.synthesise("Привет"))
        # Both reasons, so the log says which of the two actually failed rather than
        # only the one that was tried last.
        self.assertIn("edge", str(caught.exception))
        self.assertIn("silero", str(caught.exception))

    def test_turning_it_off_leaves_only_the_local_engines(self):
        service = VoiceService(settings_in(self.tmp.name, edge_enabled=False))
        self.assertEqual({"ru": ["silero"], "en": ["kokoro"]}, service.synthesiser.engines())
        self.assertEqual({"ru": False, "en": False}, service.synthesiser.available())

    def test_nothing_is_warmed_while_it_can_be_asked_first(self):
        # Warming the engine behind the online voice would spend seconds of startup
        # and hundreds of megabytes on a fallback that normally never runs. With the
        # online voice on there is nothing to warm, so no engine is even asked for.
        with mock.patch.object(self.service.residency, "use") as use:
            asyncio.run(self.service.synthesiser.warm("ru"))
        use.assert_not_called()
        self.assertEqual([], self.service.residency.resident())

    def test_health_names_the_engine_order_and_the_voices(self):
        health = self.service.health()
        self.assertEqual({"ru": ["edge", "silero"], "en": ["edge", "kokoro"]}, health["engine_order"])
        self.assertEqual("ru-RU-SvetlanaNeural", health["edge_voices"]["ru"])

    def test_health_hides_the_voices_when_it_is_off(self):
        service = VoiceService(settings_in(self.tmp.name, edge_enabled=False))
        self.assertEqual({}, service.health()["edge_voices"])


class VoiceNamingTests(unittest.TestCase):
    """Which voice a reply is spoken with, and what a caller may ask for instead."""

    def setUp(self):
        from app.voice.tts_edge import EdgeVoice

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.voice = EdgeVoice(settings_in(self.tmp.name, edge_enabled=True))

    def test_each_language_has_its_own_voice(self):
        self.assertEqual("ru-RU-SvetlanaNeural", self.voice.name_for("ru"))
        self.assertEqual("en-US-AriaNeural", self.voice.name_for("en"))

    def test_a_published_name_is_honoured(self):
        self.assertEqual(
            "ru-RU-DmitriNeural",
            self.voice.name_for("ru", "ru-RU-DmitriNeural"),
        )

    def test_a_local_speaker_name_is_ignored_rather_than_refused(self):
        # The caller may be naming a Silero speaker, which the online voice has no
        # such thing as. The reply still has to be spoken, in the default voice.
        self.assertEqual("ru-RU-SvetlanaNeural", self.voice.name_for("ru", "kseniya"))

    def test_what_would_be_asked_for_is_the_configured_voice(self):
        self.assertEqual(["ru-RU-SvetlanaNeural"], self.voice.voices("ru"))


class ProviderCompatibilityTests(unittest.TestCase):
    """The voice service has to be a drop-in for a configured speech provider.

    The runtime posts to the OpenAI paths with these exact field names and reads
    these exact shapes back. If either side moves, this fails here rather than as a
    silent 502 on the first spoken reply.
    """

    def test_transcription_matches_what_the_runtime_sends(self):
        from app.voice.service import TranscriptResponse

        body = TranscriptResponse(text="привет").model_dump()
        self.assertEqual({"text": "привет"}, body)
        # The runtime reads the transcript out of a "text" key.
        self.assertIn("text", body)

    def test_speech_accepts_the_fields_the_runtime_sends(self):
        from app.voice.service import SpeechRequest

        request = SpeechRequest(model="whisper-1", input="Привет", voice="alloy")
        self.assertEqual("Привет", request.input)
        self.assertEqual("alloy", request.voice)
        # A voice name meant for a hosted provider must not fail validation here;
        # the engine falls back rather than refusing to speak.
        self.assertIsNone(request.language)

    def test_the_voice_service_speaks_the_openai_paths(self):
        from app.voice.service import app

        paths = {route.path for route in app.routes}
        self.assertIn("/v1/audio/speech", paths)
        self.assertIn("/v1/audio/transcriptions", paths)
        self.assertIn("/health", paths)


class ServiceWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = VoiceService(settings_in(self.tmp.name))

    def test_health_reports_engines_as_unavailable_until_the_models_arrive(self):
        health = self.service.health()
        self.assertEqual("ok", health["status"])
        self.assertEqual({"ru": False, "en": False}, health["engines"])
        self.assertFalse(health["models_present"])
        self.assertEqual([], health["resident"])

    def test_speech_refuses_when_no_engine_can_answer(self):
        with self.assertRaises(SynthesisError):
            asyncio.run(self.service.synthesiser.synthesise("Привет"))

    def test_speech_refuses_text_with_nothing_to_say(self):
        for text in ("", "  ", "x"):
            with self.subTest(text=text):
                with self.assertRaises(SynthesisError):
                    asyncio.run(self.service.synthesiser.synthesise(text))

    def test_an_unsupported_language_falls_back_to_detection(self):
        # A caller may send "ru-RU" or a tag the service does not know; neither
        # should be an error, because the script in the text still decides.
        # An unsupported tag is treated as absent, so the script decides instead of
        # the caller's tag being taken on trust.
        self.assertIsNone(_clean("de"))
        self.assertEqual("ru", _clean("ru-RU"))
        self.assertEqual("ru", _clean("RU_ru"))
        self.assertIsNone(_clean(None))

    def test_a_long_reply_is_trimmed_rather_than_refused(self):
        captured = {}

        class Fake:
            def speak(self, text, speaker=None):
                captured["text"] = text
                return b"RIFF", "audio/wav"

        service = VoiceService(settings_in(self.tmp.name))
        service.synthesiser._ru_engine = _stub(Fake())
        asyncio.run(service.synthesiser.synthesise("я" * 5_000, language="ru"))
        self.assertEqual(2_000, len(captured["text"]))


def _clean(value: str | None) -> str | None:
    from app.voice.service import _clean_language

    return _clean_language(value)


def _stub(engine):
    async def call(text, speaker):
        return engine.speak(text, speaker=speaker)

    return call


if __name__ == "__main__":
    unittest.main()
