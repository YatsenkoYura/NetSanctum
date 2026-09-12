import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

MAX_HEADER_BYTES = 64 * 1024
ALLOWED_HOSTS = frozenset(
    host.strip().lower().rstrip(".")
    for host in os.getenv("BROWSER_EGRESS_HOSTS", "").split(",")
    if host.strip()
)


def _host_allowed(host: str) -> bool:
    return bool(host) and any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS)


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return []
    resolved = list(dict.fromkeys(str(address[4][0]) for address in addresses))
    return (
        resolved if resolved and all(ipaddress.ip_address(address).is_global for address in resolved) else []
    )


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _reject(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))
    await writer.drain()
    writer.close()


async def handle_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    upstream_writer = None
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if len(request_line) > 8192:
            await _reject(writer, "431 Request Header Fields Too Large")
            return
        parts = request_line.decode("ascii", errors="replace").strip().split()
        if len(parts) != 3 or parts[0] != "CONNECT":
            await _reject(writer, "405 Method Not Allowed")
            return

        total = len(request_line)
        while header := await asyncio.wait_for(reader.readline(), timeout=10):
            total += len(header)
            if total > MAX_HEADER_BYTES:
                await _reject(writer, "431 Request Header Fields Too Large")
                return
            if header in {b"\r\n", b"\n"}:
                break

        parsed = urlsplit(f"//{parts[1]}")
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or 443
        if port != 443 or not _host_allowed(host):
            await _reject(writer, "403 Forbidden")
            return
        addresses = await asyncio.to_thread(_public_addresses, host, port)
        if not addresses:
            await _reject(writer, "403 Forbidden")
            return

        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(addresses[0], port),
            timeout=10,
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
        )
    except (OSError, ValueError, TimeoutError):
        if not writer.is_closing():
            await _reject(writer, "502 Bad Gateway")
    finally:
        if upstream_writer and not upstream_writer.is_closing():
            upstream_writer.close()


async def main() -> None:
    if not ALLOWED_HOSTS:
        raise RuntimeError("BROWSER_EGRESS_HOSTS must not be empty")
    server = await asyncio.start_server(handle_proxy, "0.0.0.0", 8888)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
