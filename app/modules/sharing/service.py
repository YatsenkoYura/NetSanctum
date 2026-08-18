import hashlib
import hmac
from datetime import UTC, datetime

from app.modules.sharing.models import ShareLink

MAX_SESSION_SECONDS = 86400 * 7
MAX_SHARE_SESSIONS = 32

CREATE_SESSION_SCRIPT = """
local index_key = KEYS[1]
local session_key = KEYS[2]
local redis_time = redis.call("TIME")
local now = tonumber(redis_time[1])
local ttl = tonumber(ARGV[1])
local expires_at = now + ttl
local share_id = ARGV[2]
local session_id = ARGV[3]
local session_prefix = ARGV[4]
local max_sessions = tonumber(ARGV[5])

local expired = redis.call("ZRANGEBYSCORE", index_key, "-inf", now)
for _, expired_session_id in ipairs(expired) do
    redis.call("DEL", session_prefix .. expired_session_id)
end
redis.call("ZREMRANGEBYSCORE", index_key, "-inf", now)
local overflow = redis.call("ZCARD", index_key) - max_sessions + 1
if overflow > 0 then
    local evicted = redis.call("ZRANGE", index_key, 0, overflow - 1)
    for _, old_session_id in ipairs(evicted) do
        redis.call("DEL", session_prefix .. old_session_id)
        redis.call("ZREM", index_key, old_session_id)
    end
end

redis.call("SETEX", session_key, ttl, share_id)
redis.call("ZADD", index_key, expires_at, session_id)
local latest = redis.call("ZRANGE", index_key, -1, -1, "WITHSCORES")
redis.call("EXPIRE", index_key, math.max(1, math.ceil(tonumber(latest[2]) - now) + 1))
return 1
"""

RESERVE_PASSWORD_ATTEMPT_SCRIPT = """
local attempts = tonumber(redis.call("GET", KEYS[1]) or "0")
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

if attempts >= limit then
    local retry_after = redis.call("TTL", KEYS[1])
    if retry_after < 1 then
        retry_after = window
    end
    return {0, retry_after}
end

attempts = redis.call("INCR", KEYS[1])
local retry_after = redis.call("TTL", KEYS[1])
if attempts == 1 or retry_after < 1 then
    redis.call("EXPIRE", KEYS[1], window)
    retry_after = window
end
return {1, retry_after}
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str, secret_hash: str | None) -> bool:
    return bool(secret_hash) and hmac.compare_digest(hash_secret(secret), secret_hash)


def is_active(share: ShareLink, now: datetime | None = None) -> bool:
    now = now or utc_now()
    return share.status == "active" and (share.expires_at is None or as_utc(share.expires_at) > now)


def session_ttl(share: ShareLink, now: datetime | None = None) -> int:
    now = now or utc_now()
    if share.expires_at is None:
        return MAX_SESSION_SECONDS
    remaining = int((as_utc(share.expires_at) - now).total_seconds())
    return max(1, min(MAX_SESSION_SECONDS, remaining))
