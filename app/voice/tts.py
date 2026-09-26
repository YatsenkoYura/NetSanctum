"""Synthesis: one route, two engines, chosen by the language of the text.

Neither engine speaks both languages, so the choice is not a preference but a
requirement: the English model has no Russian at all, and the Russian one has no
English worth listening to. The language is taken from the request when the caller
knows it - the assistant knows, because it wrote the text - and guessed from the
text itself otherwise.

Both engines answer 24 kHz mono WAV, which is what the browser and any later
voice conversion expect, so nothing downstream has to care which one spoke.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import wave
from array import array

SUPPORTED_LANGUAGES = ("ru", "en")
DEFAULT_SAMPLE_RATE = 24_000
MAX_TEXT_CHARS = 2_000
# Below this a reply is a confirmation rather than a sentence, and loading a model
# to say "ок" costs more than it is worth.
MIN_SYNTHESIS_CHARS = 2
logger = logging.getLogger(__name__)
# Above this a reply is unusually slow and worth a look: a warm engine is well
# under a second, and anything near this bound means a model was loaded late.
SLOW_SYNTHESIS_SECONDS = 2.0

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")


class SynthesisError(RuntimeError):
    """No engine could speak the text."""


def guess_language(text: str, default: str = "ru") -> str:
    """Decide the language from the text, falling back when it is ambiguous.

    Mixed text is decided by which script appears more, because a Russian sentence
    quoting an English title is still a Russian sentence.
    """
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if cyrillic == latin:
        return default
    return "ru" if cyrillic > latin else "en"


class Synthesiser:
    def __init__(self, settings, residency) -> None:
        self._settings = settings
        self._residency = residency

    def available(self) -> dict:
        """Which engines could run right now, without loading anything."""
        return {
            "ru": _silero_ready(self._settings),
            "en": _kokoro_ready(self._settings),
        }

    def speakers(self, language: str) -> list[str]:
        """What can be asked for, discovered rather than declared.

        Answering this loads the engine, which is the price of listing a voice
        honestly: the names live inside the model file. A caller that only ever
        uses the default never pays it.
        """
        if language == "ru":
            if not _silero_ready(self._settings):
                return []
            engine = self._residency.use("ru", lambda: _silero(self._settings))
            return [str(index) for index in engine.speakers()]
        if not _kokoro_ready(self._settings):
            return []
        engine = self._residency.use("en", lambda: _kokoro(self._settings))
        return list(engine.voices())

    async def warm(self, language: str) -> None:
        """Load an engine without speaking, so the first reply does not pay for it.

        The load is awaited: returning early would leave the model unread and the
        first request paying for it, which is the whole thing this avoids.
        """

        def prepare() -> object:
            engine = _silero(self._settings) if language == "ru" else _kokoro(self._settings)
            # run in a thread: loading blocks for seconds and this is a server
            engine.load()
            return engine

        return await asyncio.to_thread(self._residency.use, "ru" if language == "ru" else "en", prepare)

    async def synthesise(
        self,
        text: str,
        *,
        language: str | None = None,
        speaker: str | None = None,
    ) -> tuple[bytes, str]:
        """Return (wav bytes, media type)."""
        text = (text or "").strip()
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
        if len(text) < MIN_SYNTHESIS_CHARS:
            raise SynthesisError("there is nothing to say")
        lang = (language or "").casefold()
        if lang not in SUPPORTED_LANGUAGES:
            lang = guess_language(text, self._settings.default_lang)
        if lang == "ru":
            engine = self._ru_engine
        else:
            engine = self._en_engine
        # The gap between a request arriving and the first sample of it existing is
        # the number the person waiting actually feels, so it is measured rather
        # than assumed: a cold model here is seconds, a warm one is fractions.
        started = time.perf_counter()
        result = await engine(text, speaker)
        elapsed = time.perf_counter() - started
        if elapsed > SLOW_SYNTHESIS_SECONDS:
            logger.warning(
                "synthesis took %.2f s for %d characters in %s",
                elapsed,
                len(text),
                lang,
            )
        else:
            logger.info("synthesised %d characters in %s in %.2f s", len(text), lang, elapsed)
        return result

    async def _ru_engine(self, text: str, speaker: str | None):
        from app.voice.tts_ru import SileroVoice

        ready = _silero_ready(self._settings)
        if not ready:
            raise SynthesisError("the Russian voice is not available")
        model = self._residency.use(
            "ru",
            lambda: SileroVoice(self._settings),
        )
        wav = await model.speak(text, speaker=speaker)
        return wav, "audio/wav"

    async def _en_engine(self, text: str, speaker: str | None):
        from app.voice.tts_en import KokoroVoice

        ready = _kokoro_ready(self._settings)
        if not ready:
            raise SynthesisError("the English voice is not available")
        model = self._residency.use(
            "en",
            lambda: KokoroVoice(self._settings),
        )
        wav = await model.speak(text, speaker=speaker)
        return wav, "audio/wav"


# ── what is on disk ────────────────────────────────────────────────────────────


def _path(settings, *parts: str) -> str:
    return os.path.join(settings.model_dir, *parts)


def _silero_file(settings) -> str:
    from app.voice.tts_ru import model_file

    return model_file()


def _kokoro_file(settings) -> str:
    from app.voice.tts_en import model_file

    return model_file()


def _kokoro_voices_file(settings) -> str:
    from app.voice.tts_en import voices_file

    return voices_file()


def _silero_ready(settings) -> bool:
    return os.path.isfile(_path(settings, _silero_file(settings)))


def _kokoro_ready(settings) -> bool:
    # The package takes the model and one voice file, both by path; the voices live
    # inside that file rather than as a directory of per-voice files.
    return os.path.isfile(_path(settings, _kokoro_file(settings))) and os.path.isfile(
        _path(settings, _kokoro_voices_file(settings))
    )


def _silero(settings):
    from app.voice.tts_ru import SileroVoice

    return SileroVoice(settings)


def _kokoro(settings):
    from app.voice.tts_en import KokoroVoice

    return KokoroVoice(settings)


# ── shared helpers ─────────────────────────────────────────────────────────────


def to_wav(samples, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """Wrap float samples in a WAV container, clipping rather than wrapping.

    Written against the standard library on purpose: the engines hand over numpy
    arrays, but nothing here needs numpy, and the rest of this package has to stay
    importable and testable without the synthesis stack installed.
    """
    pcm = array("h", (int(max(-1.0, min(1.0, float(value))) * 32767) for value in samples))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def wav_duration_seconds(payload: bytes) -> float:
    with wave.open(io.BytesIO(payload), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or DEFAULT_SAMPLE_RATE
    return frames / float(rate or DEFAULT_SAMPLE_RATE)


__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "MAX_TEXT_CHARS",
    "SUPPORTED_LANGUAGES",
    "SynthesisError",
    "Synthesiser",
    "guess_language",
    "to_wav",
    "wav_duration_seconds",
]
