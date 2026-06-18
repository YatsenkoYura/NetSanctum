import mimetypes
import os
import urllib.parse
from collections.abc import AsyncGenerator

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.storage import get_storage


async def bytes_chunk_generator(data: bytes, chunk_size: int = 262144) -> AsyncGenerator[bytes, None]:
    """Yield bytes in fixed chunk sizes to avoid blocking the event loop."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


async def storage_chunk_generator(
    storage, file_path: str, chunk_size: int = 262144
) -> AsyncGenerator[bytes, None]:
    """Yield file content chunks from storage, decrypting on-the-fly if necessary."""
    import asyncio

    is_enc = file_path.endswith(".enc")
    if is_enc:
        data = await asyncio.to_thread(storage.get_file_decrypted, file_path)
        async for chunk in bytes_chunk_generator(data, chunk_size):
            yield chunk
    else:

        def read_chunk(f):
            return f.read(chunk_size)

        with storage.get_file_stream(file_path) as f:
            while True:
                chunk = await asyncio.to_thread(read_chunk, f)
                if not chunk:
                    break
                yield chunk


async def range_storage_generator(
    storage, file_path: str, start: int, end: int, chunk_size: int = 262144
) -> AsyncGenerator[bytes, None]:
    """Yield a specific byte range of a file from storage."""
    import asyncio

    is_enc = file_path.endswith(".enc")
    if is_enc:
        data = await asyncio.to_thread(storage.get_file_decrypted, file_path)
        sliced_data = data[start : end + 1]
        async for chunk in bytes_chunk_generator(sliced_data, chunk_size):
            yield chunk
    else:

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


def serve_storage_file_chunked(file_path: str, media_type: str | None = None) -> StreamingResponse:
    """Serve a storage file as an asynchronous chunked stream."""
    storage = get_storage()
    if not storage.file_exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not media_type:
        media_type, _ = mimetypes.guess_type(file_path)
        media_type = media_type or "application/octet-stream"

    return StreamingResponse(storage_chunk_generator(storage, file_path), media_type=media_type)


def serve_media_stream(request: Request, file_path: str, media_type: str | None = None) -> Response:
    """Serve a media file supporting HTTP 206 Range requests dynamically and asynchronously."""
    storage = get_storage()
    if not storage.file_exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not media_type:
        media_type, _ = mimetypes.guess_type(file_path)
        media_type = media_type or "video/mp4"

    # Resolve file size
    is_enc = file_path.endswith(".enc")
    if hasattr(storage, "_full_path") and not is_enc:
        file_size = os.path.getsize(storage._full_path(file_path))
    else:
        with storage.get_file_stream(file_path) as f:
            f.seek(0, 2)
            file_size = f.tell()

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
            range_storage_generator(storage, file_path, start, end),
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
        storage_chunk_generator(storage, file_path), media_type=media_type, headers=headers
    )
