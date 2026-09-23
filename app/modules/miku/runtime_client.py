import logging

import httpx

from app.core.config import get_settings
from app.modules.miku.planner import MikuQueryError, plan_with_rules
from app.modules.miku.schemas import MikuDecision, MikuDecisionRequest

logger = logging.getLogger(__name__)


class MikuRuntimeClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        enabled: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.MIKU_RUNTIME_URL).rstrip("/")
        self.token = token if token is not None else settings.MIKU_RUNTIME_TOKEN
        self.enabled = settings.MIKU_RUNTIME_ENABLED if enabled is None else enabled
        self.transport = transport

    async def decide(self, message: str) -> MikuDecision:
        if not self.enabled:
            return plan_with_rules(message)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=10,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/v1/decide",
                    headers={"X-Miku-Runtime-Token": self.token},
                    json=MikuDecisionRequest(message=message).model_dump(mode="json"),
                )
        except httpx.HTTPError:
            logger.warning("MIKU runtime is unavailable; using the local rule planner")
            return plan_with_rules(message)
        if response.status_code == 422:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise MikuQueryError(detail or "Runtime rejected the query")
        if response.status_code >= 400:
            logger.warning(
                "MIKU runtime returned status %s; using the local rule planner", response.status_code
            )
            return plan_with_rules(message)
        return MikuDecision.model_validate(response.json())


miku_runtime_client = MikuRuntimeClient()
