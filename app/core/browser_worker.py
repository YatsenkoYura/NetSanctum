from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr

from app.core.browser_runtime import browser_runtime
from app.core.module_types import BrowserPolicySpec


class WorkerPolicy(BaseModel):
    id: str
    start_url: str
    allowed_hosts: list[str]
    persist_snapshot: bool = False
    credential_scope: str | None = None
    required_cookie_names: list[str] = Field(default_factory=list)
    persisted_cookie_names: list[str] = Field(default_factory=list)
    persisted_origins: list[str] = Field(default_factory=list)
    allowed_modes: list[str] = Field(default_factory=lambda: ["interactive", "headless"])
    idle_timeout_seconds: int = 600

    def to_spec(self) -> BrowserPolicySpec:
        return BrowserPolicySpec(
            id=self.id,
            start_url=self.start_url,
            allowed_hosts=tuple(self.allowed_hosts),
            persist_snapshot=self.persist_snapshot,
            credential_scope=self.credential_scope,
            required_cookie_names=tuple(self.required_cookie_names),
            persisted_cookie_names=tuple(self.persisted_cookie_names),
            persisted_origins=tuple(self.persisted_origins),
            allowed_modes=tuple(self.allowed_modes),
            idle_timeout_seconds=self.idle_timeout_seconds,
        )


class WorkerStartRequest(BaseModel):
    module_id: str
    policy: WorkerPolicy
    locale: str = "en-US"
    restore_snapshot: bool = True
    mode: str = "interactive"
    storage_state: dict | None = None


class ClickRequest(BaseModel):
    x: float = Field(ge=0, le=1280)
    y: float = Field(ge=0, le=800)


class TypeRequest(BaseModel):
    text: SecretStr = Field(max_length=500)


class KeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=20)


class ScrollRequest(BaseModel):
    delta_y: float = Field(ge=-2000, le=2000)


class NavigateRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4000)


class QueryField(BaseModel):
    selector: str = Field(default="", max_length=500)
    attribute: Literal["href", "src", "data-thumb", "poster", "title", "aria-label"] | None = None


class QueryRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    fields: dict[str, QueryField] = Field(default_factory=dict)


def runtime_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await browser_runtime.close()


app = FastAPI(title="NetSanctum Browser Runtime", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/sessions")
async def start_session(body: WorkerStartRequest):
    try:
        return await browser_runtime.start(
            body.module_id,
            body.policy.to_spec(),
            locale=body.locale,
            restore_snapshot=body.restore_snapshot,
            mode=body.mode,
            storage_state=body.storage_state,
        )
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.get("/sessions/{session_id}")
async def session_status(session_id: str):
    try:
        return await browser_runtime.status(session_id)
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.get("/sessions/{session_id}/frame")
async def session_frame(session_id: str):
    try:
        frame = await browser_runtime.screenshot(session_id)
    except Exception as exc:
        raise runtime_error(exc) from exc
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/sessions/{session_id}/click")
async def session_click(session_id: str, body: ClickRequest):
    try:
        await browser_runtime.click(session_id, body.x, body.y)
        return {"status": "ok"}
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.post("/sessions/{session_id}/type")
async def session_type(session_id: str, body: TypeRequest):
    try:
        await browser_runtime.type_text(session_id, body.text.get_secret_value())
        return {"status": "ok"}
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.post("/sessions/{session_id}/key")
async def session_key(session_id: str, body: KeyRequest):
    try:
        await browser_runtime.press(session_id, body.key)
        return {"status": "ok"}
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.post("/sessions/{session_id}/scroll")
async def session_scroll(session_id: str, body: ScrollRequest):
    try:
        await browser_runtime.scroll(session_id, body.delta_y)
        return {"status": "ok"}
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.post("/sessions/{session_id}/navigate")
async def session_navigate(session_id: str, body: NavigateRequest):
    try:
        return await browser_runtime.navigate(session_id, body.url)
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.post("/sessions/{session_id}/query")
async def session_query(session_id: str, body: QueryRequest):
    try:
        return {
            "items": await browser_runtime.query(
                session_id,
                body.selector,
                limit=body.limit,
                fields={name: field.model_dump() for name, field in body.fields.items()},
            )
        }
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.get("/sessions/{session_id}/storage-state")
async def session_storage_state(session_id: str):
    try:
        return await browser_runtime.export_storage_state(session_id)
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str):
    try:
        await browser_runtime.close(session_id)
        return {"status": "closed"}
    except Exception as exc:
        raise runtime_error(exc) from exc


@app.delete("/policies/{policy_id}/sessions")
async def close_policy_sessions(policy_id: str):
    await browser_runtime.close_policy(policy_id)
    return {"status": "closed"}
