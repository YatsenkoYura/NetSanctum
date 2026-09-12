from app.contracts.video_source_catalog_v1 import VideoSourceRequest, VideoSourceResult
from app.core.module_types import IntegrationContext, IntegrationRejectedError, IntegrationServiceError
from app.modules.youtube.services import YouTubeAPIError, YouTubeClient, load_cookies


async def video_source_catalog(
    request: VideoSourceRequest,
    context: IntegrationContext,
) -> VideoSourceResult:
    try:
        client = YouTubeClient(await load_cookies())
        if request.operation == "search":
            return await client.search(request.query or "", request.page_token)
        if request.operation == "recommendations":
            return await client.recommendations(request.page_token)
        if request.operation == "subscriptions":
            return await client.subscriptions(request.page_token)
        if request.operation == "history":
            return await client.history(request.page_token)
        if request.operation == "watch_later":
            return await client.watch_later(request.page_token)
        if request.operation == "channel":
            return await client.channel_videos(request.entity_id or "", request.page_token)
        if request.operation == "playlist":
            return await client.playlist_items(request.entity_id or "", request.page_token)
        return await client.popular(request.page_token)
    except ValueError as exc:
        raise IntegrationRejectedError(str(exc)) from exc
    except YouTubeAPIError as exc:
        if exc.status_code in {429, 502, 503}:
            raise IntegrationServiceError(str(exc), exc.status_code) from exc
        raise IntegrationRejectedError(str(exc)) from exc
