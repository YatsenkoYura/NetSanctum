"""Synthesis: one route, three engines, chosen by the language of the text.

The language decides the engine rather than a preference, because no engine here
speaks both languages well. The language is taken from the request when the caller
knows it - the assistant knows, because it wrote the text - and guessed from the
text itself otherwise.

Within a language the order is fixed by what the voice sounds like. The online
neural voice is asked first because it is the only one that sounds like a person;
the local engines answer when it cannot. That is a deliberate trade: the local
engines are free, offline and private, and the online one is none of those, so it
is a setting rather than the only option.

Every engine answers 24 kHz mono WAV, which is what the browser and any later voice
conversion expect, so nothing downstream has to care which one spoke.
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
    def __init__(self, settings, residency, timbre=None) -> None:
        self._settings = settings
        self._residency = residency
        # Optional last stage, so a reply leaves the service in one voice rather
        # than in whichever engine happened to speak it.
        self._timbre = timbre
        self._edge = None

    def _online(self):
        """The online engine, built on first use so the setting is read once."""
        if self._edge is None:
            from app.voice.tts_edge import EdgeVoice

            self._edge = EdgeVoice(self._settings)
        return self._edge

    def available(self) -> dict:
        """What can speak a language right now, without loading anything.

        A language the online voice can answer is available even with no model on
        disk, and that has to show here or the service would report itself
        unusable while being able to speak.
        """
        online = self._settings.edge_enabled
        return {
            "ru": _silero_ready(self._settings) or online,
            "en": _kokoro_ready(self._settings) or online,
        }

    def engines(self) -> dict:
        """Which engine would answer each language, in the order they would try."""
        online = "edge" if self._settings.edge_enabled else None
        return {
            "ru": [name for name in (online, "silero") if name],
            "en": [name for name in (online, "kokoro") if name],
        }

    def speakers(self, language: str) -> list[str]:
        """What can be asked for, discovered rather than declared.

        The online voice's name is configuration, so it is free to report. The local
        engines carry their voices inside the model file, and answering honestly
        means loading it - which is the price of listing a voice truthfully, paid
        only by a caller that actually wants the list.
        """
        if language == "ru":
            if self._settings.edge_enabled:
                return self._online().voices("ru")
            if not _silero_ready(self._settings):
                return []
            engine = self._residency.use("ru", lambda: _silero(self._settings))
            return [str(index) for index in engine.speakers()]
        if self._settings.edge_enabled:
            return self._online().voices("en")
        if not _kokoro_ready(self._settings):
            return []
        engine = self._residency.use("en", lambda: _kokoro(self._settings))
        return list(engine.voices())

    async def warm(self, language: str) -> None:
        """Load an engine without speaking, so the first reply does not pay for it.

        Nothing is loaded while the online voice is enabled and reachable in
        principle: it needs no loading, and pre-loading the engine behind it would
        spend seconds of startup and hundreds of megabytes on a fallback that
        normally never runs. The cost moves to the moment it is actually needed,
        which is the moment worth paying it.

        The load is awaited: returning early would leave the model unread and the
        first request paying for it, which is the whole thing this avoids.
        """
        if self._settings.edge_enabled:
            return

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
        if self._timbre is None:
            return result
        audio, media_type = result
        # Awaited rather than called: conversion is seconds of arithmetic, and this is
        # a server that also has to answer the next request while it happens.
        return await self._timbre.apply(audio), media_type

    async def _speak_with_fallback(self, text: str, speaker: str | None, attempts):
        """Try each engine in turn and speak with whichever answers.

        The online voice is asked first and the local engine behind it, and a failure
        of the first is not a failure of synthesis: the reply still has to be spoken,
        and in the wrong voice rather than not at all. Every error is collected so
        the refusal says why both engines passed, instead of only the last one.
        """
        reasons: list[str] = []
        for name, attempt in attempts:
            try:
                return await attempt()
            except Exception as error:
                # Deliberately wide: the online engine raises the library's own
                # errors, a timeout arrives as asyncio's, and a missing model raises
                # something else again. None of them should end the reply, and
                # SynthesisError is the only failure a caller is meant to see.
                reasons.append(f"{name}: {error}")
                logger.warning("%s could not speak; trying the next engine", name, exc_info=True)
        raise SynthesisError("; ".join(reasons) or "no engine could answer")

    async def _ru_engine(self, text: str, speaker: str | None):
        from app.voice.tts_ru import SileroVoice

        async def local():
            if not _silero_ready(self._settings):
                raise SynthesisError("the Russian model is not on disk")
            model = self._residency.use(
                "ru",
                lambda: SileroVoice(self._settings),
            )
            return await model.speak(text, speaker=speaker), "audio/wav"

        async def online():
            return await self._online().speak(text, language="ru", speaker=speaker), "audio/wav"

        attempts = [("edge", online)] if self._settings.edge_enabled else []
        attempts.append(("silero", local))
        audio, media_type = await self._speak_with_fallback(text, speaker, attempts)
        return audio, media_type

    async def _en_engine(self, text: str, speaker: str | None):
        from app.voice.tts_en import KokoroVoice

        async def local():
            if not _kokoro_ready(self._settings):
                raise SynthesisError("the English voice is not on disk")
            model = self._residency.use(
                "en",
                lambda: KokoroVoice(self._settings),
            )
            return await model.speak(text, speaker=speaker), "audio/wav"

        async def online():
            return await self._online().speak(text, language="en", speaker=speaker), "audio/wav"

        attempts = [("edge", online)] if self._settings.edge_enabled else []
        attempts.append(("kokoro", local))
        audio, media_type = await self._speak_with_fallback(text, speaker, attempts)
        return audio, media_type


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
