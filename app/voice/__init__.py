"""Server-side speech: recognition and synthesis, in one process.

This service exists so that choosing the browser's own speech support costs the
server nothing. It is an opt-in compose profile, and when it is off nothing here
is running at all - which is why it holds its models lazily and keeps only one
resident: the Russian engine needs torch, and holding both languages at once
roughly doubles the footprint for no benefit, since a reply is spoken in one
language.

It deliberately cannot see the database, the module registry or the filesystem
outside the model directory. The web process reaches it through the isolated agent
runtime and nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.voice.residency import Residency
from app.voice.stt import Transcriber
from app.voice.tts import Synthesiser

MODEL_DIR = os.environ.get("VOICE_MODEL_DIR", "/models")
STT_URL = os.environ.get("VOICE_STT_URL", "http://miku-stt:8080")
DEFAULT_LANG = os.environ.get("VOICE_LANG_DEFAULT", "ru")
MAX_RESIDENT = int(os.environ.get("VOICE_MAX_RESIDENT", "1"))
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 2_000


@dataclass(frozen=True)
class Settings:
    model_dir: str = MODEL_DIR
    stt_url: str = STT_URL
    default_lang: str = DEFAULT_LANG
    max_resident: int = MAX_RESIDENT

    @property
    def stt_model_file(self) -> str:
        return os.environ.get("VOICE_STT_MODEL_FILE", "ggml-base.bin")


def build(settings: Settings | None = None) -> VoiceService:
    return VoiceService(settings or Settings())


class VoiceService:
    """The two speech directions, plus an honest report of what is loaded."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.residency = Residency(limit=settings.max_resident)
        self.transcriber = Transcriber(settings)
        self.synthesiser = Synthesiser(settings, self.residency)

    def health(self) -> dict:
        return {
            "status": "ok",
            "stt_url": self.settings.stt_url,
            "models_present": self.transcriber.models_present(),
            "resident": self.residency.resident(),
            "engines": self.synthesiser.available(),
            "default_lang": self.settings.default_lang,
        }
