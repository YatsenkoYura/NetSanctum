import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path

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
MAX_CACHED_MEDIA_BYTES = 512 * 1024 * 1024
MAX_CACHE_BYTES = 1024 * 1024 * 1024
_CACHE_LOCK = threading.Lock()
_CACHE_DIR = Path(tempfile.gettempdir()) / "netsanctum-computercraft"


def validate_frame_dimensions(width: int, height: int, frame_format: str) -> None:
    limit = MAX_PALETTE_PIXELS if frame_format in {"cc-palette", "nfp"} else MAX_IMAGE_PIXELS
    if width * height > limit:
        raise ValueError(f"Requested frame has too many pixels; maximum for {frame_format} is {limit}")


def quantize_terminal_frame(rgb: bytes, width: int, height: int) -> list[str]:
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


def _media_filter(width: int, height: int, fit: str) -> str:
    if fit == "stretch":
        return f"scale={width}:{height}"
    if fit == "cover":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )


def _frame_command(
    media_input: str,
    timestamp: float,
    width: int,
    height: int,
    frame_format: str,
    fit: str,
    *,
    seekable: bool,
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if seekable and timestamp:
        command.extend(("-ss", f"{timestamp:.3f}"))
    command.extend(("-i", media_input))
    if not seekable and timestamp:
        command.extend(("-ss", f"{timestamp:.3f}"))
    command.extend(("-frames:v", "1", "-vf", _media_filter(width, height, fit)))
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


def storage_stream(storage: StorageInterface, path: str):
    if path.endswith(".enc"):
        return storage.get_file_stream_decrypted(path)
    return storage.get_file_stream(path)


def materialize_media(storage: StorageInterface, path: str) -> Path:
    """Return a seekable media file, caching remote/decrypted objects in protected temporary storage."""
    if isinstance(storage, LocalStorage) and not path.endswith(".enc"):
        return storage._full_path(path)

    size = (
        storage.get_encrypted_plaintext_size(path) if path.endswith(".enc") else storage.get_file_size(path)
    )
    if size > MAX_CACHED_MEDIA_BYTES:
        raise ValueError(
            f"Media is too large for the ComputerCraft cache ({size} > {MAX_CACHED_MEDIA_BYTES} bytes)"
        )
    cache_key = hashlib.sha256(f"{type(storage).__name__}:{path}:{size}".encode()).hexdigest()
    suffix = Path(path.removesuffix(".enc")).suffix
    target = _CACHE_DIR / f"{cache_key}{suffix}"
    with _CACHE_LOCK:
        _CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == size:
            target.touch()
            return target

        cached = sorted(
            (item for item in _CACHE_DIR.iterdir() if item.is_file() and not item.name.endswith(".tmp")),
            key=lambda item: item.stat().st_mtime,
        )
        total = sum(item.stat().st_size for item in cached)
        for item in cached:
            if total + size <= MAX_CACHE_BYTES:
                break
            item_size = item.stat().st_size
            item.unlink(missing_ok=True)
            total -= item_size

        temporary = _CACHE_DIR / f"{cache_key}.{os.getpid()}.tmp"
        try:
            with storage_stream(storage, path) as source, temporary.open("xb") as output:
                temporary.chmod(0o600)
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            if temporary.stat().st_size != size:
                raise ValueError("Cached media size does not match storage metadata")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def extract_media_frame(
    storage: StorageInterface,
    path: str,
    timestamp: float,
    width: int,
    height: int,
    frame_format: str,
    fit: str,
) -> bytes | list[str]:
    validate_frame_dimensions(width, height, frame_format)
    media_path = materialize_media(storage, path)
    command = _frame_command(str(media_path), timestamp, width, height, frame_format, fit, seekable=True)
    result = subprocess.run(command, capture_output=True, timeout=20, check=False)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg could not decode the requested media frame")
    output = result.stdout
    if frame_format in {"cc-palette", "nfp"}:
        return quantize_terminal_frame(output, width, height)
    return output


def probe_media_duration(storage: StorageInterface, path: str) -> float:
    media_path = materialize_media(storage, path)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return 0
    try:
        return max(0, float(result.stdout.strip()))
    except ValueError:
        return 0


def has_audio_stream(storage: StorageInterface, path: str) -> bool:
    media_path = materialize_media(storage, path)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())
