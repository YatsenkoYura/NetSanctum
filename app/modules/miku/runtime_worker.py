import hmac
import os

from fastapi import FastAPI, Header, HTTPException

from app.modules.miku.planner import MikuQueryError, plan_with_rules
from app.modules.miku.schemas import MikuDecision, MikuDecisionRequest

RUNTIME_TOKEN = os.getenv("MIKU_RUNTIME_TOKEN", "")

app = FastAPI(
    title="MIKU Runtime",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def verify_runtime_token(token: str) -> None:
    if not RUNTIME_TOKEN or not hmac.compare_digest(token, RUNTIME_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid runtime token")


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "rules", "protocol_version": 1}


@app.post("/v1/decide", response_model=MikuDecision)
async def decide(
    body: MikuDecisionRequest,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
):
    verify_runtime_token(runtime_token)
    try:
        return plan_with_rules(body.message)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
