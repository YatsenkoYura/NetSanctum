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
from app.voice.timbre import Timbre
from app.voice.tts import Synthesiser

MODEL_DIR = os.environ.get("VOICE_MODEL_DIR", "/models")
STT_URL = os.environ.get("VOICE_STT_URL", "http://miku-stt:8080")
DEFAULT_LANG = os.environ.get("VOICE_LANG_DEFAULT", "ru")
MAX_RESIDENT = int(os.environ.get("VOICE_MAX_RESIDENT", "1"))
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 2_000


def _edge_settings():
    from app.voice.tts_edge import edge_enabled, edge_rate, edge_timeout, edge_voices

    return edge_enabled(), edge_timeout(), edge_rate(), edge_voices()


_EDGE_ENABLED, _EDGE_TIMEOUT, _EDGE_RATE, _EDGE_VOICES = _edge_settings()


@dataclass(frozen=True)
class Settings:
    model_dir: str = MODEL_DIR
    stt_url: str = STT_URL
    default_lang: str = DEFAULT_LANG
    max_resident: int = MAX_RESIDENT
    # The online voice is configured here rather than read at the point of use, so a
    # service cannot be half-enabled: what it reports and what it does are the same
    # setting read once.
    edge_enabled: bool = _EDGE_ENABLED
    edge_timeout: float = _EDGE_TIMEOUT
    edge_rate: str = _EDGE_RATE
    edge_voices: tuple = ()

    @property
    def stt_model_file(self) -> str:
        return os.environ.get("VOICE_STT_MODEL_FILE", "ggml-base.bin")

    @property
    def edge_voice_map(self) -> dict:
        """The configured voice per language, as a mapping rather than a tuple.

        A dict on a frozen dataclass is not hashable, so the field keeps the raw
        value and this is where it is read; a frozen dataclass that could not be put
        in a set would be a trap for whoever caches a service by its settings.
        """
        return dict(self.edge_voices) if self.edge_voices else dict(_EDGE_VOICES)


def build(settings: Settings | None = None) -> VoiceService:
    return VoiceService(settings or Settings())


class VoiceService:
    """The two speech directions, plus an honest report of what is loaded."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.residency = Residency(limit=settings.max_resident)
        self.transcriber = Transcriber(settings)
        self.timbre = Timbre(settings)
        self.synthesiser = Synthesiser(settings, self.residency, self.timbre)

    def health(self) -> dict:
        return {
            "status": "ok",
            "stt_url": self.settings.stt_url,
            "models_present": self.transcriber.models_present(),
            "resident": self.residency.resident(),
            "engines": self.synthesiser.available(),
            "engine_order": self.synthesiser.engines(),
            "edge_voices": self.settings.edge_voice_map if self.settings.edge_enabled else {},
            "default_lang": self.settings.default_lang,
            "timbre": self.timbre.status().describe(),
        }
