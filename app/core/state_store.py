"""The Redis that holds unlock sessions, and the refusal to snapshot it.

An unlock session holds a vault's data key. A download handoff holds the address
of a video the owner has not finished saving, and the unlock throttle holds the
record of who has been guessing at a passphrase. All three live in Redis, and the
broker lives there too — which means the same `appendfsync` that makes the queue
survive a restart also makes a data key survive one.

This module is the separation: its own connection, its own instance, its own
`--save "" --appendonly no`. It also checks that the instance it was given really
has persistence off, because a setting that says "ephemeral" while the deployment
snapshots anyway is worse than no setting — it is a claim that is not true.

The checks are strictest where they can be. `appendonly yes` is a refusal. A
non-empty `save` is a warning, not a stop: snapshotting is the less harmful of
the two, it is what a default Redis does, and an operator who wants it should not
have to learn the vocabulary to turn it off. Managed Redis that does not expose
`CONFIG GET` is trusted, since there is no way to ask and no way to fix it from
here either.
"""

import logging

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def state_redis_url() -> str:
    """Where ephemeral vault state lives. Falls back to the general Redis."""
    configured = (get_settings().VAULT_STATE_REDIS_URL or "").strip()
    return configured or get_settings().REDIS_URL


def _client_for(url: str) -> aioredis.Redis:
    return aioredis.Redis.from_url(url, decode_responses=True)


async def audit_state_redis(url: str | None = None) -> dict[str, object]:
    """Ask the instance whether it persists, and say what it found.

    Returns the raw answers so a caller can log them or assert on them; raises
    nothing on its own, because whether a persistence setting is fatal is the
    deployment's decision (`VAULT_STATE_REQUIRE_EPHEMERAL`).
    """
    target = url or state_redis_url()
    client = _client_for(target)
    try:
        raw = await client.config_get("appendonly")
        saves = await client.config_get("save")
    except Exception as error:
        # Managed Redis, a proxy, or a network hiccup: there is nothing to ask
        # and nothing to change from here, so say so and let the operator decide.
        logger.warning("could not read the Redis configuration at %s: %s", target, error)
        return {"reachable": False, "error": str(error), "appendonly": None, "save": None}
    finally:
        await client.aclose()
    appendonly = str((raw or {}).get("appendonly", "")).lower() in {"yes", "1", "true"}
    save = str((saves or {}).get("save", ""))
    return {
        "reachable": True,
        "appendonly": appendonly,
        "save": save,
        "url": target,
    }


async def require_ephemeral_state_store() -> dict[str, object]:
    """Refuse a state store that writes this state to disk.

    Called from the application's startup path rather than at import: a module
    that cannot reach Redis at import time cannot be imported by a CLI, a
    migration, or a test, and a check that only runs when the network happens to
    be up is not a check.
    """
    settings = get_settings()
    report = await audit_state_redis()
    if not settings.VAULT_STATE_REQUIRE_EPHEMERAL:
        return report
    if not report.get("reachable"):
        # Unreachable is not persistence. Refusing here would turn a network
        # problem into a refusal to start, which is a different failure.
        return report
    if report.get("appendonly"):
        raise RuntimeError(
            "The vault state Redis has appendonly enabled, which would write unlock sessions "
            "— data keys — to its disk. Point VAULT_STATE_REDIS_URL at an instance started with "
            '`--save "" --appendonly no`, or unset VAULT_STATE_REQUIRE_EPHEMERAL to accept it.'
        )
    if report.get("save"):
        logger.warning(
            "The vault state Redis takes RDB snapshots (%r). It is not append-only, so a snapshot "
            'can still capture a session; run it with `--save ""`.',
            report.get("save"),
        )
    return report
