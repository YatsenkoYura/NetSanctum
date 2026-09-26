"""The timbre stage: one voice instead of two engines.

Recognition and synthesis answer with two different voices, which is audible as a
different person answering in a different language. Voice conversion is the stage
that makes both come out as the same character.

The runtime is upstream code, MIT, vendored into the image at a pinned commit and
called as a library; `app.voice.rvc` is the shim that adapts it to this service. It
is not a stub: when it is asked for and cannot deliver, it refuses rather than
passing audio through, because audio in the plain engine's voice while a character
voice was requested looks exactly like it worked.

Conversion is slow - about half real time - so the stage is opt-in twice over. It
has to be switched on, and the model is the operator's own file rather than
something this service ships, because whose voice a model speaks is not a decision
that belongs in a repository.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Kept under the names the fetcher and the health report already speak about, so an
# operator who set this up before still recognises them.
VOICE_FILE = "miku_default_rvc.pth"
HUBERT_FILE = "hubert_base"
PITCH_FILE = "rmvpe.pt"


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
        if self.missing:
            return f"missing: {', '.join(self.missing)}"
        if not self.runtime:
            return "no runtime available"
        return "active"


class Timbre:
    """The conversion stage, wired into the pipeline and honest about its state."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self.enabled = _requested()
        self._runtime = None
        self._reported = None
        if self.enabled:
            self._runtime = self._build_runtime()

    def _build_runtime(self):
        """The runtime, when it can be constructed without failing at import.

        Built on the spot rather than at import so that a missing runtime is a report
        and not an import error: the service still has to start, still has to answer
        with a plain voice, and still has to say what is wrong with the character one.
        """
        try:
            from app.voice.rvc import RvcRuntime

            runtime = RvcRuntime(self._settings)
        except Exception as error:
            logger.warning("the character voice runtime is unavailable: %s", error)
            return None
        missing = runtime.missing()
        if missing:
            # Not an error yet: the operator may be about to place the model, and the
            # stage is asked for only when a reply is actually spoken.
            logger.info("character voice not ready, missing: %s", ", ".join(missing))
        return runtime

    def status(self) -> TimbreStatus:
        missing = ()
        if self._runtime is not None:
            missing = self._runtime.missing()
        elif self.enabled:
            missing = ("rvc runtime",)
        return TimbreStatus(
            enabled=self.enabled,
            runtime=self._runtime is not None and not missing,
            missing=missing,
        )

    def missing_files(self) -> tuple[str, ...]:
        return self.status().missing

    async def warm(self) -> None:
        """Load the conversion models, so the first reply does not pay for it.

        Left to the first request on purpose when the files are not there: paying tens
        of seconds of startup for a model that is not installed helps nobody.
        """
        if not self.enabled or self._runtime is None:
            return
        if self._runtime.available():
            await asyncio.to_thread(self._runtime.load)

    async def apply(self, audio: bytes, sample_rate: int | None = None) -> bytes:
        """Convert a synthesised reply into the character voice.

        Refuses when the stage is on and cannot do the work, and passes the audio
        through untouched when it is off, so turning it on is a decision with an
        outcome rather than a setting that quietly does nothing. The conversion runs
        in a thread because it is seconds of arithmetic and this is a server.
        """
        if not self.enabled:
            return audio
        if self._runtime is None or not self._runtime.available():
            raise TimbreUnavailableError(
                f"the character voice is switched on but not installed: {self.status().describe()}"
            )
        from app.voice.rvc import ConversionError

        try:
            return await asyncio.to_thread(self._runtime.convert, audio, sample_rate)
        except ConversionError:
            raise
        except Exception as error:
            raise TimbreUnavailableError(f"the character voice failed: {error}") from error


def _requested() -> bool:
    return os.environ.get("VOICE_TIMBRE", "0") not in {"0", "false", "False", ""}


__all__ = [
    "HUBERT_FILE",
    "PITCH_FILE",
    "VOICE_FILE",
    "Timbre",
    "TimbreStatus",
    "TimbreUnavailableError",
]
