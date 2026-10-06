"""What kind of card this is, in one place.

A Vault card's `node_type` says which view opens when you click it and nothing
else. The column is a free-form string on purpose — adding a card type is not a
migration — but the *vocabulary* was spread across a comment in the schema, a
`Literal` on the capture endpoint, a branch in the capture service and a switch
in the dashboard, which is four places to forget when the fifth arrives.

This module is the single declaration of that vocabulary, and it deliberately
holds no behaviour per type: the sealed-media rules, the packaging rules and
the view code already live where they can be read against the thing they
protect. What belongs here is only what a card *is*.

Two rules the values must keep, because both are load-bearing rather than
stylistic:

* **A type is structural, never sealed.** `node_type` is not in `SEALED_FIELDS`
  so that a locked vault can still lay out its grid. Anything that moves a type
  behind the sealed boundary makes the grid unable to draw itself while locked.
* **A type may not name its source.** The column is readable by anyone holding
  the database, sealed vault or not, so a value like `youtube_comment` publishes
  the owner's browsing through a property that looks like layout metadata.
  Provenance belongs in the sealed payload or behind `public_title`.
"""

from dataclasses import dataclass
from enum import StrEnum


class NodeType(StrEnum):
    """The card shapes the dashboard knows how to open."""

    NOTE = "note"
    BOOKMARK = "bookmark"
    IMAGE = "image"
    VIDEO = "video"
    TABLE = "table"
    WHITEBOARD = "whiteboard"
    FOLDER = "folder"


# The view a card of each type opens into. These are the `openXView` functions in
# the dashboard: the one thing a type decides, and the only place it is decided.
VIEWS: dict[str, str] = {
    NodeType.NOTE: "editor",
    NodeType.BOOKMARK: "editor",
    NodeType.FOLDER: "editor",
    NodeType.IMAGE: "media",
    NodeType.VIDEO: "media",
    NodeType.TABLE: "sheet",
    NodeType.WHITEBOARD: "whiteboard",
}

# What a type the dashboard has never heard of should open as. A row written by
# a newer build, or by a local card type, has to render as *something* — one
# unknown tile must not dead-end the whole grid. The editor reads any card's
# title and body, so it is the only view with no required shape of its own.
FALLBACK_VIEW = "editor"

# Types whose card owns a file in Vault storage. These carry the `media_*`
# columns, and the endpoints that refuse a blind or locked file are written
# against this set — see the blind-media notes in `tasks.py`.
MEDIA_TYPES: frozenset[str] = frozenset({NodeType.IMAGE, NodeType.VIDEO})


def view_for(node_type: str | NodeType | None) -> str:
    """The view a card of this type opens into.

    Takes the plain string as well as the enum, because rows come back from the
    database as strings and a card type written by another build is not in this
    vocabulary at all.
    """
    if node_type is None:
        return FALLBACK_VIEW
    return VIEWS.get(str(node_type), FALLBACK_VIEW)


def is_media(node_type: str | NodeType | None) -> bool:
    """Whether this card type owns a stored file."""
    return str(node_type) in MEDIA_TYPES


@dataclass(frozen=True)
class CaptureKind:
    """What one capture kind from the extension becomes.

    `archived` is the whole difference between the two shapes of capture: a
    picture arrives inside the request and lands on the card directly, while a
    video is fetched afterwards by the module that owns archiving, so Vault
    keeps the record and the bytes come home on their own. That is a property
    of the kind, not a branch somebody re-derives at each call site.
    """

    node_type: str
    archived: bool = False
    requires_image: bool = False


# The kinds `/api/vault/capture` accepts. This used to be a `Literal` in the
# schema, which put the closed vocabulary at the edge of the API, in the one
# file nobody opens when the next kind is being added.
CAPTURE_KINDS: dict[str, CaptureKind] = {
    "screenshot": CaptureKind(node_type=NodeType.IMAGE, requires_image=True),
    "media": CaptureKind(node_type=NodeType.IMAGE, requires_image=True),
    "video": CaptureKind(node_type=NodeType.VIDEO, archived=True),
}


def capture_kind(kind: str | None) -> CaptureKind:
    """The capture spec for a kind, or a refusal that names what is accepted.

    A `ValueError`, so the endpoint answers 422 the way it already answers every
    other bad capture — rather than letting an unknown kind reach the service and
    come back as a 500.
    """
    if kind and kind in CAPTURE_KINDS:
        return CAPTURE_KINDS[kind]
    raise ValueError(f"Unknown capture kind {kind!r}. Accepted: {', '.join(sorted(CAPTURE_KINDS))}.")


def registered_view_names() -> list[str]:
    """The type vocabulary, for the dashboard and for tests."""
    return [member.value for member in NodeType]


def view_map() -> dict[str, str]:
    """The whole `node_type → view` table, built-in types plus any local ones.

    This is what the dashboard renders its dispatch out of, and the local
    directory's contribution enters here and nowhere else. Built last so a local
    type may also deliberately *repoint* a built-in one — the layout of somebody's
    own vault is their business, and the structural/sealed rules do not depend on
    which pane opens.
    """
    merged: dict[str, str] = {}
    for member in NodeType:
        merged[member.value] = VIEWS[member.value]
    merged.update(local_views())
    return merged


# Fragments that make a type name look like it says where the card came from.
# Not a filter — a warning. `node_type` is a structural column, so it is readable
# by anyone holding the database while the vault is locked, and a value like
# `youtube_comment` publishes the owner's browsing through what looks like layout
# metadata. Whether that matters is the owner's call; what is not their call is
# being surprised by it later.
PROVENANCE_HINTS: tuple[str, ...] = (
    "youtube",
    "youtu.be",
    "tiktok",
    "twitter",
    "reddit",
    "pinterest",
    "instagram",
    "facebook",
    "vk.",
    "ok.ru",
    "pixiv",
    "danbooru",
)


def looks_like_provenance(node_type: str) -> bool:
    """Whether this type name appears to name its source rather than its shape."""
    lowered = str(node_type).lower()
    return any(hint in lowered for hint in PROVENANCE_HINTS)


def local_views() -> dict[str, str]:
    """Card types declared in the local directory, if it has any.

    The directory is git-ignored on purpose: it is where an owner keeps card
    types of their own without them becoming part of the module. Loading code
    from it is not a new privilege — it runs with the same permissions as the
    module itself — but it is code that the repository does not know about, so
    a backup that skips the ignored paths will not carry it.

    The format is one `VIEWS` dict per file, and nothing else:

        # app/modules/vault/local_types/quotes.py
        VIEWS = {"quote": "editor"}

    A type declared here opens into a built-in pane, so a local card type gets
    storage, search, sealing, the share contract and the capture endpoint for
    free — and adds no committed code.

    A failure here is never allowed to take the dashboard down: a broken local
    file costs the owner their own card types, not their vault.
    """
    import importlib
    import logging
    import pkgutil

    logger = logging.getLogger(__name__)
    found: dict[str, str] = {}
    try:
        import app.modules.vault.local_types as package
    except ImportError:
        return found
    for info in pkgutil.iter_modules(package.__path__):
        module_name = f"app.modules.vault.local_types.{info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.warning("could not load the local Vault card type %s", module_name, exc_info=True)
            continue
        views = getattr(module, "VIEWS", None)
        if not isinstance(views, dict):
            # Loud, because the failure mode is otherwise invisible: the owner
            # writes the file, the type does not appear, and there is nothing on
            # the page to say why.
            logger.warning("the local Vault card type %s declares no VIEWS dict and was skipped", module_name)
            continue
        for node_type, view in views.items():
            if isinstance(node_type, str) and isinstance(view, str):
                if looks_like_provenance(node_type):
                    logger.warning(
                        "the local Vault card type %r names its source, and node_type is readable "
                        "while the Vault is locked — provenance belongs in the sealed payload or "
                        "behind public_title",
                        node_type,
                    )
                found[node_type] = view
            else:
                logger.warning(
                    "the local Vault card type %s has a non-string entry %r=%r and it was skipped",
                    module_name,
                    node_type,
                    view,
                )
    return found


__all__ = [
    "CAPTURE_KINDS",
    "FALLBACK_VIEW",
    "MEDIA_TYPES",
    "PROVENANCE_HINTS",
    "VIEWS",
    "CaptureKind",
    "NodeType",
    "capture_kind",
    "is_media",
    "local_views",
    "looks_like_provenance",
    "publish_view_map",
    "registered_view_names",
    "view_for",
    "view_map",
]


def publish_view_map() -> None:
    """Offer the view table to every template render of this module.

    Published at import time, not by the dashboard route: this template is also
    rendered by the sharing module as a read-only shared page, and a value passed
    in one route's context does not exist on the other. Publishing here means
    both paths draw the same grid.

    The snapshot is taken once, at import — so a local card type added while the
    process is running needs a restart to appear. That is the honest cost of not
    re-scanning an ignored directory on every render.
    """
    from app.core.template_globals import publish

    publish("vault_views", view_map())
