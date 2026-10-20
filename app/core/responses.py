import asyncio
import logging
import mimetypes
import urllib.parse
from collections.abc import AsyncGenerator, Iterator

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.storage import get_storage

logger = logging.getLogger(__name__)


async def bytes_chunk_generator(data: bytes, chunk_size: int = 262144) -> AsyncGenerator[bytes, None]:
    """Yield bytes in fixed chunk sizes to avoid blocking the event loop."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


async def _stream_blocking_pieces(pieces: Iterator[bytes]) -> AsyncGenerator[bytes, None]:
    """Drain a blocking iterator one piece at a time without stalling the loop.

    Reading and decrypting a chunk of the chunked envelope happens in a worker
    thread. Closing this generator — client disconnect, cancelled response —
    closes the underlying storage stream at its `finally`, so the file handle
    does not outlive the body that borrowed it.
    """
    sentinel = object()
    try:
        while True:
            piece = await asyncio.to_thread(next, pieces, sentinel)
            if piece is sentinel:
                break
            yield piece
    finally:
        pieces.close()


async def _open_encrypted_payload(storage, file_path: str) -> bytes | None:
    """Open an encrypted object before any response header is committed.

    Returns the decrypted bytes of a single-blob envelope, or `None` for a
    chunked one. A single-blob envelope authenticates its whole ciphertext
    under one tag, so it cannot be ranged: it is decrypted here, in full, and
    the payload is handed to the body generator to serve from memory. A
    chunked envelope only needs proof that the key and the path binding still
    open it — one decrypted chunk is that proof — and streams the rest from
    disk afterwards.

    The distinction matters because a failure after the headers would reach
    the client as a body that stops halfway, which HTTP reports as an opaque
    decode error rather than a status the caller can act on. A missing legacy
    key, an envelope stored under a name it is not bound to, a truncated
    header: all of them must fail here, as an ordinary HTTP error.
    """
    try:
        if await asyncio.to_thread(storage.is_seekable_encrypted, file_path):
            probe = storage.read_seekable_range(file_path, 0, 1)
            try:
                await asyncio.to_thread(next, probe, None)
            finally:
                probe.close()
            return None
        return await asyncio.to_thread(storage.get_file_decrypted, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except ValueError as exc:
        # The path and the reason stay in the server log: the client cannot
        # act on either, and the path names storage layout it never asked for.
        logger.warning("Cannot open encrypted object '%s': %s", file_path, exc)
        raise HTTPException(status_code=500, detail="Encrypted file cannot be opened") from exc


async def storage_chunk_generator(
    storage, file_path: str, chunk_size: int = 262144, *, plaintext: bytes | None = None
) -> AsyncGenerator[bytes, None]:
    """Yield file content chunks, decrypting on-the-fly if necessary.

    `plaintext` is a single-blob envelope's payload, already opened by
    `_open_encrypted_payload` before the response headers were sent — the body
    must not decrypt it a second time. Without a payload a chunked envelope
    streams straight from disk, never landing in memory; a single-blob
    envelope reached without a preflight is decrypted whole here, the only way
    its format allows.
    """
    if plaintext is not None:
        async for chunk in bytes_chunk_generator(plaintext, chunk_size):
            yield chunk
        return
    if file_path.endswith(".enc"):
        if await asyncio.to_thread(storage.is_seekable_encrypted, file_path):
            total = await asyncio.to_thread(storage.get_encrypted_plaintext_size, file_path)
            async for piece in _stream_blocking_pieces(storage.read_seekable_range(file_path, 0, total)):
                yield piece
            return
        data = await asyncio.to_thread(storage.get_file_decrypted, file_path)
        async for chunk in bytes_chunk_generator(data, chunk_size):
            yield chunk
        return

    def read_chunk(f):
        return f.read(chunk_size)

    with storage.get_file_stream(file_path) as f:
        while True:
            chunk = await asyncio.to_thread(read_chunk, f)
            if not chunk:
                break
            yield chunk


async def range_storage_generator(
    storage,
    file_path: str,
    start: int,
    end: int,
    chunk_size: int = 262144,
    *,
    plaintext: bytes | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield a specific byte range of a file from storage.

    A chunked envelope decrypts only the chunks the range touches, so a seek
    into a large video costs the chunks under the cursor rather than a
    whole-object decrypt; a single-blob envelope was decrypted whole by the
    preflight and is sliced from memory.
    """
    if plaintext is not None:
        async for chunk in bytes_chunk_generator(plaintext[start : end + 1], chunk_size):
            yield chunk
        return
    if file_path.endswith(".enc"):
        if await asyncio.to_thread(storage.is_seekable_encrypted, file_path):
            length = end - start + 1
            async for piece in _stream_blocking_pieces(storage.read_seekable_range(file_path, start, length)):
                yield piece
            return
        data = await asyncio.to_thread(storage.get_file_decrypted, file_path)
        async for chunk in bytes_chunk_generator(data[start : end + 1], chunk_size):
            yield chunk
        return

    def read_range_chunk(f, remaining):
        return f.read(min(chunk_size, remaining))

    with storage.get_file_stream(file_path) as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = await asyncio.to_thread(read_range_chunk, f, remaining)
            if not chunk:
                break
            yield chunk
            remaining -= len(chunk)


def serve_bytes_chunked(data: bytes, media_type: str, filename: str | None = None) -> StreamingResponse:
    """Serve raw in-memory bytes as an asynchronous chunked stream."""
    headers = {}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{urllib.parse.quote(filename)}"'
    return StreamingResponse(bytes_chunk_generator(data), media_type=media_type, headers=headers)


async def serve_storage_file_chunked(file_path: str, media_type: str | None = None) -> StreamingResponse:
    """Serve a storage file as an asynchronous chunked stream.

    An encrypted object is opened before the response starts — see
    `_open_encrypted_payload` — so a file whose key is gone answers with an
    HTTP status instead of a body that stops mid-flight.
    """
    storage = get_storage()
    if not storage.file_exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not media_type:
        media_type, _ = mimetypes.guess_type(file_path)
        media_type = media_type or "application/octet-stream"

    plaintext = None
    if file_path.endswith(".enc"):
        plaintext = await _open_encrypted_payload(storage, file_path)

    return StreamingResponse(
        storage_chunk_generator(storage, file_path, plaintext=plaintext),
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


async def serve_media_stream(request: Request, file_path: str, media_type: str | None = None) -> Response:
    """Serve a media file supporting HTTP 206 Range requests.

    As in `serve_storage_file_chunked`, an encrypted object is opened before
    any header is sent: a request for a file no key opens anymore becomes a
    readable status, not a media element that fails with a decode error. The
    chunked envelope streams per range, so seeking a large video decrypts the
    chunks under the cursor instead of the whole file.
    """
    storage = get_storage()
    if not storage.file_exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not media_type:
        media_type, _ = mimetypes.guess_type(file_path)
        media_type = media_type or "video/mp4"

    legacy_plaintext: bytes | None = None
    if file_path.endswith(".enc"):
        legacy_plaintext = await _open_encrypted_payload(storage, file_path)
        file_size = (
            len(legacy_plaintext)
            if legacy_plaintext is not None
            else await asyncio.to_thread(storage.get_encrypted_plaintext_size, file_path)
        )
    else:
        file_size = storage.get_file_size(file_path)

    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            ranges = range_header.replace("bytes=", "").split("-")
            start = int(ranges[0]) if ranges[0] else 0
            end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else file_size - 1
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range Header")

        if start >= file_size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

        if end >= file_size:
            end = file_size - 1

        content_length = end - start + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Access-Control-Allow-Origin": "*",
        }

        return StreamingResponse(
            range_storage_generator(storage, file_path, start, end, plaintext=legacy_plaintext),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Access-Control-Allow-Origin": "*",
    }
    return StreamingResponse(
        storage_chunk_generator(storage, file_path, plaintext=legacy_plaintext),
        media_type=media_type,
        headers=headers,
    )
