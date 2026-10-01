"""Speech helpers: chunking replies for streaming TTS and voice payload bounds."""

import re

SPEECH_CHUNK_LIMIT = 300
VOICE_AUDIO_LIMIT = 4 * 1024 * 1024
VOICE_AUDIO_TYPES = {"audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"}


def split_speech(text: str) -> list[str]:
    """Split reply text into speakable sentence chunks for streaming TTS.

    Each sentence becomes its own chunk so playback starts early; fragments
    shorter than 40 characters merge forward so the provider never gets
    one-word requests; oversized sentences hard-cut at the chunk limit.
    """
    sentences = [
        fragment.strip() for fragment in re.split(r"(?<=[.!?…\n])\s+", text.strip()) if fragment.strip()
    ]
    chunks: list[str] = []
    pending = ""
    for sentence in sentences:
        candidate = f"{pending} {sentence}".strip() if pending else sentence
        if len(candidate) < 40:
            pending = candidate
            continue
        if len(candidate) <= SPEECH_CHUNK_LIMIT:
            chunks.append(candidate)
            pending = ""
            continue
        if pending:
            chunks.append(pending)
            pending = ""
        while len(sentence) > SPEECH_CHUNK_LIMIT:
            chunks.append(sentence[:SPEECH_CHUNK_LIMIT])
            sentence = sentence[SPEECH_CHUNK_LIMIT:]
        if sentence:
            chunks.append(sentence)
    if pending:
        chunks.append(pending)
    return chunks


__all__ = ["SPEECH_CHUNK_LIMIT", "VOICE_AUDIO_LIMIT", "VOICE_AUDIO_TYPES", "split_speech"]
