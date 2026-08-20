import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request

from app.core.config import Settings, validate_runtime_security
from app.core.encryption_keys import legacy_encryption_keys, primary_encryption_key
from app.core.http_security import is_cross_site_request
from app.core.remote_fetch import RemoteFetchError, host_in_allowlist, validate_remote_url
from app.core.responses import serve_media_stream
from app.core.secret_values import (
    SECRET_PREFIX,
    decrypt_secret_value,
    encrypt_secret_value,
    rotate_secret_value,
)
from app.core.security import create_api_session, get_current_user
from app.core.storage import ENCRYPTED_FILE_MAGIC, LocalStorage
from app.modules.alllib.api import LibParser
from app.modules.alllib.router import _create_pairing_code, _is_allowed_lib_url, _verify_pairing_code
from app.modules.music.security import validate_music_url
from app.modules.vault.services import _is_public_http_url
from app.modules.video_archiver.providers import GenericProvider, PlatformRegistry


class AttributeCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, tag, attrs):
        self.attributes.update(attrs)


class LibParserSecurityTests(unittest.TestCase):
    def setUp(self):
        self.parser = LibParser()

    def test_text_and_link_attributes_are_escaped(self):
        content = [
            {
                "type": "text",
                "text": "<script>alert(1)</script>",
                "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}],
            }
        ]

        rendered = self.parser.json_to_html(content, [])

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("href='#'", rendered)

    def test_image_drops_untrusted_attributes(self):
        content = [
            {
                "type": "image",
                "attrs": {
                    "src": "https://img.cdnlibs.org/page.jpg",
                    "alt": 'cover" onerror="alert(1)',
                    "onerror": "alert(1)",
                },
            }
        ]

        rendered = self.parser.json_to_html(content, [])
        collector = AttributeCollector()
        collector.feed(rendered)

        self.assertNotIn("onerror", collector.attributes)
        self.assertIn("&quot; onerror=&quot;", rendered)


class ExternalFetchSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_image_proxy_only_accepts_known_https_hosts(self):
        self.assertTrue(_is_allowed_lib_url("https://img.cdnlibs.org/page.jpg"))
        self.assertTrue(_is_allowed_lib_url("https://ranobelib.me/uploads/page.jpg"))
        self.assertTrue(_is_allowed_lib_url("https://ranobehub.org/api/media/1"))
        self.assertTrue(_is_allowed_lib_url("https://ranobe.space/api/media/1"))
        self.assertTrue(_is_allowed_lib_url("https://uploads.mangadex.org/covers/id/file.jpg"))
        self.assertTrue(_is_allowed_lib_url("https://novel-bin.net/files/image/book.jpg"))
        self.assertFalse(_is_allowed_lib_url("http://cdnlibs.org/page.jpg"))
        self.assertFalse(_is_allowed_lib_url("https://cdnlibs.org.example.com/page.jpg"))
        self.assertFalse(_is_allowed_lib_url("https://127.0.0.1/admin"))

    async def test_pairing_code_is_short_lived_and_single_use(self):
        with patch("app.modules.alllib.router.redis_client") as redis_client:
            redis_client.setex = AsyncMock()
            redis_client.getdel = AsyncMock(side_effect=["1", None, None])
            pairing_code = await _create_pairing_code()

            self.assertTrue(await _verify_pairing_code(pairing_code))
            self.assertFalse(await _verify_pairing_code(pairing_code))
            self.assertFalse(await _verify_pairing_code("invalid"))
            redis_client.setex.assert_awaited_once()

    async def test_vault_metadata_fetch_rejects_private_addresses(self):
        self.assertFalse(await _is_public_http_url("http://127.0.0.1/admin"))
        self.assertFalse(await _is_public_http_url("http://[::1]/admin"))
        self.assertFalse(await _is_public_http_url("file:///etc/passwd"))

    def test_remote_host_matching_rejects_suffix_confusion_and_private_ips(self):
        self.assertTrue(host_in_allowlist("i.ytimg.com", {"ytimg.com"}))
        self.assertFalse(host_in_allowlist("youtube.com.evil.example", {"youtube.com"}))
        with self.assertRaises(RemoteFetchError):
            validate_remote_url("https://127.0.0.1/private", resolve=True)

    def test_downloaders_reject_unknown_hosts(self):
        self.assertIsInstance(PlatformRegistry.get_provider("https://example.com/video"), GenericProvider)
        with self.assertRaises(ValueError):
            PlatformRegistry.require_supported_url("https://youtube.com.evil.example/video")
        with self.assertRaises(ValueError):
            validate_music_url("https://soundcloud.com.evil.example/track", resolve=False)


class CoreBoundarySecurityTests(unittest.TestCase):
    @staticmethod
    def request(method: str, host: str, origin: str | None = None, fetch_site: str | None = None):
        headers = [(b"host", host.encode())]
        if origin:
            headers.append((b"origin", origin.encode()))
        if fetch_site:
            headers.append((b"sec-fetch-site", fetch_site.encode()))
        return Request(
            {
                "type": "http",
                "method": method,
                "path": "/api/shares",
                "headers": headers,
                "query_string": b"",
                "scheme": "https",
                "server": (host, 443),
                "client": ("127.0.0.1", 1234),
            }
        )

    def test_cross_site_mutations_are_rejected(self):
        self.assertTrue(
            is_cross_site_request(
                self.request("POST", "sanctum.example", "https://evil.example", "cross-site")
            )
        )
        self.assertFalse(
            is_cross_site_request(
                self.request("POST", "sanctum.example", "https://sanctum.example", "same-origin")
            )
        )
        capability_request = self.request(
            "POST",
            "sanctum.example",
            "https://evil.example",
            "cross-site",
        )
        capability_request.scope["path"] = "/s/example/access"
        self.assertFalse(is_cross_site_request(capability_request))

    def test_secret_values_are_encrypted_and_authenticated(self):
        encrypted = encrypt_secret_value("sensitive-value")

        self.assertTrue(encrypted.startswith(SECRET_PREFIX))
        self.assertNotIn("sensitive-value", encrypted)
        self.assertEqual("sensitive-value", decrypt_secret_value(encrypted))

    def test_known_legacy_master_ciphertext_is_rotated(self):
        import base64
        import hashlib
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        legacy_key = hashlib.sha256(b"dev-api-key-change-me").digest()
        legacy = SECRET_PREFIX + base64.urlsafe_b64encode(
            nonce + AESGCM(legacy_key).encrypt(nonce, b"legacy-secret", None)
        ).decode("ascii")

        rotated = rotate_secret_value(legacy)
        self.assertEqual("legacy-secret", decrypt_secret_value(rotated))
        self.assertNotEqual(legacy, rotated)

    def test_owner_auth_no_longer_accepts_query_tokens(self):
        source = (Path(__file__).resolve().parents[1] / "app/core/security.py").read_text()

        self.assertNotIn('query_params.get("token")', source)

    def test_private_share_secret_is_fragment_bootstrapped(self):
        root = Path(__file__).resolve().parents[1]
        router = (root / "app/modules/sharing/router.py").read_text()
        bootstrap = (root / "static/share-bootstrap.js").read_text()

        self.assertIn('f"/s/{share.id}#{secret}"', router)
        self.assertNotIn("/access/{secret}", router)
        self.assertIn("window.location.hash.slice(1)", bootstrap)

    def test_lib_token_is_not_sent_in_query_string(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "app/modules/alllib/templates/alllib_dashboard.html").read_text()

        self.assertNotIn("&token=${encodeURIComponent(tokenVal)}", dashboard)
        self.assertIn("body: JSON.stringify({url, token: tokenVal || null})", dashboard)

    def test_sensitive_routes_require_owner_auth(self):
        root = Path(__file__).resolve().parents[1]
        alllib = (root / "app/modules/alllib/router.py").read_text()

        cover_block = alllib[alllib.index("async def get_cover(") : alllib.index("async def get_page(")]
        self.assertIn("user=Depends(get_current_user)", cover_block)

    def test_default_compose_is_local_and_rootless(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / "docker-compose.yml").read_text()
        dockerfile = (root / "Dockerfile").read_text()

        self.assertIn("127.0.0.1:${HOST_PORT:-8000}:8000", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("USER netsanctum", dockerfile)
        self.assertIn("APP_UID", dockerfile)

        parsed = yaml.safe_load(compose)
        self.assertNotIn("ports", parsed["services"]["youtube-pot"])
        self.assertTrue(parsed["services"]["youtube-pot"]["read_only"])
        worker_command = parsed["services"]["worker"]["command"]
        self.assertEqual("sh", worker_command[0])
        self.assertIn("worker --loglevel=info --concurrency=2", worker_command[2])
        self.assertEqual("0", parsed["services"]["worker"]["environment"]["NETSANCTUM_LOAD_DOTENV"])
        self.assertEqual(
            "production",
            parsed["services"]["worker"]["environment"]["NETSANCTUM_ENVIRONMENT"],
        )
        self.assertNotIn(".:/app:ro", parsed["services"]["web"]["volumes"])
        self.assertNotIn(".:/app:ro", parsed["services"]["worker"]["volumes"])
        self.assertIn("encryption_key:/run/netsanctum-key:ro", parsed["services"]["web"]["volumes"])
        self.assertIn(
            "encryption_key:/run/netsanctum-key:ro",
            parsed["services"]["worker"]["volumes"],
        )
        self.assertIn("Encryption key volume is missing", parsed["services"]["storage-init"]["command"][2])
        storage_init = parsed["services"]["storage-init"]
        self.assertEqual("0:0", storage_init["user"])
        self.assertIn("chown -R", storage_init["command"][2])
        self.assertEqual(
            "service_completed_successfully",
            parsed["services"]["web"]["depends_on"]["storage-init"]["condition"],
        )

        start_script = (root / "start.sh").read_text()
        self.assertNotIn("mkdir -p storage/config", start_script)

    def test_container_environment_does_not_read_unreadable_dotenv(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text("MASTER_API_KEY=must-not-be-read\n")
            dotenv.chmod(0)
            environment = os.environ.copy()
            environment["NETSANCTUM_LOAD_DOTENV"] = "0"
            environment["MASTER_API_KEY"] = "injected-environment-value"
            environment["PYTHONPATH"] = str(root)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from app.core.config import Settings; print(Settings().MASTER_API_KEY)",
                ],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual("injected-environment-value", result.stdout.strip())

    def test_production_rejects_known_or_shared_secrets(self):
        unsafe = Settings(
            NETSANCTUM_ENVIRONMENT="production",
            MASTER_API_KEY="dev-api-key-change-me",
            FILE_ENCRYPTION_KEY="dev-api-key-change-me",
        )

        with self.assertRaisesRegex(RuntimeError, "Unsafe production configuration"):
            validate_runtime_security(unsafe)

    def test_https_production_requires_secure_cookies(self):
        unsafe = Settings(
            NETSANCTUM_ENVIRONMENT="production",
            MASTER_API_KEY="a" * 32,
            FILE_ENCRYPTION_KEY="b" * 32,
            DATABASE_URL="postgresql+asyncpg://app:secret@postgres/app",
            DATABASE_URL_SYNC="postgresql+psycopg2://app:secret@postgres/app",
            PUBLIC_BASE_URL="https://sanctum.example",
            SECURE_COOKIES=False,
        )

        with self.assertRaisesRegex(RuntimeError, "SECURE_COOKIES"):
            validate_runtime_security(unsafe)

    def test_generated_key_file_is_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            first = Settings(
                FILE_ENCRYPTION_KEY_PATH=str(key_path),
                FILE_ENCRYPTION_KEY="legacy-a" * 8,
            )
            second = Settings(
                FILE_ENCRYPTION_KEY_PATH=str(key_path),
                FILE_ENCRYPTION_KEY="legacy-b" * 8,
            )

            self.assertEqual(primary_encryption_key(first), primary_encryption_key(second))
            self.assertNotEqual(
                primary_encryption_key(first, purpose="files"),
                primary_encryption_key(first, purpose="settings"),
            )
            self.assertIn(
                hashlib.sha256(first.FILE_ENCRYPTION_KEY.encode()).digest(),
                legacy_encryption_keys(first),
            )

    def test_recovered_legacy_keys_are_loaded_from_protected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            legacy_path = Path(directory) / "legacy.keys"
            legacy_path.write_text("recovered-old-key\n")
            settings = Settings(
                FILE_ENCRYPTION_KEY_PATH=str(key_path),
                LEGACY_FILE_ENCRYPTION_KEYS_PATH=str(legacy_path),
            )

            self.assertIn(
                hashlib.sha256(b"recovered-old-key").digest(),
                legacy_encryption_keys(settings),
            )

    def test_unreadable_migration_retries_when_recovery_key_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            root.mkdir()
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            legacy_path = Path(directory) / "legacy.keys"
            settings = Settings(
                FILE_ENCRYPTION_KEY_PATH=str(key_path),
                LEGACY_FILE_ENCRYPTION_KEYS_PATH=str(legacy_path),
            )
            recovered_value = "recovered-old-key"
            recovered_key = hashlib.sha256(recovered_value.encode()).digest()
            nonce = os.urandom(12)
            (root / "recovered.enc").write_bytes(
                nonce + AESGCM(recovered_key).encrypt(nonce, b"recovered-content", None)
            )

            with patch("app.core.encryption_keys.get_settings", return_value=settings):
                storage = LocalStorage(str(root))
                first = storage.migrate_legacy_encryption_batch(limit=1)
                legacy_path.write_text(recovered_value + "\n")
                second = storage.migrate_legacy_encryption_batch(limit=1)

            self.assertEqual(1, first.unreadable)
            self.assertEqual(1, second.migrated)
            self.assertEqual(0, second.unreadable)

    def test_legacy_file_is_rotated_to_generated_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            root.mkdir()
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            settings = Settings(
                FILE_ENCRYPTION_KEY_PATH=str(key_path),
                FILE_ENCRYPTION_KEY="legacy-file-key-material" * 2,
            )
            nonce = os.urandom(12)
            legacy_key = hashlib.sha256(settings.FILE_ENCRYPTION_KEY.encode()).digest()
            (root / "sample.enc").write_bytes(
                nonce + AESGCM(legacy_key).encrypt(nonce, b"protected-content", None)
            )

            with patch("app.core.encryption_keys.get_settings", return_value=settings):
                storage = LocalStorage(str(root))
                self.assertEqual(1, storage.migrate_legacy_encryption())
                self.assertTrue((root / "sample.enc").read_bytes().startswith(ENCRYPTED_FILE_MAGIC))
                self.assertEqual(b"protected-content", storage.get_file_decrypted("sample.enc"))
                self.assertEqual(0, storage.migrate_legacy_encryption())

    def test_legacy_migration_is_bounded_and_preserves_unreadable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            root.mkdir()
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            settings = Settings(
                FILE_ENCRYPTION_KEY_PATH=str(key_path),
                FILE_ENCRYPTION_KEY="legacy-file-key-material" * 2,
            )
            legacy_key = hashlib.sha256(settings.FILE_ENCRYPTION_KEY.encode()).digest()
            for index in range(2):
                nonce = os.urandom(12)
                (root / f"legacy-{index}.enc").write_bytes(
                    nonce + AESGCM(legacy_key).encrypt(nonce, f"content-{index}".encode(), None)
                )
            unreadable = root / "unreadable.enc"
            unreadable.write_bytes(os.urandom(64))

            with patch("app.core.encryption_keys.get_settings", return_value=settings):
                storage = LocalStorage(str(root))
                first = storage.migrate_legacy_encryption_batch(limit=1)
                second = storage.migrate_legacy_encryption_batch(limit=1)
                final = storage.migrate_legacy_encryption_batch(limit=1)

            self.assertEqual(1, first.migrated)
            self.assertEqual(1, second.migrated)
            self.assertEqual(0, final.migrated)
            self.assertEqual(1, final.unreadable)
            self.assertEqual(2, final.current)
            self.assertEqual(64, unreadable.stat().st_size)

    def test_v2_ciphertext_is_bound_to_its_storage_path(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            settings = Settings(FILE_ENCRYPTION_KEY_PATH=str(key_path))

            with patch("app.core.encryption_keys.get_settings", return_value=settings):
                storage = LocalStorage(str(Path(directory) / "storage"))
                storage.save_file_encrypted(b"path-bound", "first.enc")
                self.assertEqual(10, storage.get_encrypted_plaintext_size("first.enc"))
                (storage._full_path("second.enc")).write_bytes(storage._full_path("first.enc").read_bytes())
                with self.assertRaises(ValueError):
                    storage.get_file_decrypted("second.enc")

    def test_encrypted_media_ranges_use_plaintext_size(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "file.key"
            key_path.write_bytes(b"generated-runtime-key-material" * 2)
            settings = Settings(FILE_ENCRYPTION_KEY_PATH=str(key_path))
            request = self.request("GET", "sanctum.example")
            request.scope["headers"].append((b"range", b"bytes=2-5"))

            with patch("app.core.encryption_keys.get_settings", return_value=settings):
                storage = LocalStorage(str(Path(directory) / "storage"))
                storage.save_file_encrypted(b"0123456789", "media.enc")
                with patch("app.core.responses.get_storage", return_value=storage):
                    response = serve_media_stream(request, "media.enc")

            self.assertEqual(206, response.status_code)
            self.assertEqual("bytes 2-5/10", response.headers["content-range"])
            self.assertEqual("4", response.headers["content-length"])


class SessionSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_owner_token_is_not_accepted_as_bearer(self):
        request = CoreBoundarySecurityTests.request("GET", "sanctum.example")
        request.scope["headers"].append((b"authorization", b"Bearer raw-owner-token"))

        with (
            patch("app.core.security.verify_access_token", return_value=True),
            patch("app.core.security.redis_client.get", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException),
        ):
            await get_current_user(request)

    async def test_api_session_is_stored_by_hash(self):
        with patch("app.core.security.redis_client.setex", new=AsyncMock()) as setex:
            token = await create_api_session()

        key = setex.await_args.args[0]
        self.assertNotIn(token, key)
        self.assertTrue(key.startswith("api-session:"))


class LocalStorageSecurityTests(unittest.TestCase):
    def test_similarly_prefixed_sibling_is_not_inside_storage_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "storage"
            storage = LocalStorage(str(root))

            with self.assertRaises(ValueError):
                storage.save_file(b"secret", "../storage-backup/escaped.txt")


if __name__ == "__main__":
    unittest.main()
