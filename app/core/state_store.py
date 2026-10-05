"""The Redis that holds unlock sessions, and the refusal to snapshot it.

An unlock session holds a vault's data key sealed under the tab's token (see
`app.modules.vault.sealing._seal_session_record`): Redis alone cannot open it,
and the token lives only in the page's memory. A download handoff holds the
address of a video the owner has not finished saving, and the unlock throttle
holds the record of who has been guessing at a passphrase. All three live in
Redis, and the broker lives there too — which means the same `appendfsync` that
makes the queue survive a restart would also keep sealed sessions around.

This module is the separation: its own connection, its own instance, its own
`--save "" --appendonly no`. It also checks that the instance it was given really
has persistence off, because a setting that says "ephemeral" while the deployment
snapshots anyway is worse than no setting — it is a claim that is not true.

When `VAULT_STATE_REQUIRE_EPHEMERAL` is on, both persistence signals refuse:
`appendonly yes` and a non-empty `save` each raise. Managed Redis that does not
expose `CONFIG GET` fails closed unless the deployment explicitly accepts it
with `VAULT_STATE_ASSUME_EPHEMERAL` — and that escape hatch covers only
"could not be asked" (ACL, proxy, managed instance), never a refused
connection or a bad password.
"""

import logging
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def redact(url: str) -> str:
    """A URL safe to write down. The password is in it, and a connection string
    with the state store's credentials belongs in exactly one place."""
    # Everything inside the guard, `.port` included: it is a property that
    # parses lazily and raises on a bad port, which is exactly the kind of URL a
    # misconfiguration produces.
    try:
        parts = urlsplit(url)
        password = parts.password
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        user = parts.username or ""
        scheme = parts.scheme
    except ValueError:
        return "<unparseable url>"
    if not password:
        return url
    return urlunsplit((scheme, f"{user}:***@{host}", parts.path, parts.query, parts.fragment))


def state_redis_url() -> str:
    """Where ephemeral vault state lives. Falls back to the general Redis."""
    configured = (get_settings().VAULT_STATE_REDIS_URL or "").strip()
    return configured or get_settings().REDIS_URL


def _client_for(url: str) -> aioredis.Redis:
    return aioredis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)


def _classify_redis_error(error: Exception) -> str:
    """Connection/auth failures are never excused; only "could not be asked" is."""
    try:
        from redis.exceptions import AuthenticationError, ConnectionError as RedisConnectionError

        if isinstance(error, RedisConnectionError):
            return "connection"
        if isinstance(error, AuthenticationError):
            return "auth"
    except ImportError:
        pass
    name = type(error).__name__
    if "auth" in name.lower() or "connection" in str(error).lower():
        return "connection"
    return "unknown"


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
        # and nothing to change from here, so say what kind of failure it was
        # and let the caller decide. Connection/auth failures are reported as
        # such so the strict path can always refuse them.
        logger.warning("could not read the Redis configuration at %s: %s", redact(target), error)
        return {
            "reachable": False,
            "reason": _classify_redis_error(error),
            "error": str(error),
            "appendonly": None,
            "save": None,
            "url": redact(target),
        }
    finally:
        await client.aclose()
    appendonly = str((raw or {}).get("appendonly", "")).lower() in {"yes", "1", "true"}
    save = str((saves or {}).get("save", ""))
    return {
        "reachable": True,
        "appendonly": appendonly,
        "save": save,
        "url": redact(target),
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
        # Off by default, so a single-Redis deployment is untouched — but not
        # silently. The fallback below means that without VAULT_STATE_REDIS_URL this
        # is the *main* Redis, the one the broker writes its append log to, and
        # unlock sessions are data keys. Saying so once at startup is the difference
        # between a setting nobody knew about and a setting nobody has to.
        if report.get("reachable") and (report.get("appendonly") or report.get("save")):
            logger.warning(
                "The vault state store keeps unlock sessions — data keys — on disk "
                "(appendonly=%s, save=%r) because VAULT_STATE_REQUIRE_EPHEMERAL is off. Point "
                'VAULT_STATE_REDIS_URL at an instance started with `--save "" --appendonly no` and '
                "turn the flag on.",
                report.get("appendonly"),
                report.get("save"),
            )
        return report
    if not report.get("reachable"):
        # Asking is not the same as knowing. `CONFIG GET` can be denied by an ACL,
        # blocked by a proxy, or unavailable on a managed instance, and then
        # "unreachable" says nothing about whether the instance persists — which is
        # the only question this flag exists to ask. "No route to host" and "wrong
        # password" say something else entirely, and are always fatal: assuming an
        # instance ephemeral when it cannot even be reached confuses "Redis is
        # down" with "Redis is safe".
        if report.get("reason") in {"connection", "auth"}:
            raise RuntimeError(
                "The vault state Redis is unreachable or refused authentication "
                f"({report.get('error')}). Fix VAULT_STATE_REDIS_URL rather than assuming it."
            )
        if not settings.VAULT_STATE_ASSUME_EPHEMERAL:
            raise RuntimeError(
                "The vault state Redis could not be asked whether it persists "
                "(CONFIG GET is unavailable: an ACL, a proxy or a managed instance). "
                "Point VAULT_STATE_REDIS_URL at an instance this deployment can inspect, or set "
                "VAULT_STATE_ASSUME_EPHEMERAL=true to accept it without the answer."
            )
        logger.warning(
            "Assuming the vault state Redis is ephemeral because it could not be asked. "
            "If it writes to disk, unlock sessions — and therefore data keys — are on that disk."
        )
        return report
    if report.get("appendonly"):
        raise RuntimeError(
            "The vault state Redis has appendonly enabled, which would write unlock sessions "
            "— data keys — to its disk. Point VAULT_STATE_REDIS_URL at an instance started with "
            '`--save "" --appendonly no`, or unset VAULT_STATE_REQUIRE_EPHEMERAL to accept it.'
        )
    if report.get("save"):
        # An RDB snapshot is the same leak by a different route: it is not
        # append-only, it is not not-written-to-disk. Refusing on one and merely
        # logging the other made the refusal look like it covered both.
        raise RuntimeError(
            f"The vault state Redis takes RDB snapshots ({report.get('save')!r}), and a snapshot "
            "can capture an unlock session — a data key — the same way the append log does. Run it "
            'with `--save ""`, or unset VAULT_STATE_REQUIRE_EPHEMERAL to accept it.'
        )
    return report
