import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class VaultCollection(Base):
    __tablename__ = "vault_collections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    color = Column(String, default="teal", nullable=False)
    icon = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # A sealed collection owns a data key that is never stored in the clear. Only
    # the passphrase-wrapped key and its KDF parameters are persisted, so the
    # passphrase itself has to be supplied again to read anything inside.
    is_encrypted = Column(Boolean, default=False, nullable=False, index=True)
    key_salt = Column(String, nullable=True)
    wrapped_key = Column(String, nullable=True)
    key_kdf = Column(String, nullable=True)
    key_kdf_params = Column(JSON, nullable=True)
    # The alias the sidebar shows while the collection is locked.
    public_name = Column(String, nullable=True)
    # Blind-write inbox. The public half is readable on purpose: it is what lets
    # the browser extension seal a capture without ever holding the passphrase.
    inbox_public_key = Column(String, nullable=True)
    inbox_private_key = Column(Text, nullable=True)

    items = relationship("VaultItem", back_populates="collection")


class VaultItem(Base):
    __tablename__ = "vault_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_type = Column(String, nullable=False, default="bookmark", index=True)  # bookmark, rating, thought
    title = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=True)
    url = Column(String, nullable=True)

    # Auto-fetched preview metadata
    og_title = Column(String, nullable=True)
    og_description = Column(Text, nullable=True)
    og_image = Column(String, nullable=True)

    # A stored video file, kept in Vault's own storage namespace. Vault owns the
    # bytes for a media card rather than pointing at another module's copy, which
    # is what kept the two records from drifting apart.
    media_path = Column(String, nullable=True, index=True)
    media_mime = Column(String, nullable=True)
    media_size = Column(Integer, nullable=True)

    # For an item in a sealed collection, everything the owner wrote lives here as
    # one AEAD blob and the readable columns above stay empty. Structural columns
    # (which collection, which node type, pinned, timestamps) deliberately stay in
    # the clear: the grid needs them to lay out a tile, and they describe the shape
    # of the record rather than its contents.
    sealed_payload = Column(Text, nullable=True)
    # The alias a sealed item shows while it is locked. It is readable on purpose —
    # a locked vault has to be navigable — and it is never the real title.
    public_title = Column(String, nullable=True)
    # For a blind write, the item key wrapped under the collection's inbox public
    # key. Without this the sealed payload is unrecoverable.
    wrapped_key = Column(Text, nullable=True)

    # Media tracker fields
    score = Column(Float, nullable=True)  # 1.0 - 10.0
    status = Column(String, nullable=True, index=True)  # watching, completed, dropped, planned, on_hold
    progress_current = Column(Integer, default=0, nullable=False)
    progress_total = Column(Integer, nullable=True)
    rewatch_count = Column(Integer, default=0, nullable=False)
    category = Column(
        String, nullable=True, index=True
    )  # anime, series, movie, game, manga, book, article, other

    # Organization
    tags = Column(JSON, default=list, nullable=False)  # List of tag strings
    is_pinned = Column(Boolean, default=False, nullable=False, index=True)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)

    collection_id = Column(
        Integer, ForeignKey("vault_collections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    collection = relationship("VaultCollection", back_populates="items")

    # Loose coupling / soft integration with other NetSanctum modules
    related_entity_type = Column(String, nullable=True, index=True)  # video, manga, song, torrent, other
    related_entity_id = Column(String, nullable=True)

    # Obsidian-style hierarchy and workspace nodes
    parent_id = Column(Integer, ForeignKey("vault_items.id", ondelete="CASCADE"), nullable=True, index=True)
    is_folder = Column(Boolean, default=False, nullable=False, index=True)
    node_type = Column(
        String, default="note", nullable=False, index=True
    )  # folder, note, table, whiteboard, bookmark, rating
    canvas_data = Column(JSON, default=dict, nullable=False)  # Node positions/cards for whiteboard view

    parent = relationship("VaultItem", remote_side=[id], backref="children")

    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
