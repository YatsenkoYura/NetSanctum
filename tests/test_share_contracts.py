import asyncio
import inspect
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call, patch

from fastapi import HTTPException, Request, Response
from pydantic import ValidationError

from app.core.modules import ModuleRegistry
from app.modules.alllib.share import AllLibShareProvider
from app.modules.music.share import MusicShareProvider, _selected_songs
from app.modules.sharing import router as sharing_router
from app.modules.sharing.router import (
    _dispatch_shared_api,
    _harden_shared_response,
    _render_shared_application,
    shared_application,
    shared_module_api,
)
from app.modules.sharing.schemas import ShareCreate
from app.modules.sharing.service import (
    CLEAR_SHARE_SESSIONS_SCRIPT,
    CREATE_SESSION_SCRIPT,
    MAX_SHARE_SESSIONS,
    RESERVE_PASSWORD_ATTEMPT_SCRIPT,
    hash_secret,
    is_active,
    session_ttl,
    verify_secret,
)
from app.modules.vault.share import VaultShareProvider
from app.modules.video_archiver.share import VideoShareProvider

ROOT = Path(__file__).resolve().parents[1]


def make_request(path: str = "/s/share-id") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


class ShareServiceTests(unittest.TestCase):
    def test_secret_is_hashed_and_compared(self):
        secret_hash = hash_secret("secret")

        self.assertNotEqual("secret", secret_hash)
        self.assertTrue(verify_secret("secret", secret_hash))
        self.assertFalse(verify_secret("wrong", secret_hash))

    def test_expiration_is_enforced_without_worker_cleanup(self):
        now = datetime.now(UTC)
        active = cast(Any, SimpleNamespace(status="active", expires_at=now + timedelta(minutes=5)))
        expired = cast(Any, SimpleNamespace(status="active", expires_at=now - timedelta(seconds=1)))

        self.assertTrue(is_active(active, now))
        self.assertFalse(is_active(expired, now))

    def test_session_never_outlives_share(self):
        now = datetime.now(UTC)
        share = cast(Any, SimpleNamespace(expires_at=now + timedelta(seconds=90)))

        self.assertEqual(90, session_ttl(share, now))

    def test_shared_html_blocks_external_and_embedded_content(self):
        response = _harden_shared_response(Response())

        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual("DENY", response.headers["X-Frame-Options"])
        self.assertEqual("no-referrer", response.headers["Referrer-Policy"])

    def test_password_length_is_validated_in_characters_and_utf8_bytes(self):
        base = {"module_id": "video_archiver", "title": "Shared videos"}

        with self.assertRaises(ValidationError):
            ShareCreate.model_validate({**base, "password": "1234567"})
        with self.assertRaises(ValidationError):
            ShareCreate.model_validate({**base, "password": ""})
        ShareCreate.model_validate({**base, "password": "x" * 72})
        with self.assertRaises(ValidationError):
            ShareCreate.model_validate({**base, "password": "x" * 73})
        with self.assertRaises(ValidationError):
            ShareCreate.model_validate({**base, "password": "я" * 37})

    def test_redis_scripts_enforce_atomic_security_contracts(self):
        self.assertIn('redis.call("ZREMRANGEBYSCORE"', CREATE_SESSION_SCRIPT)
        self.assertIn('redis.call("ZRANGE"', CREATE_SESSION_SCRIPT)
        self.assertIn('redis.call("SETEX"', CREATE_SESSION_SCRIPT)
        self.assertIn('redis.call("EXISTS", KEYS[3])', CREATE_SESSION_SCRIPT)
        self.assertIn('redis.call("ZRANGE", KEYS[1]', CLEAR_SHARE_SESSIONS_SCRIPT)
        self.assertEqual(32, MAX_SHARE_SESSIONS)
        self.assertIn('redis.call("INCR"', RESERVE_PASSWORD_ATTEMPT_SCRIPT)
        self.assertIn('redis.call("EXPIRE"', RESERVE_PASSWORD_ATTEMPT_SCRIPT)


class ShareRouteSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_share_without_password_skips_session_and_db_write(self):
        request = make_request()
        share = SimpleNamespace(
            id="share-id",
            is_public=True,
            password_hash=None,
            module_id="video_archiver",
        )
        db = AsyncMock()

        with (
            patch.object(sharing_router, "_active_share", AsyncMock(return_value=share)) as active_share,
            patch.object(
                sharing_router,
                "_render_shared_application",
                AsyncMock(return_value=Response("shared")),
            ) as render,
            patch.object(sharing_router, "_has_session", AsyncMock()) as has_session,
            patch.object(sharing_router, "_establish_session", AsyncMock()) as establish_session,
        ):
            response = await shared_application("share-id", request, db)

        self.assertEqual(b"shared", response.body)
        active_share.assert_awaited_once_with(db, "share-id")
        has_session.assert_not_awaited()
        establish_session.assert_not_awaited()
        render.assert_awaited_once_with(request, share)
        db.commit.assert_not_awaited()

    async def test_public_resource_is_authorized_without_session(self):
        request = make_request("/s/share-id/api/video-archiver/videos")
        share = SimpleNamespace(
            id="share-id",
            is_public=True,
            password_hash=None,
            module_id="video_archiver",
        )
        db = AsyncMock()

        with (
            patch.object(sharing_router, "_active_share", AsyncMock(return_value=share)) as active_share,
            patch.object(
                sharing_router,
                "_dispatch_shared_api",
                AsyncMock(return_value=Response("asset")),
            ) as dispatch,
            patch.object(sharing_router, "_has_session", AsyncMock()) as has_session,
        ):
            response = await shared_module_api("share-id", "video-archiver/videos", request, db)

        self.assertEqual(b"asset", response.body)
        active_share.assert_awaited_once_with(db, "share-id")
        has_session.assert_not_awaited()
        dispatch.assert_awaited_once_with(request, share, db, "video-archiver/videos")

    async def test_password_rate_limit_has_retry_after_header(self):
        request = make_request()

        with patch.object(
            sharing_router.redis_client,
            "eval",
            AsyncMock(return_value=[0, 27]),
        ):
            with self.assertRaises(HTTPException) as raised:
                await sharing_router._reserve_password_attempt(request, "share-id")

        self.assertEqual(429, raised.exception.status_code)
        self.assertEqual("27", raised.exception.headers["Retry-After"])

    async def test_password_attempt_is_reserved_before_threaded_bcrypt(self):
        request = make_request()
        share = cast(Any, SimpleNamespace(id="share-id", password_hash="password-hash"))
        operations = []

        async def reserve_attempt(*args):
            operations.append("reserve")
            return [1, 300]

        async def verify_in_thread(*args):
            operations.append("bcrypt")
            return True

        reserve = AsyncMock(side_effect=reserve_attempt)
        delete = AsyncMock()

        with (
            patch.object(sharing_router.redis_client, "eval", reserve),
            patch.object(sharing_router.redis_client, "delete", delete),
            patch.object(
                sharing_router.asyncio,
                "to_thread",
                AsyncMock(side_effect=verify_in_thread),
            ) as to_thread,
        ):
            valid = await sharing_router._check_password(request, share, "password")

        self.assertTrue(valid)
        reserve.assert_awaited_once()
        to_thread.assert_awaited_once_with(
            sharing_router.verify_password,
            "password",
            "password-hash",
        )
        self.assertEqual(["reserve", "bcrypt"], operations)
        delete.assert_awaited_once_with("share_attempts:share-id:127.0.0.1")

    async def test_revocation_marker_blocks_racing_session_creation(self):
        share = cast(
            Any,
            SimpleNamespace(
                id="share-id",
                expires_at=None,
                access_count=0,
                last_accessed_at=None,
            ),
        )
        db = AsyncMock()
        with patch.object(sharing_router.redis_client, "eval", AsyncMock(return_value=0)):
            with self.assertRaises(HTTPException) as raised:
                await sharing_router._establish_session(make_request(), share, db)

        self.assertEqual(404, raised.exception.status_code)
        db.commit.assert_not_awaited()

    async def test_revoke_atomically_clears_indexed_sessions(self):
        share = SimpleNamespace(id="share-id", status="active", revoked_at=None)
        db = AsyncMock()
        db.get.return_value = share
        evaluate = AsyncMock(return_value=2)

        with patch.object(sharing_router.redis_client, "eval", evaluate):
            result = await sharing_router.revoke_share("share-id", db, SimpleNamespace())

        self.assertEqual({"status": "revoked", "id": "share-id"}, result)
        evaluate.assert_awaited_once_with(
            CLEAR_SHARE_SESSIONS_SCRIPT,
            2,
            "share_sessions:share-id",
            "share_revoked:share-id",
            "share_session:",
        )

    async def test_revoke_all_marks_active_shares_and_clears_sessions(self):
        shares = [
            SimpleNamespace(id="share-a", status="active", revoked_at=None),
            SimpleNamespace(id="share-b", status="active", revoked_at=None),
        ]
        result_proxy = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: shares))
        db = AsyncMock()
        db.execute.return_value = result_proxy
        with patch.object(
            sharing_router,
            "_clear_share_sessions",
            AsyncMock(return_value=1),
        ) as clear_sessions:
            result = await sharing_router.revoke_all_shares(db, SimpleNamespace())

        self.assertEqual({"status": "revoked", "count": 2}, result)
        self.assertTrue(all(share.status == "revoked" for share in shares))
        db.commit.assert_awaited_once()
        self.assertEqual([call("share-a"), call("share-b")], clear_sessions.await_args_list)


class ShareProviderTests(unittest.TestCase):
    def test_video_provider_is_loaded_only_for_active_module(self):
        active = ModuleRegistry.discover({"video_archiver"})
        disabled = ModuleRegistry.discover(set())

        provider = active.share_provider("video_archiver")
        spec = active.share_spec("video_archiver")
        self.assertIsNotNone(provider)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual("video_ids", spec.selector_key)
        self.assertEqual(
            {"video_ids", "playlist_ids"},
            {item.selector_key for item in spec.declared_selection_types},
        )
        self.assertIsNone(disabled.share_provider("video_archiver"))

    def test_selected_video_outside_scope_is_hidden(self):
        provider = VideoShareProvider()
        share = SimpleNamespace(selection_mode="selected", selector={"video_ids": ["allowed"]})

        with (
            patch.object(provider, "_selected_videos", AsyncMock(return_value=[])),
            self.assertRaises(HTTPException) as raised,
        ):
            asyncio.run(provider._get_allowed_video(AsyncMock(), share, "other"))

        self.assertEqual(404, raised.exception.status_code)

    def test_music_and_video_accept_mixed_item_and_playlist_selection(self):
        def result(values):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

        music_db = AsyncMock()
        music_db.execute.side_effect = [result([3]), result([7]), result([4, 5])]
        music = asyncio.run(
            MusicShareProvider().selection(
                music_db,
                "selected",
                {"song_ids": ["3"], "playlist_ids": ["7"]},
            )
        )
        video_db = AsyncMock()
        video_db.execute.side_effect = [result(["video-1"]), result([9]), result(["video-2"])]
        video = asyncio.run(
            VideoShareProvider().selection(
                video_db,
                "selected",
                {"video_ids": ["video-1"], "playlist_ids": ["9"]},
            )
        )

        self.assertEqual(
            {"song_ids": [3, 4, 5], "playlist_ids": [7]},
            music,
        )
        self.assertEqual(
            {
                "video_ids": ["video-1", "video-2"],
                "playlist_ids": [9],
            },
            video,
        )

    def test_selected_playlists_expand_the_shared_media_scope(self):
        def result(values):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

        songs = [SimpleNamespace(id=2), SimpleNamespace(id=3)]
        music_db = AsyncMock()
        music_db.execute.return_value = result(songs)
        selected_songs = asyncio.run(
            _selected_songs(
                music_db,
                SimpleNamespace(
                    selection_mode="selected",
                    selector={"song_ids": [2, 3], "playlist_ids": [7]},
                ),
            )
        )

        videos = [SimpleNamespace(id="video-2"), SimpleNamespace(id="video-3")]
        video_db = AsyncMock()
        video_db.execute.return_value = result(videos)
        selected_videos = asyncio.run(
            VideoShareProvider()._selected_videos(
                video_db,
                SimpleNamespace(
                    selection_mode="selected",
                    selector={"video_ids": ["video-2", "video-3"], "playlist_ids": [9]},
                ),
            )
        )

        self.assertEqual([2, 3], [song.id for song in selected_songs])
        self.assertEqual(["video-2", "video-3"], [video.id for video in selected_videos])

    def test_sharing_ui_builds_multi_type_selectors_and_bulk_revoke(self):
        template = (ROOT / "app/modules/sharing/templates/shares_dashboard.html").read_text()

        self.assertIn("input.dataset.selectorKey", template)
        self.assertIn("revokeAllShares", template)
        self.assertIn("shares.filter(share => share.status === 'active')", template)

    def test_public_routes_do_not_depend_on_owner_auth(self):
        application_dependencies = {
            parameter.name for parameter in inspect.signature(shared_application).parameters.values()
        }
        api_dependencies = {
            parameter.name for parameter in inspect.signature(shared_module_api).parameters.values()
        }

        self.assertNotIn("user", application_dependencies)
        self.assertNotIn("user", api_dependencies)

    def test_shared_video_template_uses_only_scoped_resource_urls(self):
        template = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()
        provider = (ROOT / "app/modules/video_archiver/share.py").read_text()
        router = (ROOT / "app/modules/sharing/router.py").read_text()

        self.assertIn('extends module_base|default("base.html")', template)
        self.assertNotIn("handle_api", provider)
        self.assertNotIn("serve_asset", provider)
        self.assertIn('"/s/{share_id}/api/{path:path}"', router)
        self.assertFalse((ROOT / "app/modules/video_archiver/templates/shared_video.html").exists())


class SharedVideoUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_renders_owner_dashboard_with_scoped_urls(self):
        request = make_request()
        cases = (
            ("video_archiver", "/s/share-id/api/video-archiver/videos"),
            ("music", "/s/share-id/api/music/songs"),
            ("alllib", "/s/share-id/api/alllib/library"),
            ("vault", "/s/share-id/api/vault/items"),
        )
        for module_id, scoped_url in cases:
            with self.subTest(module=module_id):
                share = SimpleNamespace(id="share-id", module_id=module_id)
                response = await _render_shared_application(request, share)
                body = response.body.decode()
                self.assertIn(scoped_url, body)

    async def test_content_providers_reject_empty_selected_scope(self):
        providers = (
            (VideoShareProvider(), "video_ids"),
            (MusicShareProvider(), "song_ids"),
            (AllLibShareProvider(), "media_ids"),
            (VaultShareProvider(), "item_ids"),
        )
        for provider, selector_key in providers:
            with self.subTest(selector=selector_key), self.assertRaises(HTTPException) as raised:
                await provider.selection(AsyncMock(), "selected", {selector_key: []})
            self.assertEqual(422, raised.exception.status_code)

    async def test_core_rejects_mutations_before_provider_dispatch(self):
        request = make_request()
        request.scope["method"] = "DELETE"
        share = SimpleNamespace(id="share-id", module_id="video_archiver")

        with patch.object(sharing_router, "_provider") as provider:
            response = await _dispatch_shared_api(
                request,
                share,
                AsyncMock(),
                "video-archiver/videos/video-id",
            )

        provider.assert_not_called()
        self.assertEqual(403, response.status_code)

    async def test_core_dispatches_only_declared_routes(self):
        request = make_request()
        share = SimpleNamespace(id="share-id", module_id="video_archiver")
        provider = SimpleNamespace(entities=AsyncMock(return_value=[{"id": "video-id"}]))

        with patch.object(sharing_router, "_provider", return_value=provider):
            response = await _dispatch_shared_api(
                request,
                share,
                AsyncMock(),
                "video-archiver/videos",
            )

        self.assertEqual(200, response.status_code)
        provider.entities.assert_awaited_once()
        with self.assertRaises(HTTPException) as raised:
            await _dispatch_shared_api(
                request,
                share,
                AsyncMock(),
                "video-archiver/undeclared",
            )
        self.assertEqual(404, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
