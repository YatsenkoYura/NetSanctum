"""The conversion runtime: any engine's words, spoken as one character.

Voice conversion is the last stage of synthesis, and the only one that changes who is
speaking rather than what is said. The engine writes the words and the delivery; this
changes the voice underneath them. That is why it sits after the engine rather than
instead of it - a neural engine is the better source to convert, and a small local
one is not.

What is ours here is a shim; the conversion itself is upstream,
RVC-Project/Retrieval-based-Voice-Conversion-WebUI, MIT, vendored into the image at a
pinned commit and called as a library. It is not vendored into this repository: it is
several hundred files, and an in-tree copy is a liability nobody keeps honest.

Two things about it are worth knowing before it is switched on.

It is slow, and not through anything this code controls. Conversion runs at about
half real time on a few cores, so a reply is spoken roughly as long after it was
written as it is long. That is a property of the model, not a setting, and the
measured time is logged per conversion so the cost shows up in the log instead of
being discovered by a listener.

The voice is the operator's file, not this code's choice. The runtime reads a
checkpoint and converts into it. Who that voice belongs to is a question about the
file, and nothing in this module has an opinion about it.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import wave

from app.voice.tts import DEFAULT_SAMPLE_RATE, to_wav

logger = logging.getLogger(__name__)

# Where the image build puts the upstream checkout.
RVC_HOME = os.environ.get("VOICE_RVC_HOME", "/opt/rvc")
# Upstream reads its source at 16 kHz and generates at the checkpoint's own rate, and
# resamples to this on the way out when it is given one. Asking for the rate the rest
# of the service uses keeps the stage from adding a resample of its own.
OUTPUT_RATE = int(os.environ.get("VOICE_RVC_OUTPUT_RATE", DEFAULT_SAMPLE_RATE))
# Pitch offset in semitones, and how much of the pitch track is protected from it.
# Shifting by zero keeps the delivery the engine chose; protecting it keeps consonants
# from smearing on a voice that was not trained from this material.
PITCH_SHIFT = int(os.environ.get("VOICE_RVC_PITCH", "0"))
PROTECT = float(os.environ.get("VOICE_RVC_PROTECT", "0.33"))
# Above this a conversion is worth a warning, because it is the delay a listener feels.
SLOW_CONVERSION_SECONDS = float(os.environ.get("VOICE_RVC_WARN_SECONDS", "5"))


class ConversionError(RuntimeError):
    """The character voice could not be produced."""


def checkpoint_file() -> str:
    """The checkpoint. Named by the operator, because the model is theirs to pick."""
    return os.environ.get("VOICE_RVC_CHECKPOINT", "miku_default_rvc.pth")


def index_file() -> str:
    """The retrieval index, or empty to convert without one.

    Optional because a model converts without it; the index replaces extracted
    content with the nearest frames from the material the model was trained on, which
    is what makes the result resemble that material rather than only the source.
    """
    return os.environ.get("VOICE_RVC_INDEX", "added_IVF798_Flat_nprobe_1.index")


def hubert_dir() -> str:
    """The content model, a directory because upstream loads three files from it."""
    return os.environ.get("VOICE_RVC_HUBERT_DIR", "hubert_base")


def index_rate() -> float:
    try:
        return min(1.0, max(0.0, float(os.environ.get("VOICE_RVC_INDEX_RATE", "0.75"))))
    except ValueError:
        return 0.75


def f0_method() -> str:
    """Pitch tracking. `pm` needs no model and is the default; `rmvpe` tracks better."""
    return os.environ.get("VOICE_RVC_F0", "pm")


class RvcRuntime:
    """One converted voice, loaded once and reused for every reply.

    Reuse is the point of this being an object. Upstream loads the checkpoint, the
    content model and the index when the pipeline is built, which is tens of seconds;
    paying that per sentence would cost more than the conversion.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._vc = None
        # Held for the life of the process: a repaired checkpoint is written here on
        # load, and the pipeline holds a path into it for as long as it runs.
        self._workspace = None

    # ── what is on disk ─────────────────────────────────────────────────────────

    def missing(self) -> tuple[str, ...]:
        """Every file this runtime needs, named the way the operator would."""
        if not os.path.isdir(os.path.join(RVC_HOME, "infer")):
            return ("rvc runtime",)
        root = self._settings.model_dir
        needed = [checkpoint_file(), os.path.join(hubert_dir(), "config.json")]
        index = index_file()
        if index:
            needed.append(index)
        return tuple(name for name in needed if not os.path.isfile(os.path.join(root, *name.split("/"))))

    def available(self) -> bool:
        return not self.missing()

    # ── loading ────────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Import upstream, build the pipeline, and make no sound.

        Silent on purpose: a warm-up that produced audio would be indistinguishable
        from a reply, and the point of loading is to move the cost off the first
        thing anybody says.
        """
        with self._lock:
            if self._vc is not None:
                return
            self._workspace = tempfile.TemporaryDirectory(prefix="rvc-model-")
            self._vc = _build(self._settings.model_dir, self._workspace.name)

    # ── conversion ─────────────────────────────────────────────────────────────

    def convert(self, audio: bytes, sample_rate: int | None = None) -> bytes:
        """Convert one synthesised reply and answer in the service's own format."""
        if self._vc is None:
            self.load()
        rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="rvc-") as workspace:
            source = os.path.join(workspace, "source.wav")
            with open(source, "wb") as handle:
                handle.write(audio)
            status, result = self._vc.vc_single(
                0,
                source,
                PITCH_SHIFT,
                f0_method(),
                self._index_path(),
                index_rate(),
                OUTPUT_RATE,
                1.0,
                PROTECT,
            )
        elapsed = time.perf_counter() - started
        duration = _duration(audio, rate)
        logger.info(
            "character voice: %.1f s of audio in %.1f s (%.0f%% of the rate it was written at)",
            duration,
            elapsed,
            100.0 * duration / elapsed if elapsed else 0.0,
        )
        if elapsed > SLOW_CONVERSION_SECONDS:
            logger.warning(
                "voice conversion took %.1f s for %.1f s of audio; the reply will arrive that late",
                elapsed,
                duration,
            )
        if not result or result[0] is None or result[1] is None:
            raise ConversionError(f"the character voice did not convert this reply: {status}")
        out_rate, samples = result
        return to_wav(samples, out_rate)

    def _index_path(self) -> str:
        index = index_file()
        if not index:
            return ""
        path = os.path.join(self._settings.model_dir, index)
        return path if os.path.isfile(path) else ""


def _prepare_checkpoint(path: str, workspace: str) -> tuple[str, str]:
    """Return a checkpoint whose version tag matches its own weights.

    Upstream keys the generator class off that tag and defaults anything it does not
    recognise to v1, so a v2 model published without a tag loads into the v1 class,
    runs, and produces audio that is wrong in a way no log line mentions. The width
    the text embedding accepts is not a matter of opinion, so the tag is rewritten to
    agree with it and the copy is loaded from. The operator's file is not touched:
    the model volume is mounted read-only, and a repaired copy is not something to
    leave lying next to the original.
    """
    import torch

    _check_checkpoint(path)
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    width = int(checkpoint["weight"]["enc_p.emb_phone.weight"].shape[1])
    wanted = "v1" if width == 256 else "v2"
    tagged = str(checkpoint.get("version") or "").lower()
    if tagged == wanted:
        return path, os.path.dirname(path)
    checkpoint["version"] = wanted
    repaired = os.path.join(workspace, os.path.basename(path))
    torch.save(checkpoint, repaired)
    logger.info(
        "checkpoint %s is tagged %s but takes %d content features; loading it as %s",
        os.path.basename(path),
        tagged or "nothing",
        width,
        wanted,
    )
    return repaired, workspace


def _build(model_dir: str, workspace: str):
    """Bring up the upstream pipeline, adapting two things it assumes about its host."""
    import sys
    from pathlib import Path

    if RVC_HOME not in sys.path:
        sys.path.insert(0, RVC_HOME)
    _check_upstream()
    import infer.audio as audio_module
    import infer.hubert as hubert_module
    from configs.config import Config
    from infer.vc.modules import VC

    # Upstream looks for the content model inside its own checkout. It is 181 MB and
    # belongs with the other models, in the volume the fetcher writes and the operator
    # can replace without rebuilding an image.
    hubert_module.HUBERT_MODEL_PATH = Path(model_dir) / hubert_dir()
    # Upstream decodes through ffmpeg even for WAV, which would mean a system codec and
    # a subprocess per reply. The service already holds the samples and soundfile is a
    # dependency for everything else, so the decode is done here instead.
    audio_module.load_audio = _load_audio
    loadable, weight_root = _prepare_checkpoint(os.path.join(model_dir, checkpoint_file()), workspace)
    # Upstream resolves models and its pitch model through directories in the
    # environment rather than through arguments, and reads two of them while loading.
    # Set here, or loading fails on a missing variable rather than on anything to do
    # with the model - which is a much harder thing to read a log about.
    os.environ["weight_root"] = weight_root
    os.environ["rmvpe_root"] = model_dir
    os.environ["index_root"] = model_dir
    os.environ["outside_index_root"] = model_dir
    controller = VC(Config())
    controller.get_vc(os.path.basename(loadable))
    return controller


def _check_upstream() -> None:
    try:
        import infer  # noqa: F401
    except ImportError as error:
        raise ConversionError(f"the character voice runtime is not installed at {RVC_HOME}") from error


def _check_checkpoint(path: str) -> None:
    """Read the two things a wrong guess would produce audio instead of an error."""
    import torch

    if not os.path.isfile(path):
        raise ConversionError(f"no voice conversion checkpoint at {path}")
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    weight = checkpoint.get("weight")
    if not isinstance(weight, dict) or "emb_pitch" not in weight:
        raise ConversionError(f"{path} is not a voice conversion checkpoint")
    # Upstream reads the version tag and, when it is missing or unrecognised, falls
    # back to the v1 generator. A v2 checkpoint then loads into the wrong class and
    # still produces audio, which is the one outcome a listener cannot mistake for
    # success - so the width the text embedding accepts decides it here instead.
    phone = weight.get("enc_p.emb_phone.weight")
    width = int(phone.shape[1]) if phone is not None else 0
    if "emb_g.weight" not in weight:
        # Upstream reads this unconditionally to size the speaker table, so a model
        # without one cannot be loaded at all. Said plainly rather than as a KeyError.
        raise ConversionError(
            f"{os.path.basename(path)} has no speaker table, which this runtime needs to load it"
        )
    if width not in {256, 768}:
        raise ConversionError(
            f"{os.path.basename(path)} takes {width} content features, which is neither v1 nor v2"
        )


def _load_audio(path, rate, force_mono: bool = True):
    """Upstream's audio loader, reading WAV without ffmpeg."""
    import librosa
    import numpy as np
    import soundfile as sf

    data, source_rate = sf.read(str(path), dtype="float32", always_2d=True)
    if force_mono:
        data = data.mean(axis=1)
    else:
        data = data.T
    if source_rate != rate:
        data = librosa.resample(data, orig_sr=source_rate, target_sr=rate)
    return np.ascontiguousarray(data, dtype="float32")


def _duration(audio: bytes, rate: int) -> float:
    with wave.open(_buffer(audio), "rb") as handle:
        frames = handle.getnframes()
        actual = handle.getframerate() or rate
    return frames / float(actual or rate)


def _buffer(payload: bytes):
    import io

    return io.BytesIO(payload)


__all__ = [
    "RVC_HOME",
    "ConversionError",
    "RvcRuntime",
    "checkpoint_file",
    "f0_method",
    "hubert_dir",
    "index_file",
    "index_rate",
]
