def build_audio_command(
    video_input: str,
    timestamp: float,
    audio_format: str,
    *,
    seekable: bool,
) -> list[str]:
    """Build an FFmpeg command for browser MP3 or CC:Tweaked DFPWM audio."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if seekable and timestamp:
        command.extend(("-ss", f"{timestamp:.3f}"))
    command.extend(("-i", video_input))
    if not seekable and timestamp:
        command.extend(("-ss", f"{timestamp:.3f}"))
    command.append("-vn")
    if audio_format == "dfpwm":
        command.extend(("-ac", "1", "-ar", "48000", "-c:a", "dfpwm", "-f", "dfpwm"))
    else:
        command.extend(("-c:a", "libmp3lame", "-f", "mp3"))
    command.append("pipe:1")
    return command
