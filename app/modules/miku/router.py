from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.modules import module_registry
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.miku.schemas import MikuCapabilities, MikuQuery, MikuReply
from app.modules.miku.service import MikuQueryError, capabilities, query

router = APIRouter()


@router.get("/miku/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def miku_dashboard(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "miku_dashboard.html",
        {"user": user, "lang": request.cookies.get("lang", "en")},
    )


@router.get("/api/miku/capabilities", response_model=MikuCapabilities)
async def miku_capabilities(user=Depends(get_current_user)):
    return capabilities(module_registry)


@router.post("/api/miku/query", response_model=MikuReply)
async def miku_query(
    body: MikuQuery,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await query(body, db, user, module_registry)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
