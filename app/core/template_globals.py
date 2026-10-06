"""Values a module offers the templates, filled in as modules load.

A template may be rendered by somebody other than the route that "owns" it — the
Vault dashboard is also rendered by the sharing module as a read-only shared
page — so anything a template needs from a module cannot be passed through the
route's context: it would simply not exist on the other path. This module is the
way out, and it is a dict on purpose.

A module writes into `DEFERRED_GLOBALS` when it is imported:

    from app.core.template_globals import DEFERRED_GLOBALS
    DEFERRED_GLOBALS["vault_views"] = view_map()

and the template reads it as an ordinary context variable. The engine merges the
dict into every render through a context processor, so the value is whatever the
module published *at render time* — after discovery has finished importing
modules, which is exactly when it cannot be known.

It is a separate module from `app.core.templates` on purpose: that one imports
the module registry, and a module importing it at import time would deadlock the
discovery that is importing it. This file imports nothing.
"""

from typing import Any

# Populated by modules, read by `app.core.templates` at render time. Never read
# at import time — anything read here would freeze the value before discovery
# had a chance to fill it.
DEFERRED_GLOBALS: dict[str, Any] = {}


def publish(name: str, value: Any) -> None:
    """Offer a value to every template render.

    Overwrites silently: two modules publishing the same name is a conflict the
    owner resolves by renaming, and refusing at import time would take the whole
    application down over a template variable.
    """
    DEFERRED_GLOBALS[name] = value
