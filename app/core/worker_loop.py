"""One event loop per worker process, reused for every task that needs async.

The async engine's pool hands out connections bound to the event loop that
opened them. A Celery task that calls `asyncio.run` gets a brand-new loop on
every pass, so the second pass receives a connection from a loop that no longer
exists and fails with "got Future attached to a different loop" — or, worse,
succeeds while quietly doing nothing.

Worker code therefore runs async work on a single long-lived loop per process.
Tasks whose entire body is synchronous (see the planner sweep) should still use
the synchronous engine and client; this helper is for work that is genuinely
async, such as calling integrations.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None


def worker_loop() -> asyncio.AbstractEventLoop:
    """The process-wide loop. Created on first use and never closed."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_async(coro):
    """Run a coroutine on the worker loop, whatever loop the caller is on."""
    loop = worker_loop()
    previous = None
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        previous = None
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Leave the caller's loop in place: Celery may run another task right after.
        try:
            asyncio.set_event_loop(previous)
        except Exception:
            logger.debug("could not restore the previous event loop", exc_info=True)


__all__ = ["run_async", "worker_loop"]
