"""The online voice: Microsoft's neural voices, reached over the network.

This engine is the opposite of the two beside it. Nothing is downloaded, nothing
is loaded, no weights sit in memory, and the reply is exactly as available as the
network is. It earns its place by being the only voice in this service that sounds
like a person rather than a synthesiser, which is why it is asked first.

Two consequences are deliberate, and both are reasons it is optional rather than
the only engine:

  * the text of every reply leaves this machine for a third party;
  * the endpoint is the one the Edge browser speaks to, not a documented API, so
    it can change or stop without notice.

Hence the local engine behind it, and hence VOICE_EDGE=0 to refuse it outright.
Nothing here reaches the network until a reply is actually spoken, so a service
with the setting off is as unreachable as it was before this existed.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.voice.tts import DEFAULT_SAMPLE_RATE, SynthesisError, to_wav

logger = logging.getLogger(__name__)

# One voice per language, because a companion that answers in two unrelated
# voices reads as two systems. Both are the neural ones; the identifiers are what
# the service publishes, with no gender suffix, so the name in a configuration is
# the name that works.
DEFAULT_VOICES = {
    "ru": "ru-RU-SvetlanaNeural",
    "en": "en-US-AriaNeural",
}
# The reply is spoken in a voice that may not be reachable, and a caller waiting
# for a voice that is never coming is worse off than one told so.
DEFAULT_TIMEOUT_SECONDS = 30.0
# Below this an answer is a confirmation, and the round trip costs more than the
# word is worth. The local engines do not pay a network hop for "ок".
MIN_CHARS = 2


class EdgeVoice:
    """One voice, addressed by its published name, synthesised on demand."""

    def __init__(self, settings) -> None:
        self._settings = settings

    def enabled(self) -> bool:
        return bool(self._settings.edge_enabled)

    def name_for(self, language: str, requested: str | None = None) -> str:
        """Accept a requested voice, or fall back to the configured one per language.

        A requested name is only honoured when it looks like a voice this service
        could ask for. Anything else is ignored rather than refused: a caller that
        names a local speaker should still get a spoken reply.
        """
        if requested and _looks_like_edge_voice(requested):
            return requested
        return self._settings.edge_voice_map.get(language) or DEFAULT_VOICES.get(
            language, DEFAULT_VOICES["ru"]
        )

    def voices(self, language: str) -> list[str]:
        return [self.name_for(language)]

    async def speak(
        self,
        text: str,
        *,
        language: str,
        speaker: str | None = None,
        rate: str | None = None,
    ) -> bytes:
        """Return 24 kHz mono WAV, whatever shape the service answers in.

        The reply arrives as MP3, which the local engines never produce, so it is
        decoded here rather than passed on: everything downstream of this module -
        the browser, the recogniser, the timbre stage - only ever sees WAV.
        """
        text = (text or "").strip()
        if len(text) < MIN_CHARS:
            raise SynthesisError("there is nothing to say")
        voice = self.name_for(language, speaker)
        payload = await asyncio.wait_for(
            self._fetch(text, voice, rate),
            timeout=self._settings.edge_timeout,
        )
        if not payload:
            raise SynthesisError("the online voice returned no audio")
        return _decode(payload)

    async def _fetch(self, text: str, voice: str, rate: str | None) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=rate or self._settings.edge_rate,
        )
        audio = bytearray()
        try:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio" and chunk["data"]:
                    audio.extend(chunk["data"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The library raises a wide range of its own errors, and a caller only
            # needs to know the online voice could not answer so the local engine
            # can. The cause is kept in the message for the log rather than lost.
            raise SynthesisError(f"the online voice failed: {error}") from error
        return bytes(audio)


def _decode(payload: bytes) -> bytes:
    """MP3 to 24 kHz mono WAV, resampling in the decoder if it has to."""
    import miniaudio

    try:
        decoded = miniaudio.decode(
            payload,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=DEFAULT_SAMPLE_RATE,
        )
    except Exception as error:
        raise SynthesisError(f"the online voice sent audio that would not decode: {error}") from error
    if not len(decoded.samples):
        raise SynthesisError("the online voice sent no samples")
    # Samples arrive as int16, so they are divided back down rather than treated as
    # floats: to_wav clips to [-1, 1] and would otherwise wrap every second one.
    return to_wav((int(value) / 32768.0 for value in decoded.samples), decoded.sample_rate)


def _looks_like_edge_voice(name: str) -> bool:
    """Whether a caller-supplied string is a voice this service could ask for.

    The published names all look like ru-RU-SvetlanaNeural, so the check is on the
    shape rather than on a list: a list would go stale the moment a voice is added
    upstream, and refusing a valid name is worse than asking for an odd one.
    """
    candidate = name.strip()
    parts = candidate.split("-")
    return len(parts) >= 3 and candidate.replace("-", "").isalnum() and parts[-1].endswith(("Neural",))


def edge_enabled() -> bool:
    return os.environ.get("VOICE_EDGE", "1") not in {"0", "false", "no"}


def edge_timeout() -> float:
    try:
        return float(os.environ.get("VOICE_EDGE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def edge_rate() -> str:
    return os.environ.get("VOICE_EDGE_RATE", "+0%")


def edge_voices() -> dict[str, str]:
    """The configured voice per language, over the published defaults."""
    configured: dict[str, str] = {}
    for language, fallback in DEFAULT_VOICES.items():
        name = os.environ.get(f"VOICE_EDGE_VOICE_{language.upper()}", "").strip()
        configured[language] = name or fallback
    return configured


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_VOICES",
    "EdgeVoice",
    "edge_enabled",
    "edge_rate",
    "edge_timeout",
    "edge_voices",
]
