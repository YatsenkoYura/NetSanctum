"""Recognition, by adapting whisper.cpp to the shape the runtime already speaks.

The runtime posts OpenAI-shaped multipart to `/v1/audio/transcriptions`. The
whisper.cpp server exposes `/inference` instead, and the two differ only in the
path and an optional field, so the adapter is thin. Keeping the OpenAI shape on
this side of the boundary means the runtime does not grow a second code path for a
different speech engine.
"""

from __future__ import annotations

import os

import httpx

INFERENCE_PATH = "/inference"
TRANSCRIBE_TIMEOUT_SECONDS = 120.0
# The runtime bounds uploads already; this is the second gate, not the only one.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class TranscriptionError(RuntimeError):
    """The recogniser could not produce text."""


class Transcriber:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._stt_url = settings.stt_url.rstrip("/")

    def models_present(self) -> bool:
        """Whether the configured model file is actually on disk.

        A missing model has to be visible here rather than as a failed request
        later: the service starts before anything asks it to recognise anything.
        """
        return os.path.isfile(os.path.join(self._settings.model_dir, self._settings.stt_model_file))

    def english_only_model(self) -> bool:
        """whisper.cpp ships an English-only variant next to the multilingual one.

        Both are the same container and both pass a format check, so nothing else
        would notice the swap - but the English-only model is markedly worse on
        Russian, which is the language this assistant is used in.
        """
        name = self._settings.stt_model_file.casefold()
        return ".en." in name or name.endswith(".en.bin") or name.endswith(".en")

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "utterance.webm",
        content_type: str = "audio/webm",
        language: str | None = None,
    ) -> str:
        if not audio:
            raise TranscriptionError("no audio was sent")
        if len(audio) > MAX_AUDIO_BYTES:
            raise TranscriptionError("the recording is too large to transcribe")
        files = {"file": (filename, audio, content_type)}
        data = {"response_format": "json", "temperature": "0.0"}
        # Empty means "decide from the audio", which is what a Russian or English
        # utterance needs; pinning a language here would silently mislabel one of them.
        if language:
            data["language"] = language
        try:
            async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT_SECONDS) as client:
                response = await client.post(f"{self._stt_url}{INFERENCE_PATH}", files=files, data=data)
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"the recogniser is unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            detail = response.text[:200].replace("\n", " ")
            raise TranscriptionError(f"the recogniser returned {response.status_code}: {detail}")
        return _text_from(response.json())


def _text_from(payload: object) -> str:
    """Pull the transcript out of whichever shape the recogniser answered with."""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        raise TranscriptionError("the recogniser answered with something unexpected")
    text = payload.get("text")
    if isinstance(text, str):
        return text.strip()
    # With --output-json-full the segments are nested rather than flattened.
    segments = payload.get("transcription")
    if isinstance(segments, list):
        parts = [
            str(item.get("text", "")).strip()
            for item in segments
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        if parts:
            return " ".join(parts)
    raise TranscriptionError("the recogniser returned no text")


__all__ = ["INFERENCE_PATH", "Transcriber", "TranscriptionError"]
