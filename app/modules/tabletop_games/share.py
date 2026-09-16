from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession


class TabletopShareProvider:
    async def catalog(self, db: AsyncSession) -> list[dict]:
        return [
            {
                "id": "panel",
                "title": "Панель настольных игр",
                "subtitle": "Создание лобби и полный доступ ведущего",
                "selector_key": "panel_ids",
            }
        ]

    async def selection(
        self,
        db: AsyncSession,
        selection_mode: str,
        selector: dict,
    ) -> dict:
        if selection_mode == "all":
            return {}
        if selector.get("panel_ids") != ["panel"]:
            raise HTTPException(status_code=422, detail="Select the tabletop panel")
        return {"panel_ids": ["panel"]}


PROVIDER = TabletopShareProvider()
