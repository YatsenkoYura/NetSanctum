"""The Russian voice: Silero, loaded straight from the model archive.

The published `silero-tts` wrapper is not used. Its constructor downloads a model
index over the network every time an engine is created, which a service on an
internal network cannot do, and the archive it would load is all it ends up using
anyway. Loading the archive directly needs nothing but torch, and the model carries
its own speaker list - so the voices are read from the model rather than declared
here.

The model places stress and handles ё itself, which is why it is used instead of an
English engine pointed at Russian text. It is TorchScript, which is why torch is a
dependency of this service at all.
"""

from __future__ import annotations

import asyncio
import os
import threading

SAMPLE_RATE = 24_000
MODEL_FILE = "v5_ru_ru.pt"
# The two female voices, in the order they sound warmest. The male ones are there
# for variety rather than as a default.
PREFERRED_SPEAKERS = ("kseniya", "xenia", "baya")
FALLBACK_SPEAKER = "kseniya"


class SileroVoice:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._model = None
        self._speakers: tuple[str, ...] = ()
        # Re-entrant on purpose: load() warms by synthesising, and a plain lock
        # held across that call would deadlock the second entry into itself.
        self._lock = threading.RLock()

    def _load(self):
        from torch.package import PackageImporter

        _use_allowed_threads()
        path = os.path.join(self._settings.model_dir, model_file())
        # The archive holds a pickled submodule under this name; the package's own
        # loader is the supported way in, and anything else fails on the layout.
        self._model = PackageImporter(path).load_pickle("tts_models", "model")
        self._speakers = tuple(str(name) for name in getattr(self._model, "speakers", ()) or ())
        return self._model

    def load(self) -> None:
        """Read the archive, build the engine, and speak one short throwaway line.

        Separate from construction on purpose: the constructor only prepares a
        wrapper, and a warm-up that skipped the actual load would report itself
        ready while the first reply still paid for the model. The throwaway line is
        what makes the warm-up worth its seconds - the first synthesis of a process
        builds the runtimes the model executes, and that cost is otherwise paid by
        the first thing anybody says.
        """
        with self._lock:
            if self._model is not None:
                return
            self._load()
            # One throwaway line here rather than through _speak_sync: that path
            # calls back into load(), and this lock is held for the duration.
            self._model.apply_tts(
                text="Готова.",
                speaker=_speaker_name(None, self._speakers),
                sample_rate=SAMPLE_RATE,
                put_accent=True,
                put_yo=True,
            )

    def speakers(self) -> tuple[str, ...]:
        if not self._speakers:
            with self._lock:
                if not self._speakers:
                    self._load()
        return self._speakers

    async def speak(self, text: str, speaker: str | None = None) -> bytes:
        return await asyncio.to_thread(self._speak_sync, text, speaker)

    def _speak_sync(self, text: str, speaker: str | None) -> bytes:
        from app.voice.tts import to_wav

        self.load()
        with self._lock:
            model = self._model
            chosen = _speaker_name(speaker, self._speakers)
            audio = model.apply_tts(
                text=text,
                speaker=chosen,
                sample_rate=SAMPLE_RATE,
                put_accent=True,
                put_yo=True,
            )
        samples = audio.numpy() if hasattr(audio, "numpy") else audio
        return to_wav(samples, SAMPLE_RATE)


def model_file() -> str:
    return os.environ.get("VOICE_TTS_RU_FILE", MODEL_FILE)


def _speaker_name(requested: str | None, available: tuple[str, ...]) -> str:
    """Accept a name or an index, and never fail over an unknown one.

    A wrong speaker falls back rather than raising: the reply still gets spoken,
    which matters more than the voice being the intended one.
    """
    if not available:
        return requested or FALLBACK_SPEAKER
    if requested:
        text = str(requested).strip()
        for name in available:
            if name.casefold() == text.casefold():
                return name
        if text.isdigit():
            index = int(text)
            if 0 <= index < len(available):
                return available[index]
    for name in PREFERRED_SPEAKERS:
        if name in available:
            return name
    return available[0]


def _use_allowed_threads() -> None:
    """Match torch's thread count to the CPUs this container may actually use.

    Left to itself torch sizes its pool from the host's core count, so a container
    capped at a few cores runs several workers per core; the contention costs more
    than the parallelism buys on a model this size.
    """
    import torch

    try:
        allowed = int(os.environ["VOICE_THREADS"])
    except (KeyError, ValueError):
        allowed = _cpu_allowance()
    try:
        torch.set_num_threads(max(1, allowed))
    except Exception:  # pragma: no cover - defensive
        pass


def _cpu_allowance() -> int:
    """Read the cgroup CPU quota, falling back to what the machine reports."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as handle:
            quota, period = handle.read().split()
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return max(1, os.cpu_count() or 1)


__all__ = ["FALLBACK_SPEAKER", "MODEL_FILE", "PREFERRED_SPEAKERS", "SAMPLE_RATE", "SileroVoice"]
