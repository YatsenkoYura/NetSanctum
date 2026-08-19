import base64
import binascii
from typing import Any

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_types import ShareAsset, ShareRoute
from app.modules.vault.models import VaultCollection, VaultItem

MAX_SHARED_ITEMS = 500
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DATA_IMAGE_TYPES = {
    "data:image/gif;base64": "image/gif",
    "data:image/jpeg;base64": "image/jpeg",
    "data:image/png;base64": "image/png",
    "data:image/webp;base64": "image/webp",
}


def _not_found(detail: str = "Shared content not found") -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def _parse_item_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        item_id = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        item_id = int(value)
    else:
        raise ValueError
    if item_id < 1:
        raise ValueError
    return item_id


def _decode_data_image(value: str | None) -> tuple[bytes, str] | None:
    if not value:
        return None
    header, separator, payload = value.partition(",")
    media_type = DATA_IMAGE_TYPES.get(header.lower())
    if not separator or not media_type or len(payload) > (MAX_IMAGE_BYTES * 4 // 3) + 4:
        return None
    try:
        content = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not content or len(content) > MAX_IMAGE_BYTES:
        return None
    return content, media_type


class VaultShareProvider:
    async def catalog(self, db: AsyncSession) -> list[dict]:
        result = await db.execute(
            select(VaultItem, VaultCollection.name)
            .outerjoin(VaultCollection, VaultCollection.id == VaultItem.collection_id)
            .order_by(VaultItem.created_at.desc())
        )
        return [
            {
                "id": item.id,
                "title": item.title or item.og_title or f"Vault item #{item.id}",
                "subtitle": collection_name or item.node_type or item.entry_type,
                "entry_type": item.entry_type,
                "node_type": item.node_type,
                "category": item.category,
                "collection_name": collection_name,
                "is_archived": item.is_archived,
            }
            for item, collection_name in result.all()
        ]

    async def selection(
        self,
        db: AsyncSession,
        selection_mode: str,
        selector: dict,
    ) -> dict:
        if not isinstance(selector, dict):
            raise HTTPException(status_code=422, detail="Selector must be an object")
        if selection_mode == "all":
            if selector:
                raise HTTPException(status_code=422, detail="Selector must be empty for an all-items share")
            return {}
        if selection_mode != "selected":
            raise HTTPException(status_code=422, detail="Invalid selection mode")
        if set(selector) != {"item_ids"}:
            raise HTTPException(status_code=422, detail="Selector must contain only item_ids")

        item_ids = selector["item_ids"]
        if not isinstance(item_ids, list) or not item_ids:
            raise HTTPException(status_code=422, detail="Select at least one Vault item")
        if len(item_ids) > MAX_SHARED_ITEMS:
            raise HTTPException(
                status_code=422,
                detail=f"A share may contain at most {MAX_SHARED_ITEMS} Vault items",
            )

        normalized_ids: list[int] = []
        try:
            for value in item_ids:
                item_id = _parse_item_id(value)
                if item_id not in normalized_ids:
                    normalized_ids.append(item_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid Vault item ID")

        result = await db.execute(select(VaultItem.id).where(VaultItem.id.in_(normalized_ids)))
        found_ids = set(result.scalars().all())
        missing_ids = [item_id for item_id in normalized_ids if item_id not in found_ids]
        if missing_ids:
            missing = ", ".join(str(item_id) for item_id in missing_ids)
            raise HTTPException(status_code=422, detail=f"Vault items not found: {missing}")
        return {"item_ids": normalized_ids}

    @staticmethod
    def _scope_ids(share) -> list[int] | None:
        if share.selection_mode == "all":
            return None
        values = share.selector.get("item_ids", []) if isinstance(share.selector, dict) else []
        try:
            return [_parse_item_id(value) for value in values]
        except ValueError:
            return []

    async def _get_allowed_item(
        self,
        db: AsyncSession,
        share,
        value: Any,
    ) -> tuple[VaultItem, str | None]:
        try:
            item_id = _parse_item_id(value)
        except ValueError:
            raise _not_found()
        scope_ids = self._scope_ids(share)
        if scope_ids is not None and item_id not in scope_ids:
            raise _not_found()
        result = await db.execute(
            select(VaultItem, VaultCollection.name)
            .outerjoin(VaultCollection, VaultCollection.id == VaultItem.collection_id)
            .where(VaultItem.id == item_id)
        )
        row = result.one_or_none()
        if not row:
            raise _not_found()
        return row[0], row[1]

    @staticmethod
    def _parse_bool(value: str | None, default: bool | None) -> bool | None:
        if value is None:
            return default
        normalized = value.lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise HTTPException(status_code=422, detail="Invalid boolean query parameter")

    async def _list_items(self, request: Request, share, db: AsyncSession) -> list[dict]:
        query = request.query_params
        stmt = select(VaultItem, VaultCollection.name).outerjoin(
            VaultCollection,
            VaultCollection.id == VaultItem.collection_id,
        )
        scope_ids = self._scope_ids(share)
        if scope_ids is not None:
            if not scope_ids:
                return []
            stmt = stmt.where(VaultItem.id.in_(scope_ids))

        is_archived = self._parse_bool(query.get("is_archived"), False)
        is_pinned = self._parse_bool(query.get("is_pinned"), None)
        if is_archived is not None:
            stmt = stmt.where(VaultItem.is_archived == is_archived)
        if is_pinned is not None:
            stmt = stmt.where(VaultItem.is_pinned == is_pinned)

        filters = {
            "entry_type": VaultItem.entry_type,
            "category": VaultItem.category,
            "status": VaultItem.status,
            "node_type": VaultItem.node_type,
        }
        for parameter, column in filters.items():
            if value := query.get(parameter):
                stmt = stmt.where(column == value)

        for parameter, column in {
            "collection_id": VaultItem.collection_id,
            "parent_id": VaultItem.parent_id,
        }.items():
            if value := query.get(parameter):
                try:
                    parsed_value = _parse_item_id(value)
                except ValueError:
                    raise HTTPException(status_code=422, detail=f"Invalid {parameter}")
                stmt = stmt.where(column == parsed_value)

        if search := query.get("q"):
            pattern = f"%{search}%"
            stmt = stmt.where(
                VaultItem.title.ilike(pattern)
                | VaultItem.content.ilike(pattern)
                | VaultItem.url.ilike(pattern)
                | VaultItem.og_title.ilike(pattern)
            )

        sort_by = query.get("sort_by", "created_at")
        sort_order = query.get("sort_order", "desc")
        if sort_order not in {"asc", "desc"}:
            raise HTTPException(status_code=422, detail="Invalid sort_order")
        sort_columns = {
            "created_at": VaultItem.created_at,
            "score": VaultItem.score,
            "title": VaultItem.title,
            "updated_at": VaultItem.updated_at,
        }
        if sort_by not in sort_columns:
            raise HTTPException(status_code=422, detail="Invalid sort_by")
        sort_column = sort_columns[sort_by]
        ordering = sort_column.desc() if sort_order == "desc" else sort_column.asc()

        try:
            limit = min(MAX_SHARED_ITEMS, max(1, int(query.get("limit", "100"))))
            offset = max(0, int(query.get("offset", "0")))
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid pagination")
        stmt = stmt.order_by(VaultItem.is_pinned.desc(), ordering).offset(offset).limit(limit)
        result = await db.execute(stmt)
        rows = result.all()

        if tag := query.get("tag"):
            normalized_tag = tag.lower()
            rows = [
                row
                for row in rows
                if row[0].tags and any(value.lower() == normalized_tag for value in row[0].tags)
            ]
        return [self._serialize_item(item, collection_name, share) for item, collection_name in rows]

    @staticmethod
    def _serialize_item(item: VaultItem, collection_name: str | None, share) -> dict:
        image_url = None
        if _decode_data_image(item.og_image):
            image_url = f"/s/{share.id}/api/vault/items/{item.id}/image"
        return {
            "id": item.id,
            "entry_type": item.entry_type,
            "title": item.title,
            "content": item.content,
            "url": item.url,
            "og_title": item.og_title,
            "og_description": item.og_description,
            "og_image": image_url,
            "score": item.score,
            "status": item.status,
            "progress_current": item.progress_current,
            "progress_total": item.progress_total,
            "rewatch_count": item.rewatch_count,
            "category": item.category,
            "tags": item.tags or [],
            "is_pinned": item.is_pinned,
            "is_archived": item.is_archived,
            "collection_id": item.collection_id,
            "collection_name": collection_name,
            "related_entity_type": item.related_entity_type,
            "related_entity_id": item.related_entity_id,
            "parent_id": item.parent_id,
            "is_folder": item.is_folder,
            "node_type": item.node_type,
            "canvas_data": item.canvas_data or {},
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }

    async def _stats(self, db: AsyncSession, share) -> dict:
        stmt = select(VaultItem)
        scope_ids = self._scope_ids(share)
        if scope_ids is not None:
            if not scope_ids:
                items = []
            else:
                result = await db.execute(stmt.where(VaultItem.id.in_(scope_ids)))
                items = list(result.scalars().all())
        else:
            result = await db.execute(stmt)
            items = list(result.scalars().all())

        active_items = [item for item in items if not item.is_archived]
        scores = [item.score for item in active_items if item.score is not None]
        categories: dict[str, int] = {}
        tags: dict[str, int] = {}
        for item in active_items:
            category = item.category or "other"
            categories[category] = categories.get(category, 0) + 1
            for tag in item.tags or []:
                tags[tag] = tags.get(tag, 0) + 1
        top_tags = [
            {"tag": tag, "count": count}
            for tag, count in sorted(tags.items(), key=lambda value: value[1], reverse=True)[:15]
        ]
        return {
            "total_items": len(active_items),
            "bookmarks_count": sum(item.entry_type == "bookmark" for item in active_items),
            "ratings_count": sum(item.entry_type == "rating" for item in active_items),
            "thoughts_count": sum(item.entry_type == "thought" for item in active_items),
            "completed_count": sum(item.status == "completed" for item in active_items),
            "watching_count": sum(item.status == "watching" for item in active_items),
            "pinned_count": sum(item.is_pinned for item in active_items),
            "archived_count": sum(item.is_archived for item in items),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "categories_breakdown": categories,
            "top_tags": top_tags,
        }

    async def entities(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name == "items":
            return await self._list_items(request, share, db)
        if route.name == "item_detail":
            item, collection_name = await self._get_allowed_item(db, share, params["item_id"])
            return self._serialize_item(item, collection_name, share)
        if route.name == "stats":
            return await self._stats(db, share)
        raise _not_found("Shared entity route not found")

    async def relations(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name != "collections":
            raise _not_found("Shared relation route not found")
        stmt = (
            select(VaultCollection, VaultItem.id)
            .join(VaultItem, VaultItem.collection_id == VaultCollection.id)
            .order_by(VaultCollection.name.asc(), VaultItem.id.asc())
        )
        scope_ids = self._scope_ids(share)
        if scope_ids is not None:
            if not scope_ids:
                return []
            stmt = stmt.where(VaultItem.id.in_(scope_ids))
        result = await db.execute(stmt)
        collections: dict[int, dict] = {}
        for collection, item_id in result.all():
            serialized = collections.setdefault(
                collection.id,
                {
                    "id": collection.id,
                    "name": collection.name,
                    "description": collection.description,
                    "color": collection.color,
                    "icon": collection.icon,
                    "created_at": collection.created_at,
                    "items_count": 0,
                    "item_ids": [],
                },
            )
            serialized["items_count"] += 1
            serialized["item_ids"].append(item_id)
        return list(collections.values())

    async def asset(
        self,
        request: Request,
        share,
        db: AsyncSession,
        asset: ShareAsset,
        params: dict,
    ):
        if asset.name != "item_image":
            raise _not_found()
        item, _collection_name = await self._get_allowed_item(db, share, params["item_id"])
        decoded = _decode_data_image(item.og_image)
        if not decoded:
            raise _not_found("Shared image not found")
        content, media_type = decoded
        response = Response(content=content, media_type=media_type)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


PROVIDER = VaultShareProvider()
