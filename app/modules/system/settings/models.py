"""
Settings module — database model.

Stores key-value pairs with optional scoping:
  - scope="global"       → application-wide defaults
  - scope="module"       → per-module configuration (module_name filled in)
  - scope="user"         → per-user overrides (user_id filled in)

The (scope, module_name, user_id, key) combination is unique, allowing
the same key to have different values at different scope levels.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Setting(Base):
    """Universal key-value setting with hierarchical scoping."""

    __tablename__ = "settings"

    __table_args__ = (
        Index(
            "uq_settings_global_key",
            "key",
            unique=True,
            postgresql_where=text("scope = 'global' AND module_name IS NULL AND user_id IS NULL"),
            sqlite_where=text("scope = 'global' AND module_name IS NULL AND user_id IS NULL"),
        ),
        Index(
            "uq_settings_module_key",
            "module_name",
            "key",
            unique=True,
            postgresql_where=text("scope = 'module' AND module_name IS NOT NULL AND user_id IS NULL"),
            sqlite_where=text("scope = 'module' AND module_name IS NOT NULL AND user_id IS NULL"),
        ),
        Index(
            "uq_settings_user_key",
            "user_id",
            "key",
            unique=True,
            postgresql_where=text("scope = 'user' AND user_id IS NOT NULL AND module_name IS NULL"),
            sqlite_where=text("scope = 'user' AND user_id IS NOT NULL AND module_name IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Scope: "global", "module", or "user"
    scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="global",
        index=True,
    )

    # Filled when scope="module" — the logical name of the target module
    module_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        default=None,
        index=True,
    )

    # Filled when scope="user" — FK-less reference to users.id
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
        index=True,
    )

    # The setting key, e.g. "max_download_concurrency"
    key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # The setting value stored as text (JSON-serializable)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    # Human-readable description of what this setting does
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The JSON type hint for the value: "string", "integer", "float", "boolean", "json"
    value_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="string",
    )

    # Whether this setting is visible in the UI / API
    is_secret: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Setting scope={self.scope!r} module={self.module_name!r} "
            f"user_id={self.user_id} key={self.key!r}>"
        )
