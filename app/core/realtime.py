"""Process-safe realtime event delivery backed by Redis Pub/Sub."""

import asyncio
import json
import logging
import re
from collections import defaultdict
from typing import Any

import redis.asyncio as redis
from fastapi import WebSocket

from app.core.config import get_settings

logger = logging.getLogger(__name__)
CHANNEL_PATTERN = re.compile(r"^[a-z][a-z0-9:_-]{0,127}$")


class RealtimeHub:
    """Fan Redis events out to local WebSocket clients, one subscription per channel."""

    def __init__(self, redis_url: str, prefix: str = "netsanctum:realtime:") -> None:
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = prefix
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._listeners: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def validate_channel(channel: str) -> str:
        if not CHANNEL_PATTERN.fullmatch(channel):
            raise ValueError("Invalid realtime channel")
        return channel

    async def publish(self, channel: str, event: str, data: dict[str, Any] | None = None) -> None:
        channel = self.validate_channel(channel)
        if not event or len(event) > 64:
            raise ValueError("Invalid realtime event name")
        payload = json.dumps(
            {"event": event, "data": data or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self._redis.publish(f"{self._prefix}{channel}", payload)

    async def connect(self, channel: str, websocket: WebSocket) -> None:
        channel = self.validate_channel(channel)
        async with self._lock:
            self._connections[channel].add(websocket)
            if channel not in self._listeners:
                self._listeners[channel] = asyncio.create_task(
                    self._listen(channel), name=f"realtime:{channel}"
                )

    async def disconnect(self, channel: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(channel)
            if connections is None:
                return
            connections.discard(websocket)
            if connections:
                return
            self._connections.pop(channel, None)
            task = self._listeners.pop(channel, None)
            if task:
                task.cancel()

    async def _listen(self, channel: str) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(f"{self._prefix}{channel}")
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message:
                    await asyncio.sleep(0.05)
                    continue
                await self._broadcast(channel, message["data"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Realtime listener failed for channel %s", channel)
        finally:
            await pubsub.aclose()

    async def _broadcast(self, channel: str, payload: str) -> None:
        stale = []
        for websocket in tuple(self._connections.get(channel, ())):
            try:
                await websocket.send_text(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(channel, websocket)

    async def close(self) -> None:
        async with self._lock:
            tasks = tuple(self._listeners.values())
            self._listeners.clear()
            self._connections.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._redis.aclose()


realtime_hub = RealtimeHub(get_settings().REDIS_URL)
