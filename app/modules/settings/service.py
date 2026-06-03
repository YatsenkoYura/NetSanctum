"""
Settings module — Business logic / service layer.

Provides CRUD operations and a resolution engine that walks the
scope hierarchy:  user → module → global.
"""

import json
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.settings.models import Setting
from app.modules.settings.schemas import SettingScope, ValueType


# ── Type casting helper ──────────────────────────────────
def cast_value(raw: str, value_type: str) -> Any:
    """Deserialize a stored string value into the declared Python type."""
    match value_type:
        case "integer":
            return int(raw)
        case "float":
            return float(raw)
        case "boolean":
            return raw.lower() in ("true", "1", "yes")
        case "json":
            return json.loads(raw)
        case _:
            return raw


def serialize_value(value: Any) -> str:
    """Serialize a Python value into a storage-safe string."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ── CRUD ─────────────────────────────────────────────────
async def upsert_setting(
    db: AsyncSession,
    *,
    key: str,
    value: str,
    scope: str = "global",
    module_name: str | None = None,
    user_id: int | None = None,
    description: str | None = None,
    value_type: str = "string",
    is_secret: bool = False,
) -> Setting:
    """
    Create or update a setting identified by (scope, module_name, user_id, key).
    """
    stmt = select(Setting).where(
        and_(
            Setting.scope == scope,
            Setting.module_name == module_name if module_name else Setting.module_name.is_(None),
            Setting.user_id == user_id if user_id else Setting.user_id.is_(None),
            Setting.key == key,
        )
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        existing.value = value
        if description is not None:
            existing.description = description
        existing.value_type = value_type
        existing.is_secret = is_secret
        await db.flush()
        await db.refresh(existing)
        return existing

    setting = Setting(
        scope=scope,
        module_name=module_name,
        user_id=user_id,
        key=key,
        value=value,
        description=description,
        value_type=value_type,
        is_secret=is_secret,
    )
    db.add(setting)
    await db.flush()
    await db.refresh(setting)
    return setting


async def bulk_upsert(
    db: AsyncSession,
    settings_data: list[dict],
    user_id: int | None = None,
) -> list[Setting]:
    """Upsert multiple settings in a single transaction."""
    results = []
    for data in settings_data:
        setting = await upsert_setting(
            db,
            key=data["key"],
            value=data["value"],
            scope=data.get("scope", "global"),
            module_name=data.get("module_name"),
            user_id=user_id if data.get("scope") == "user" else None,
            description=data.get("description"),
            value_type=data.get("value_type", "string"),
            is_secret=data.get("is_secret", False),
        )
        results.append(setting)
    return results


async def get_setting_by_id(db: AsyncSession, setting_id: int) -> Setting | None:
    """Fetch a single setting by its primary key."""
    result = await db.execute(select(Setting).where(Setting.id == setting_id))
    return result.scalar_one_or_none()


async def list_settings(
    db: AsyncSession,
    *,
    scope: str | None = None,
    module_name: str | None = None,
    user_id: int | None = None,
    key_prefix: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Setting], int]:
    """
    List settings with optional filters and pagination.
    Returns (items, total_count).
    """
    conditions = []
    if scope:
        conditions.append(Setting.scope == scope)
    if module_name is not None:
        conditions.append(Setting.module_name == module_name)
    if user_id is not None:
        conditions.append(Setting.user_id == user_id)
    if key_prefix:
        conditions.append(Setting.key.startswith(key_prefix))

    # Total count
    count_stmt = select(func.count(Setting.id))
    if conditions:
        count_stmt = count_stmt.where(and_(*conditions))
    total = (await db.execute(count_stmt)).scalar_one()

    # Items
    query = select(Setting).order_by(Setting.scope, Setting.key)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    items = list(result.scalars().all())

    return items, total


async def delete_setting(db: AsyncSession, setting_id: int) -> bool:
    """Delete a setting by ID. Returns True if deleted."""
    result = await db.execute(
        delete(Setting).where(Setting.id == setting_id)
    )
    return result.rowcount > 0


async def delete_module_settings(
    db: AsyncSession, module_name: str
) -> int:
    """Delete ALL settings for a given module. Returns count deleted."""
    result = await db.execute(
        delete(Setting).where(
            and_(
                Setting.scope == "module",
                Setting.module_name == module_name,
            )
        )
    )
    return result.rowcount


# ── Resolution engine ────────────────────────────────────
async def resolve_setting(
    db: AsyncSession,
    *,
    key: str,
    module_name: str | None = None,
    user_id: int | None = None,
) -> Setting | None:
    """
    Resolve a setting by walking the scope hierarchy:
        1. user-level   (scope=user, user_id=X, key=K)
        2. module-level (scope=module, module_name=M, key=K)
        3. global-level (scope=global, key=K)

    Returns the most specific Setting found, or None if the key
    doesn't exist at any scope.
    """
    # 1. User scope
    if user_id is not None:
        result = await db.execute(
            select(Setting).where(
                and_(
                    Setting.scope == "user",
                    Setting.user_id == user_id,
                    Setting.key == key,
                )
            )
        )
        found = result.scalar_one_or_none()
        if found:
            return found

    # 2. Module scope
    if module_name is not None:
        result = await db.execute(
            select(Setting).where(
                and_(
                    Setting.scope == "module",
                    Setting.module_name == module_name,
                    Setting.key == key,
                )
            )
        )
        found = result.scalar_one_or_none()
        if found:
            return found

    # 3. Global scope
    result = await db.execute(
        select(Setting).where(
            and_(
                Setting.scope == "global",
                Setting.module_name.is_(None),
                Setting.user_id.is_(None),
                Setting.key == key,
            )
        )
    )
    return result.scalar_one_or_none()


async def resolve_many(
    db: AsyncSession,
    *,
    keys: list[str],
    module_name: str | None = None,
    user_id: int | None = None,
) -> dict[str, Setting]:
    """
    Resolve multiple keys at once through the scope hierarchy.
    Returns a dict of {key: resolved_Setting}.
    """
    resolved: dict[str, Setting] = {}
    for key in keys:
        setting = await resolve_setting(
            db, key=key, module_name=module_name, user_id=user_id,
        )
        if setting:
            resolved[key] = setting
    return resolved
