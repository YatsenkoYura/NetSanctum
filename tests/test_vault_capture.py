"""Browser-extension capture path: extension origin, bearer-only auth, item shape."""

import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.http_security import is_cross_site_request
from app.core.security import get_current_bearer_user
from app.modules.vault.router import create_capture
from app.modules.vault.schemas import VaultCaptureCreate
from app.modules.vault.services import MAX_IMAGE_BYTES, create_captured_item

# A 1x1 transparent PNG, the smallest payload the server accepts.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()


class ExecuteOnlySession:
    """`create_vault_item` only needs add/commit/refresh from the adapter."""

    def __init__(self, session: Session):
        self.session = session

    def add(self, instance):
        self.session.add(instance)

    async def commit(self):
        self.session.commit()

    async def refresh(self, instance):
        self.session.refresh(instance)


def capture_payload(**overrides) -> dict:
    payload = {
        "kind": "screenshot",
        "title": "Example page",
        "page_url": "https://example.com/article",
        "image": PNG_DATA_URL,
    }
    payload.update(overrides)
    return payload


def make_request(method: str, path: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "query_string": b"",
            "scheme": "https",
            "server": ("sanctum.example", 443),
            "client": ("127.0.0.1", 1234),
        }
    )


class CaptureOriginTests(unittest.TestCase):
    """A packaged extension is cross-origin by construction; only capture allows it."""

    def base_headers(self, path: str, origin: str | None, fetch_site: str | None = "cross-site") -> dict:
        headers = {"host": "sanctum.example"}
        if origin:
            headers["origin"] = origin
        if fetch_site:
            headers["sec-fetch-site"] = fetch_site
        return headers

    def test_extension_origin_may_post_to_the_capture_endpoint(self):
        for origin in (
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            "moz-extension://3f2b1c4d-5e6f-4a1b-8c9d-0e1f2a3b4c5d",
        ):
            with self.subTest(origin=origin):
                request = make_request(
                    "POST",
                    "/api/vault/capture",
                    self.base_headers("/api/vault/capture", origin),
                )
                self.assertFalse(is_cross_site_request(request))

    def test_extension_origin_may_exchange_the_bootstrap_token(self):
        # Without this the extension can never obtain a bearer session, so every
        # capture fails at the login step instead of at the capture endpoint.
        for origin in (
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            "moz-extension://3f2b1c4d-5e6f-4a1b-8c9d-0e1f2a3b4c5d",
        ):
            with self.subTest(origin=origin):
                request = make_request("POST", "/auth/login", self.base_headers("/auth/login", origin))
                self.assertFalse(is_cross_site_request(request))

    def test_extension_origin_is_rejected_on_every_other_route(self):
        for path in ("/api/vault/items", "/auth/ui/login", "/api/shares"):
            with self.subTest(path=path):
                request = make_request(
                    "POST",
                    path,
                    self.base_headers(path, "chrome-extension://abcdefghijklmnopabcdefghijklmnop"),
                )
                self.assertTrue(is_cross_site_request(request))

    def test_a_web_origin_cannot_borrow_the_capture_allowance(self):
        request = make_request(
            "POST",
            "/api/vault/capture",
            self.base_headers("/api/vault/capture", "https://evil.example"),
        )
        self.assertTrue(is_cross_site_request(request))

    def test_the_capture_path_is_not_a_general_cross_site_capability(self):
        request = make_request(
            "POST",
            "/s/example/access",
            self.base_headers("/s/example/access", "https://evil.example"),
        )
        self.assertFalse(is_cross_site_request(request))


class BearerOnlyAuthTests(unittest.TestCase):
    async def _resolve(self, headers: dict[str, str]):
        return await get_current_bearer_user(make_request("POST", "/api/vault/capture", headers))

    def test_a_session_cookie_alone_is_not_accepted(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(self._resolve({"host": "sanctum.example", "cookie": "access_token=session"}))
        self.assertEqual(401, caught.exception.status_code)

    def test_a_bearer_token_authenticates(self):
        async def scenario():
            with patch(
                "app.core.security.get_current_user",
                AsyncMock(return_value=SimpleNamespace(id=1, username="owner")),
            ):
                return await self._resolve(
                    {"host": "sanctum.example", "authorization": "Bearer session-token"}
                )

        self.assertEqual("owner", asyncio.run(scenario()).username)


class CaptureSchemaTests(unittest.TestCase):
    def test_a_capture_requires_an_image(self):
        payload = capture_payload()
        del payload["image"]
        with self.assertRaises(ValidationError):
            VaultCaptureCreate(**payload)

    def test_a_capture_requires_a_title(self):
        with self.assertRaises(ValidationError):
            VaultCaptureCreate(**capture_payload(title=""))

    def test_non_http_urls_are_rejected(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,<h1>x</h1>"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                VaultCaptureCreate(**capture_payload(page_url=url))

    def test_urls_with_credentials_are_rejected(self):
        with self.assertRaises(ValidationError):
            VaultCaptureCreate(**capture_payload(page_url="https://user:pass@example.com/"))

    def test_source_url_is_provenance_text_and_may_be_any_scheme(self):
        # A blob: address is dead the moment the page navigates, but refusing it
        # would fail a capture the user deliberately made over an informational
        # field. It is shown as text and never dereferenced.
        for value in ("blob:https://www.youtube.com/9f0e", "data:image/png;base64,AAAA", "about:blank"):
            with self.subTest(value=value):
                capture = VaultCaptureCreate(**capture_payload(source_url=value))
                self.assertEqual(value, capture.source_url)

    def test_a_video_url_must_stay_addressable(self):
        # Unlike source_url this one reaches a downloader, so it is checked.
        for value in ("file:///etc/passwd", "javascript:alert(1)", "blob:https://x/y"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                VaultCaptureCreate(**video_payload(video_url=value))

    def test_an_oversized_body_is_rejected_by_the_schema(self):
        payload = "A" * ((MAX_IMAGE_BYTES * 4 // 3) + 64)
        with self.assertRaises(ValidationError):
            VaultCaptureCreate(**capture_payload(image=f"data:image/png;base64,{payload}"))

    def test_an_image_under_the_ceiling_still_validates(self):
        payload = "A" * 1024
        self.assertEqual(
            f"data:image/png;base64,{payload}",
            VaultCaptureCreate(**capture_payload(image=f"data:image/png;base64,{payload}")).image,
        )


class CreateCapturedItemTests(unittest.TestCase):
    def build_session(self):
        from app.core.database import Base
        from app.modules.vault.models import VaultCollection, VaultItem

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[VaultCollection.__table__, VaultItem.__table__])
        return Session(engine, expire_on_commit=False), VaultItem

    def test_a_screenshot_lands_as_an_embedded_image_bookmark(self):
        session, item_model = self.build_session()
        item = asyncio.run(
            create_captured_item(
                ExecuteOnlySession(session),
                VaultCaptureCreate(**capture_payload(tags=["from-extension"])),
            )
        )

        self.assertIsInstance(item, item_model)
        self.assertEqual("bookmark", item.entry_type)
        self.assertEqual("image", item.node_type)
        self.assertEqual(PNG_DATA_URL, item.og_image)
        self.assertEqual("https://example.com/article", item.url)
        self.assertEqual(["from-extension"], item.tags)
        self.assertIn("https://example.com/article", item.content)

    def test_media_captures_keep_their_source_url_and_alt_text(self):
        session, _ = self.build_session()
        item = asyncio.run(
            create_captured_item(
                ExecuteOnlySession(session),
                VaultCaptureCreate(
                    **capture_payload(
                        kind="media",
                        title="A cat",
                        source_url="https://cdn.example.com/cat.png",
                        alt_text="A sleeping cat",
                        tags=[],
                    )
                ),
            )
        )

        self.assertIn("A sleeping cat", item.content)
        self.assertIn("Source: https://cdn.example.com/cat.png", item.content)
        self.assertIn("Page: https://example.com/article", item.content)

    def test_an_unsupported_image_is_refused_before_any_write(self):
        session, item_model = self.build_session()
        capture = VaultCaptureCreate(**capture_payload())
        capture.image = "data:image/tiff;base64,AAAA"

        with self.assertRaises(ValueError):
            asyncio.run(create_captured_item(ExecuteOnlySession(session), capture))

        self.assertEqual(0, session.query(item_model).count())

    def test_the_router_returns_the_lazy_image_url_of_the_new_item(self):
        created = SimpleNamespace(id=42, title="Example page", og_image=PNG_DATA_URL, related_entity_id=None)
        capture = VaultCaptureCreate(**capture_payload())

        with patch(
            "app.modules.vault.router.create_captured_item", AsyncMock(return_value=created)
        ) as create:
            result = asyncio.run(create_capture(capture, db=object(), user=None))

        create.assert_awaited_once()
        self.assertEqual(42, result.item_id)
        self.assertEqual("/api/vault/items/42/image", result.image_url)
        self.assertEqual("completed", result.status)

    def test_the_router_reports_an_invalid_image_as_422(self):
        capture = VaultCaptureCreate(**capture_payload())
        with patch(
            "app.modules.vault.router.create_captured_item",
            AsyncMock(side_effect=ValueError("Capture image must be a supported data:image URL")),
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(create_capture(capture, db=object(), user=None))

        self.assertEqual(422, caught.exception.status_code)


def video_payload(**overrides) -> dict:
    payload = {
        "kind": "video",
        "title": "A talk",
        "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "page_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    payload.update(overrides)
    return payload


class VideoCaptureTests(unittest.TestCase):
    """A video card keeps its own bytes; nothing is handed to another module."""

    def build_session(self):
        from app.core.database import Base
        from app.modules.vault.models import VaultCollection, VaultItem

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[VaultCollection.__table__, VaultItem.__table__])
        return Session(engine, expire_on_commit=False), VaultItem

    def capture(self, session, **overrides):
        return asyncio.run(
            create_captured_item(
                ExecuteOnlySession(session),
                VaultCaptureCreate(**video_payload(**overrides)),
                user="owner",
            )
        )

    def test_a_video_capture_becomes_a_video_card(self):
        session, item_model = self.build_session()
        with patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="task-7")):
            item = self.capture(session)

        self.assertIsInstance(item, item_model)
        self.assertEqual("video", item.node_type)
        self.assertEqual("https://www.youtube.com/watch?v=dQw4w9WgXcQ", item.url)

    def test_the_download_is_queued_for_vault_storage(self):
        session, _ = self.build_session()
        queue = AsyncMock(return_value="task-7")
        with patch("app.modules.vault.services.queue_video_download", queue):
            self.capture(session, quality="1080", title="A talk")

        item_id = queue.await_args.args[1]
        url = queue.await_args.args[2]
        self.assertIn("youtube.com/watch", url)
        self.assertEqual("1080", queue.await_args.kwargs["quality"])
        self.assertGreater(item_id, 0)

    def test_no_other_module_is_asked_to_archive_it(self):
        session, item_model = self.build_session()
        with (
            patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="task-7")),
            patch(
                "app.modules.vault.services.module_registry.invoke_integration",
                AsyncMock(side_effect=AssertionError("vault must not call another module")),
            ),
        ):
            item = self.capture(session)

        self.assertIsNone(item.related_entity_id)
        self.assertIsNone(item.related_entity_type)
        self.assertEqual(1, session.query(item_model).count())

    def test_a_broker_failure_keeps_the_card(self):
        session, item_model = self.build_session()
        # dispatch_tracked_async is imported inside the function, so the broker
        # is faked at its own module.
        with patch(
            "app.core.task_dispatch.dispatch_tracked_async",
            AsyncMock(side_effect=RuntimeError("no broker")),
        ):
            item = self.capture(session)

        self.assertEqual("video", item.node_type)
        self.assertEqual(1, session.query(item_model).count())

    def test_a_video_capture_needs_no_image(self):
        session, _ = self.build_session()
        with patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="t1")):
            item = self.capture(session)

        self.assertIsNone(item.og_image)
        self.assertIsNone(item.media_path)

    def test_a_video_capture_may_carry_a_still(self):
        session, _ = self.build_session()
        with patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="t1")):
            item = self.capture(session, image=PNG_DATA_URL)

        self.assertEqual(PNG_DATA_URL, item.og_image)

    def test_a_video_still_may_be_a_remote_poster(self):
        session, _ = self.build_session()
        poster = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        with patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="t1")):
            item = self.capture(session, image=poster)

        self.assertEqual(poster, item.og_image)

    def test_a_video_still_may_not_be_a_script(self):
        session, _ = self.build_session()
        with (
            patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="t1")),
            self.assertRaises(ValueError),
        ):
            self.capture(session, image="javascript:alert(1)")

    def test_a_blob_provenance_is_text_and_never_the_link(self):
        session, _ = self.build_session()
        blob = "blob:https://www.youtube.com/2f3c-video"
        with patch("app.modules.vault.services.queue_video_download", AsyncMock(return_value="t1")):
            item = self.capture(session, source_url=blob)

        self.assertIn(f"Source: {blob}", item.content)
        # The link stays on the real page, not on a dead blob address.
        self.assertEqual("https://www.youtube.com/watch?v=dQw4w9WgXcQ", item.url)


if __name__ == "__main__":
    unittest.main()
