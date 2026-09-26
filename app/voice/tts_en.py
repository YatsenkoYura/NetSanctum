"""The English voice: Kokoro, on ONNX Runtime.

The English model has no Russian and the Russian one has no English worth
listening to, which is why a reply is spoken by whichever engine matches it. This
one needs no torch - only the runtime - so it is the cheaper of the two, and the
int8 build is used because the difference is inaudible on a companion's line and
the full weights are three times the size.
"""

from __future__ import annotations

import asyncio
import os
import threading

SAMPLE_RATE = 24_000
DEFAULT_VOICE = "af_heart"
# The transformer variant of the G2P drags in a transformers install, which is
# larger than the model itself. The rule-based English path is all this needs.
G2P_TRANSFORMER = False


class KokoroVoice:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._kokoro = None
        self._g2p = None
        self._lock = threading.Lock()

    def _paths(self) -> tuple[str, str]:
        root = self._settings.model_dir
        return (
            os.path.join(root, model_file()),
            os.path.join(root, voices_file()),
        )

    def _load(self):
        from kokoro_onnx import Kokoro
        from misaki import en

        model_path, voices_path = self._paths()
        self._kokoro = Kokoro(model_path, voices_path)
        # british=False keeps the accent consistent with the voice names, and
        # fallback=None means an unknown word raises instead of being mangled.
        self._g2p = en.G2P(trf=G2P_TRANSFORMER, british=False, fallback=None)
        return self._kokoro

    def voices(self) -> list[str]:
        """The names the voice file actually carries.

        Read from the file rather than from a list written by hand: the names are
        the model's, and a hardcoded list drifts the moment the file is replaced.
        """
        import numpy as np

        _model_path, voices_path = self._paths()
        if not os.path.isfile(voices_path):
            return []
        with np.load(voices_path) as archive:
            return sorted(str(name) for name in archive.files)

    async def speak(self, text: str, speaker: str | None = None) -> bytes:
        return await asyncio.to_thread(self._speak_sync, text, speaker)

    def _speak_sync(self, text: str, speaker: str | None) -> bytes:
        import numpy as np

        from app.voice.tts import to_wav

        with self._lock:
            if self._kokoro is None:
                self._load()
            available = self.voices()
            phonemes, _tokens = self._g2p(text)
            # is_phonemes tells the engine the text is already phonemised. Without
            # it the engine runs its own espeak pass over the phonemes, which needs
            # a native library this service deliberately does not carry.
            samples, _rate = self._kokoro.create(
                phonemes,
                voice=_voice_name(speaker, available),
                speed=1.0,
                is_phonemes=True,
            )
        return to_wav(np.asarray(samples, dtype="float32"), SAMPLE_RATE)


def model_file() -> str:
    return os.environ.get("VOICE_TTS_EN_FILE", "kokoro-v1.0.int8.onnx")


def voices_file() -> str:
    return os.environ.get("VOICE_TTS_EN_VOICES", "voices-v1.0.bin")


def _voice_name(requested: str | None, available: list[str]) -> str:
    """Fall back rather than fail: a reply still gets spoken with the wrong voice."""
    if requested and requested in available:
        return requested
    if DEFAULT_VOICE in available:
        return DEFAULT_VOICE
    return available[0] if available else DEFAULT_VOICE


__all__ = ["DEFAULT_VOICE", "SAMPLE_RATE", "KokoroVoice", "model_file", "voices_file"]
