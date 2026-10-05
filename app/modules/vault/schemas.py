import datetime
import json
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

# Shared ceiling for an embedded picture. It lives here because the capture
# schema needs it to bound a request body, and services imports this module.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Ceilings for fields that went in unbounded on the item write paths. None of
# these is a security boundary — the renderer escapes all of it, and the point is
# weight, not safety: every value is serialized into the dashboard payload and,
# for a sealed card, sealed and re-opened on each unlock, so one field with no
# ceiling is a weight the whole collection carries.
#
# They are deliberately far above anything real. A limit that fires on ordinary
# input is a bug that looks like a safety feature, so each one sits where a
# person would have to be doing something unusual to reach it: URLs from sites
# that stuff a session and a signature into the query, tags from platforms that
# treat them as sentences, descriptions from feeds that emit a paragraph.
MAX_CONTENT_CHARS = 1024 * 1024
MAX_URL_CHARS = 8000
MAX_OG_TITLE_CHARS = 4000
MAX_OG_DESCRIPTION_CHARS = 8000
# `og_image` is not a URL on the write path: the dashboard puts an inline
# `data:image/...;base64,` picture straight into it, so it gets the encoded
# image ceiling the capture schema already uses for its own picture field.
MAX_OG_IMAGE_CHARS = (MAX_IMAGE_BYTES * 4 // 3) + 64
MAX_TAGS = 200
MAX_TAG_CHARS = 200
MAX_CANVAS_BYTES = 8 * 1024 * 1024


def _bounded_tags(tags: list[str]) -> list[str]:
    """Trim, cut and count tags.

    A tag is a label, not a document. Cutting rather than rejecting keeps a
    paste from one platform — a comma-separated paragraph of them, or a stray
    essay — from failing the whole write.
    """
    out: list[str] = []
    for tag in tags[:MAX_TAGS]:
        trimmed = " ".join(str(tag).split())[:MAX_TAG_CHARS].strip()
        if trimmed:
            out.append(trimmed)
    return out


def _bounded_canvas(canvas: dict[str, Any]) -> dict[str, Any]:
    """Refuse a canvas blob too large to keep.

    A whiteboard drawing is an inline image, so the honest ceiling is megabytes
    rather than characters; this is a weight limit, not a format check.
    """
    if len(json.dumps(canvas, default=str).encode("utf-8")) > MAX_CANVAS_BYTES:
        raise ValueError(f"canvas_data is over the {MAX_CANVAS_BYTES} byte ceiling")
    return canvas


class VaultCaptureCreate(BaseModel):
    """A capture pushed by the browser extension.

    `image` is a `data:image/...;base64,` URL, the same shape the dashboard
    stores for a pasted screenshot, so a capture lands in Vault exactly like a
    manually pasted picture. `source_url` keeps the media or page address the
    capture came from so the entry stays navigable.

    `kind="video"` is different in kind, not in shape: the video file itself is
    archived by the module that owns it, and Vault keeps the record plus an
    optional still. That keeps large blobs out of Vault, which matters for the
    per-collection encryption planned for it.
    """

    kind: Literal["screenshot", "media", "video"]
    title: str = Field(..., min_length=1, max_length=2000)
    # The alias shown while a sealed Vault is locked. The extension cannot be given
    # a passphrase, so this is the only handle the owner has on a blind write.
    public_title: str | None = Field(default=None, max_length=1000)
    page_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    # Free-form provenance, shown as text and never dereferenced, so a `blob:`
    # or `data:` value is kept rather than refused.
    source_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    alt_text: str | None = Field(default=None, max_length=1000)
    image: str | None = Field(
        default=None,
        description="data:image/...;base64 payload; required unless kind is video",
        # The encoded ceiling of `MAX_IMAGE_BYTES`; the service still decodes and
        # checks the bytes, this only keeps an oversized body out of the router.
        max_length=(MAX_IMAGE_BYTES * 4 // 3) + 64,
    )
    video_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    quality: Literal["best", "1080", "720", "480", "360"] = "720"
    tags: list[str] = Field(default_factory=list)
    collection_id: int | None = None
    parent_id: int | None = None
    auto_fetch_og: bool = True

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "video":
            if not self.video_url:
                raise ValueError("A video capture requires video_url")
        elif not self.image:
            raise ValueError("A screenshot or media capture requires an image")
        # `page_url` becomes the item's clickable link and `video_url` is handed
        # to a downloader that will fetch it, so both must be real addresses.
        # `source_url` is neither: it is only ever shown as text, so an
        # unrecognised scheme there must not fail a capture the user chose.
        for value in (self.page_url, self.video_url):
            if value is None:
                continue
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Capture URLs must be public HTTP URLs")
            if parsed.username or parsed.password:
                raise ValueError("Capture URLs must not contain credentials")
        self.tags = _bounded_tags(self.tags)
        return self


class VaultCaptureResponse(BaseModel):
    status: Literal["completed"] = "completed"
    item_id: int
    kind: Literal["screenshot", "media", "video"]
    title: str
    image_url: str | None = None
    task_id: str | None = Field(default=None, description="Archive job, for a video capture")
    message: str


class VaultCollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    color: str = "teal"
    icon: str | None = None

    # A sealed collection needs a passphrase, and a public alias: while the
    # collection is locked the sidebar must show something, and it must not be the
    # real name.
    passphrase: str | None = Field(default=None, max_length=512)
    public_name: str | None = Field(default=None, max_length=100)
    # Create the space inside another one. Nesting a sealed space is allowed;
    # the sidebar keeps a locked sealed branch folded so its children stay hidden.
    parent_id: int | None = Field(default=None, ge=1)


class VaultCollectionMove(BaseModel):
    """Nest a space under another one, or move it among its siblings."""

    collection_id: int = Field(..., ge=1)
    parent_id: int | None = Field(default=None, ge=1)
    # Neighbours by id, the same way a card drop names them: the server works out
    # the position, so two tabs disagreeing about the order cannot corrupt it.
    before_id: int | None = Field(default=None, ge=1)
    after_id: int | None = Field(default=None, ge=1)


class VaultItemMove(BaseModel):
    """Put a card where it was dropped, between two of its neighbours."""

    item_id: int = Field(..., ge=1)
    before_id: int | None = Field(default=None, ge=1)
    after_id: int | None = Field(default=None, ge=1)


class VaultCollectionResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    color: str
    icon: str | None = None
    created_at: datetime.datetime
    items_count: int | None = 0
    # Where the space sits in the tree. `parent_id` is null for a root space;
    # `position` orders siblings and is what the sidebar renders.
    parent_id: int | None = None
    position: float | None = None
    # A sealed collection has two names as well: the alias shown while it is
    # locked, and the real one once the passphrase has been supplied.
    is_encrypted: bool = False
    public_name: str | None = None
    is_locked: bool = False
    # First 16 hex of SHA-256 over the inbox public key. Public by design — the key
    # itself is public — so the owner can compare it against the extension's copy
    # before typing the passphrase into a page that might show a swapped key.
    key_fingerprint: str | None = None

    class Config:
        from_attributes = True


class VaultCollectionMerge(BaseModel):
    """Move every card from one workspace into another, deleting the emptied one."""

    from_id: int = Field(..., ge=1)
    into_id: int | None = Field(default=None, ge=1)


class VaultItemCreate(BaseModel):
    entry_type: str = Field("bookmark", description="bookmark, rating, or thought")
    title: str | None = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=MAX_CONTENT_CHARS)
    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)

    og_title: str | None = Field(default=None, max_length=MAX_OG_TITLE_CHARS)
    og_description: str | None = Field(default=None, max_length=MAX_OG_DESCRIPTION_CHARS)
    og_image: str | None = Field(default=None, max_length=MAX_OG_IMAGE_CHARS)

    # Required for a sealed item: the alias shown while its Vault is locked. It is
    # stored in the clear on purpose and is never the real title.
    public_title: str | None = Field(default=None, max_length=1000)

    score: float | None = Field(None, ge=1.0, le=10.0)
    status: str | None = Field(None, description="watching, completed, dropped, planned, on_hold")
    progress_current: int = 0
    progress_total: int | None = None
    rewatch_count: int = 0
    category: str | None = Field(None, description="anime, series, movie, game, manga, book, article, other")

    tags: list[str] = Field(default_factory=list)
    is_pinned: bool = False
    is_archived: bool = False
    collection_id: int | None = None

    related_entity_type: str | None = None
    related_entity_id: str | None = None

    parent_id: int | None = None
    is_folder: bool = False
    node_type: str = "note"  # folder, note, table, whiteboard, bookmark, rating
    canvas_data: dict[str, Any] = Field(default_factory=dict)

    auto_fetch_og: bool = True  # If true and url provided, fetch OG metadata

    @model_validator(mode="after")
    def _fit_the_payload(self) -> "VaultItemCreate":
        self.tags = _bounded_tags(self.tags)
        _bounded_canvas(self.canvas_data)
        return self


class VaultItemUpdate(BaseModel):
    # Editable while the Vault is locked: the alias is public by design.
    public_title: str | None = Field(default=None, max_length=1000)

    title: str | None = Field(default=None, max_length=1000)
    content: str | None = Field(default=None, max_length=MAX_CONTENT_CHARS)
    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    score: float | None = None
    status: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    rewatch_count: int | None = None
    category: str | None = None
    tags: list[str] | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None
    collection_id: int | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    parent_id: int | None = None
    is_folder: bool | None = None
    node_type: str | None = None
    canvas_data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _fit_the_payload(self) -> "VaultItemUpdate":
        if self.tags is not None:
            self.tags = _bounded_tags(self.tags)
        if self.canvas_data is not None:
            _bounded_canvas(self.canvas_data)
        return self


class VaultItemResponse(BaseModel):
    id: int
    entry_type: str
    # While a sealed item is locked this holds its alias, never the real title.
    title: str
    content: str | None = None
    url: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    # True when the item stores an embedded image that the list endpoint
    # omits for weight; the bytes are served via /api/vault/items/{id}/image.
    has_image: bool = False
    # A media card owns a file in Vault storage. The path itself is never sent
    # to a client; only whether the bytes are there and what the download is doing.
    has_media: bool = False
    media_status: str | None = None
    media_duration: int | None = None
    # A short-lived signed video URL, present only for an unlocked sealed card.
    # `<video>` cannot send the unlock header, so the player gets a URL that
    # carries its own authorization instead. Locked cards never have one.
    media_url: str | None = None
    score: float | None = None
    status: str | None = None
    progress_current: int
    progress_total: int | None = None
    rewatch_count: int
    category: str | None = None
    tags: list[str]
    is_pinned: bool
    is_archived: bool
    collection_id: int | None = None
    collection_name: str | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    parent_id: int | None = None
    is_folder: bool = False
    node_type: str = "note"
    canvas_data: dict[str, Any] = Field(default_factory=dict)
    # Where the card sits among its neighbours, set by a drag. Structural rather
    # than sealed, so a locked space still lays its grid out the same way.
    position: float | None = None
    # Lock-state flags. `is_sealed` never changes; `is_locked` is true only while
    # the collection's key is out of reach, and it is what the grid paints red.
    is_sealed: bool = False
    is_locked: bool = False
    public_title: str | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    class Config:
        from_attributes = True


class VaultSpaceDeleteRequest(BaseModel):
    """The passphrase that opens a space, when the owner is destroying one.

    Only sent for a sealed space. A plain one needs nothing, and asking for a
    passphrase there would be a prompt nobody could satisfy by looking at it.
    """

    passphrase: str | None = Field(default=None, max_length=512)


class VaultUnlockRequest(BaseModel):
    passphrase: str = Field(..., min_length=1, max_length=512)


class VaultUnlockResponse(BaseModel):
    status: Literal["unlocked"] = "unlocked"
    collection_id: int
    name: str
    # Lives only in the page's memory. It is not a cookie, it is not stored server
    # side, and it is required again after a reload — that is what makes an unlock
    # per tab rather than per instance.
    unlock_token: str
    unlocked_collections: list[int] = Field(default_factory=list)


class VaultLockResponse(BaseModel):
    status: Literal["locked"] = "locked"
    collection_id: int


class VaultStatsResponse(BaseModel):
    total_items: int
    bookmarks_count: int
    ratings_count: int
    thoughts_count: int
    completed_count: int
    watching_count: int
    pinned_count: int
    archived_count: int
    avg_score: float | None = 0.0
    categories_breakdown: dict[str, int]
    top_tags: list[dict[str, Any]]
