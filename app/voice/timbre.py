"""The timbre stage: one voice instead of two engines.

Recognition and synthesis answer with two different voices, which is audible as a
different person answering in a different language. Voice conversion is the stage
that would make both come out as the same character, and it is the one part of the
pipeline that is not built here.

That is a deliberate omission rather than a stub that pretends. The available
options were a runtime from the original project, whose repository is no longer
reachable, or one of the published packages, which drags in fairseq 0.12, gradio
and audio-separator. Shipping either untested would put an unverifiable audio path
in front of the user: the only way to tell a working conversion from a wrong one
is by ear, and unlike every other stage here there is no measurement that shows it.

So the stage exists, is wired into the pipeline, and reports precisely what is
missing. When it is asked for and cannot deliver, it refuses rather than passing
audio through: audio that sounds like the plain engine while a character voice was
asked for is worse than an error, because it looks like it worked.
"""

from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass

# The voice preset is a TorchScript-era RVC checkpoint, and the feature extractors
# are published as ONNX. Named here so the fetcher, the health report and the error
# all speak about the same files.
VOICE_FILE = "miku_default_rvc.pth"
HUBERT_FILE = "hubert_base_layer12_32000.onnx"
PITCH_FILE = "rmvpe.onnx"


class TimbreUnavailableError(RuntimeError):
    """The character voice was asked for and cannot be produced."""


@dataclass(frozen=True)
class TimbreStatus:
    enabled: bool
    runtime: bool
    missing: tuple[str, ...]

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        if not self.missing and self.runtime:
            return "active"
        if self.missing:
            return f"missing: {', '.join(self.missing)}"
        return "no runtime available"


class Timbre:
    """The conversion stage, present in the pipeline and honest about its state."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self.enabled = _requested()
        # A runtime would be constructed here once one is available. Nothing is
        # registered, so nothing claims to convert.
        self._runtime = None

    def status(self) -> TimbreStatus:
        return TimbreStatus(
            enabled=self.enabled,
            runtime=self._runtime is not None,
            missing=self.missing_files(),
        )

    def missing_files(self) -> tuple[str, ...]:
        root = self._settings.model_dir
        names = (VOICE_FILE, HUBERT_FILE, PITCH_FILE)
        return tuple(name for name in names if not os.path.isfile(os.path.join(root, name)))

    def apply(self, audio: bytes, sample_rate: int | None = None) -> bytes:
        """Convert a synthesised reply into the character voice.

        Refuses when the stage is on and cannot do the work, and passes the audio
        through untouched when it is off, so turning it on is a decision with an
        outcome rather than a setting that quietly does nothing.
        """
        if not self.enabled:
            return audio
        if self._runtime is None:
            raise TimbreUnavailableError(
                f"the character voice is switched on but not installed: {self.status().describe()}"
            )
        return self._runtime.convert(audio, sample_rate)


def _requested() -> bool:
    return os.environ.get("VOICE_TIMBRE", "0") not in {"0", "false", "False", ""}


def wav_info(payload: bytes) -> tuple[int, int, int]:
    """Channels, sample rate, frame count - the three things conversion must keep."""
    with wave.open(io.BytesIO(payload), "rb") as handle:
        return handle.getnchannels(), handle.getframerate(), handle.getnframes()


__all__ = [
    "HUBERT_FILE",
    "PITCH_FILE",
    "VOICE_FILE",
    "Timbre",
    "TimbreStatus",
    "TimbreUnavailableError",
    "wav_info",
]
