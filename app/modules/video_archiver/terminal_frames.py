import subprocess
import threading

from app.core.storage import LocalStorage, StorageInterface

CC_PALETTE = (
    ("0", 240, 240, 240),
    ("1", 242, 178, 51),
    ("2", 229, 127, 216),
    ("3", 153, 178, 242),
    ("4", 222, 222, 108),
    ("5", 127, 204, 25),
    ("6", 242, 178, 204),
    ("7", 76, 76, 76),
    ("8", 153, 153, 153),
    ("9", 76, 153, 178),
    ("a", 178, 102, 229),
    ("b", 51, 102, 204),
    ("c", 127, 102, 76),
    ("d", 87, 166, 78),
    ("e", 204, 76, 76),
    ("f", 17, 17, 17),
)

FRAME_MEDIA_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "nfp": "text/plain; charset=us-ascii",
}
MAX_IMAGE_PIXELS = 2_073_600
MAX_PALETTE_PIXELS = 262_144


def validate_frame_dimensions(width: int, height: int, frame_format: str) -> None:
    limit = MAX_PALETTE_PIXELS if frame_format in {"cc-palette", "nfp"} else MAX_IMAGE_PIXELS
    if width * height > limit:
        raise ValueError(f"Requested frame has too many pixels; maximum for {frame_format} is {limit}")


def quantize_terminal_frame(rgb: bytes, width: int, height: int) -> list[str]:
    """Map an RGB24 frame to CC:Tweaked's default palette."""
    if len(rgb) != width * height * 3:
        raise ValueError("FFmpeg returned an incomplete frame")

    rows: list[str] = []
    offset = 0
    for _ in range(height):
        row: list[str] = []
        for _ in range(width):
            red, green, blue = rgb[offset : offset + 3]
            offset += 3
            color = min(
                CC_PALETTE,
                key=lambda item: (red - item[1]) ** 2 + (green - item[2]) ** 2 + (blue - item[3]) ** 2,
            )
            row.append(color[0])
        rows.append("".join(row))
    return rows


def _video_filter(width: int, height: int, fit: str) -> str:
    if fit == "stretch":
        return f"scale={width}:{height}"
    if fit == "cover":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )


def _frame_command(
    video_input: str,
    timestamp: float,
    width: int,
    height: int,
    frame_format: str,
    fit: str,
    *,
    seekable: bool,
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if seekable:
        command.extend(("-ss", f"{timestamp:.3f}"))
    command.extend(("-i", video_input))
    if not seekable:
        command.extend(("-ss", f"{timestamp:.3f}"))
    command.extend(("-frames:v", "1", "-vf", _video_filter(width, height, fit)))

    if frame_format in {"cc-palette", "nfp"}:
        command.extend(("-pix_fmt", "rgb24", "-f", "rawvideo"))
    elif frame_format == "png":
        command.extend(("-c:v", "png", "-f", "image2pipe"))
    elif frame_format == "jpeg":
        command.extend(("-c:v", "mjpeg", "-q:v", "5", "-f", "image2pipe"))
    else:
        command.extend(("-c:v", "libwebp", "-quality", "75", "-f", "image2pipe"))
    command.append("pipe:1")
    return command


def _run_streamed(command: list[str], storage: StorageInterface, path: str) -> bytes:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    process_stdin = process.stdin
    process.stdin = None

    def feed_input() -> None:
        try:
            with storage.get_file_stream(path) as stream:
                while chunk := stream.read(65536):
                    process_stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass
        finally:
            process_stdin.close()

    feeder = threading.Thread(target=feed_input, daemon=True)
    feeder.start()
    try:
        stdout, _ = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    finally:
        feeder.join(timeout=1)
    if process.returncode != 0:
        raise RuntimeError("FFmpeg could not decode the requested frame")
    return stdout


def extract_video_frame(
    storage: StorageInterface,
    path: str,
    timestamp: float,
    width: int,
    height: int,
    frame_format: str,
    fit: str,
) -> bytes | list[str]:
    """Extract one frame in an API-selected image or terminal format."""
    validate_frame_dimensions(width, height, frame_format)
    if isinstance(storage, LocalStorage):
        command = _frame_command(
            str(storage._full_path(path)),
            timestamp,
            width,
            height,
            frame_format,
            fit,
            seekable=True,
        )
        result = subprocess.run(command, capture_output=True, timeout=20, check=False)
        if result.returncode != 0:
            raise RuntimeError("FFmpeg could not decode the requested frame")
        output = result.stdout
    else:
        command = _frame_command("pipe:0", timestamp, width, height, frame_format, fit, seekable=False)
        output = _run_streamed(command, storage, path)

    if frame_format in {"cc-palette", "nfp"}:
        return quantize_terminal_frame(output, width, height)
    return output
