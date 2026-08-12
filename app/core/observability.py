"""Shared Redis-backed logging for web and worker processes."""

import json
import logging
import logging.handlers
import os
import queue
import re
import time
from datetime import UTC, datetime

import redis

from app.core.config import get_settings

_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|token|password)(\s*[=:]\s*)[^\s,;&]+"),
    re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"),
)
_LISTENERS: list[logging.handlers.QueueListener] = []


def redact_log_message(message: str) -> str:
    redacted = message
    redacted = _TOKEN_PATTERNS[0].sub(r"\1[REDACTED]", redacted)
    redacted = _TOKEN_PATTERNS[1].sub(r"\1\2[REDACTED]", redacted)
    redacted = _TOKEN_PATTERNS[2].sub("[REDACTED_JWT]", redacted)
    return redacted


class RedisLogHandler(logging.Handler):
    """Best-effort bounded log sink used only from a QueueListener thread."""

    def __init__(self, role: str) -> None:
        super().__init__(logging.INFO)
        settings = get_settings()
        self.role = role
        self.key = settings.OBSERVABILITY_LOG_KEY
        self.limit = settings.OBSERVABILITY_LOG_LIMIT
        self.ttl = settings.OBSERVABILITY_LOG_TTL_SECONDS
        self.client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        self.disabled_until = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("redis"):
            return
        if time.monotonic() < self.disabled_until:
            return
        try:
            message = redact_log_message(self.format(record))
            payload = json.dumps(
                {
                    "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                    "role": self.role,
                    "process": record.process,
                    "process_name": record.processName,
                },
                ensure_ascii=False,
            )
            pipeline = self.client.pipeline(transaction=False)
            pipeline.lpush(self.key, payload)
            pipeline.ltrim(self.key, 0, self.limit - 1)
            pipeline.expire(self.key, self.ttl)
            pipeline.execute()
        except Exception:
            self.disabled_until = time.monotonic() + 5.0


def configure_observability(role: str, target_logger: logging.Logger | None = None) -> None:
    logger = target_logger or logging.getLogger()
    marker = "netsanctum_queue_log_handler"
    if any(getattr(handler, marker, False) for handler in logger.handlers):
        return
    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(log_queue)
    setattr(queue_handler, marker, True)
    redis_handler = RedisLogHandler(role)
    redis_handler.setFormatter(logging.Formatter("%(message)s"))
    listener = logging.handlers.QueueListener(log_queue, redis_handler, respect_handler_level=True)
    listener.start()
    _LISTENERS.append(listener)
    logger.addHandler(queue_handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)


def process_role(default: str) -> str:
    return os.getenv("NETSANCTUM_PROCESS_ROLE", default)
