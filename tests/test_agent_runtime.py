import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from fastapi import HTTPException

from app.core.agent.primitives import AgentFetchResult
from app.core.agent_runtime import _local_or_saved, app, verify_runtime_token

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
START_SH = Path(__file__).resolve().parents[1] / "start.sh"


class AgentRuntimeIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load(COMPOSE.read_text())

    def test_storage_can_be_handed_back_without_root(self):
        """A locked storage directory has to have a way out that needs no root.

        The stack hands ./storage to a uid and then locks it to that uid, so a host
        whose user is a different number is left unable to read its own data. The
        repair runs as a root container of its own, which is the only thing here
        allowed to do that, and it must not bring the stack up on the way.
        """
        script = START_SH.read_text()
        self.assertIn("--chown-abort|chown-abort)", script)
        self.assertIn('ACTION="chown-abort"', script)
        # Root, and only for the one-shot: a repair that quietly started the stack
        # would rebuild images on a machine that is asking for a permission fix.
        self.assertIn("--user 0:0", script)
        block = script[script.index('if [ "$ACTION" = "chown-abort" ]') :]
        block = block[: block.index("exit 0")]
        self.assertNotIn("docker compose up", block)
        # Both halves of the repair, or the directory comes back owned by the user
        # and still unreadable by anyone else who has to inspect it.
        self.assertIn("chown -R", block)
        self.assertIn("chmod -R a+rwX", block)
        # And the argument is reachable: the flag has to be in the usage line, since
        # a repair nobody can find is not a way out.
        self.assertIn("--chown-abort", script.split("Usage: ./start.sh")[-1][:200])

    def test_agent_runtime_is_opt_in_and_hardened(self):
        service = self.compose["services"]["agent-runtime"]
        self.assertEqual(["agent"], service["profiles"])
        self.assertTrue(service["read_only"])
        self.assertEqual(["ALL"], service["cap_drop"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertTrue(service["mem_limit"])
        self.assertTrue(service["pids_limit"])
        self.assertEqual(
            ["agent-control", "model-control", "agent-egress"],
            service["networks"],
        )
        self.assertIn("app.core.agent_runtime:app", service["command"])

    def test_agent_networks_are_separate_from_the_database(self):
        self.assertTrue(self.compose["networks"]["agent-control"]["internal"])
        self.assertFalse((self.compose["networks"].get("agent-egress") or {}).get("internal", False))
        for name in ("postgres", "redis"):
            networks = self.compose["services"][name]["networks"]
            self.assertNotIn("agent-control", networks, name)
            self.assertNotIn("agent-egress", networks, name)
        agent_networks = self.compose["services"]["agent-runtime"]["networks"]
        for forbidden in ("backend", "media-control", "browser-control", "default"):
            self.assertNotIn(forbidden, agent_networks)
        # A model is reachable only from the services that drive it. The voice models
        # sit here too, and only the runtime ever speaks to them.
        model_clients = [
            name
            for name, service in self.compose["services"].items()
            if "model-control" in (service.get("networks") or [])
        ]
        self.assertEqual(
            ["agent-runtime", "miku-llm", "miku-stt", "miku-voice"],
            sorted(model_clients),
        )

    def test_only_web_reaches_the_agent_runtime(self):
        peers = [
            name
            for name, service in self.compose["services"].items()
            if "agent-control" in (service.get("networks") or [])
        ]
        self.assertEqual(["agent-runtime", "web"], sorted(peers))
        web = self.compose["services"]["web"]
        self.assertNotIn("agent-egress", web["networks"])
        self.assertIn("AGENT_INTERNAL_KEY", web["environment"])

    def test_model_is_fetched_on_first_run(self):
        init = self.compose["services"].get("model-init")
        self.assertIsNotNone(init, "the model must be fetched automatically")
        self.assertEqual(["miku-local"], init["profiles"])
        self.assertEqual("no", init["restart"])
        # The fetch lives in a script so it can be tested without Docker: the Alpine
        # image only offers BusyBox tools, and a flag it does not know aborts the
        # download before a single byte moves.
        script = (START_SH.parent / "scripts" / "fetch_model.sh").read_text()
        self.assertIn("wget", script)
        self.assertIn("chown", script)
        self.assertIn("already present", script)
        self.assertEqual(["sh", "/fetch-model.sh"], init["entrypoint"])
        self.assertIn("./scripts/fetch_model.sh:/fetch-model.sh:ro", init["volumes"])
        self.assertNotIn("read_only", init)
        model = self.compose["services"]["miku-llm"]
        depends = model["depends_on"]
        if isinstance(depends, list):
            conditions = {name: item.get("condition") for entry in depends for name, item in entry.items()}
        else:
            conditions = {name: item.get("condition") for name, item in (depends or {}).items()}
        self.assertEqual("service_completed_successfully", conditions.get("model-init"))

    def test_a_server_without_a_gpu_needs_no_device_mapping(self):
        """GPU lives in an override so a headless server can start the same stack."""
        base = yaml.safe_load(COMPOSE.read_text())
        self.assertNotIn("devices", base["services"]["miku-llm"])
        gpu = yaml.safe_load((COMPOSE.parent / "docker-compose.gpu.yml").read_text())
        self.assertIn("/dev/dri", gpu["services"]["miku-llm"]["devices"][0])
        script = START_SH.read_text()
        self.assertIn("docker-compose.gpu.yml", script)
        self.assertIn("/dev/dri", script)

    def test_base_build_stays_untouched_without_the_flag(self):
        script = START_SH.read_text()
        self.assertIn("--no-agent", script)
        self.assertIn("--profile agent", script)
        # the assistant is the product: its runtime is on unless explicitly disabled
        self.assertIn("AGENT_RUNTIME=1", script)
        self.assertNotIn("miku-runtime", script)

    def test_voice_is_opt_in_and_actually_released(self):
        # The browser does its own speech when the server is not asked to. Leaving the
        # model loaded would make that mode cost memory it claims not to cost, so the
        # services are stopped and removed rather than merely left unused.
        for name in ("miku-voice", "miku-stt"):
            service = self.compose["services"][name]
            self.assertEqual(["voice"], service["profiles"], name)
            # Capped so the memory budget is a fact rather than a hope.
            self.assertTrue(service["mem_limit"], name)
        # Only the runtime may reach them, so they sit on the internal model network.
        # Recognition never needs the internet and has no reason to hold a route out.
        self.assertEqual(["model-control"], list(self.compose["services"]["miku-stt"]["networks"]))
        # Synthesis does need one, and only because the online voice is asked before
        # the local engines. It is a network of its own so the model network stays
        # internal: nothing else on model-control gains a route out by this.
        self.assertEqual(
            ["model-control", "voice-egress"],
            list(self.compose["services"]["miku-voice"]["networks"]),
        )
        self.assertTrue(self.compose["networks"]["model-control"].get("internal"))
        # A network declared with no options parses as None, which is how every other
        # egress network in the file is written: no internal flag means it routes out.
        self.assertIsNone(self.compose["networks"]["voice-egress"])
        # The one-shot fetcher needs no network at all: it only writes to the volume.
        fetcher = self.compose["services"]["voice-init"]
        self.assertEqual(["voice"], fetcher["profiles"])
        self.assertNotIn("networks", fetcher)

        script = START_SH.read_text()
        self.assertIn("VOICE=0", script)
        self.assertIn("--voice)", script)
        self.assertIn("PROFILE_ARGS+=(--profile voice)", script)
        stop = script.index("stop miku-voice miku-stt voice-init")
        self.assertIn("rm -f miku-voice miku-stt voice-init", script)
        # The release has to happen on the way past, not only inside the enabled branch.
        self.assertLess(script.index('if [ "$VOICE" = "1" ]; then\n    PROFILE_ARGS'), stop)

    def test_the_voice_profile_fetches_the_models_it_needs(self):
        manifest = self.compose["services"]["voice-init"]["environment"]["VOICE_MANIFEST"]
        entries = {}
        for line in manifest.strip().splitlines():
            if not line.strip():
                continue
            name, url, magic = line.split("|")
            entries[name] = (url, magic)
        # Recognition: the multilingual base. The English-only variant is the same
        # container and the same size, so nothing else would notice the swap - and
        # Russian recognition degrades badly.
        self.assertIn("ggml-base.bin", entries)
        self.assertNotIn("ggml-base.en.bin", entries)
        self.assertFalse([name for name in entries if ".en." in name])
        # Synthesis, one engine per language.
        self.assertIn("v5_ru_ru.pt", entries)
        self.assertIn("kokoro-v1.0.int8.onnx", entries)
        self.assertIn("voices-v1.0.bin", entries)
        # Every entry declares the magic its container must start with.
        for name, (_url, magic) in entries.items():
            self.assertIn(magic, {"GGML", "GGUF", "ONNX", "PK", "none"}, name)

    def test_env_gains_settings_a_pulled_branch_introduced(self):
        script = START_SH.read_text()
        # An .env written before this branch lacks MIKU_MODEL_*, and the one-shot model
        # fetcher then exits 1 with no explanation. start.sh copies the documented
        # defaults across so an upgrade cannot half-start the stack.
        self.assertIn("while IFS= read -r EXAMPLE_LINE", script)
        self.assertIn('grep -q "^${EXAMPLE_KEY}=" "$ENV_FILE"', script)
        self.assertIn("Added missing settings from .env.example", script)

    def test_env_backfill_never_copies_a_published_secret(self):
        script = START_SH.read_text()
        self.assertIn("*change_me*|dev-*) continue ;;", script)
        self.assertIn('if [ -z "$EXAMPLE_VALUE" ]; then', script)
        # The backfill has to run before the secret pass, or a copied placeholder
        # would survive as a real password or file encryption key.
        self.assertLess(
            script.index("while IFS= read -r EXAMPLE_LINE"),
            script.index("AGENT_SECRET_NAME in AGENT_RUNTIME_TOKEN"),
        )

    def test_local_model_is_checked_before_compose_runs(self):
        script = START_SH.read_text()
        self.assertIn("MIKU_MODEL_FILE", script)
        self.assertIn("MIKU_MODEL_URL", script)
        self.assertIn("has less than 2 GB free", script)
        self.assertLess(
            script.index("has less than 2 GB free"),
            script.index('echo "Building and launching Docker services..."'),
        )


class ProviderSelectionTests(unittest.TestCase):
    def test_local_mode_serves_the_runtime_model_not_a_saved_url(self):
        url, model, key = _local_or_saved(
            "local",
            "https://external/v1",
            "gemini",
            "secret",
            "http://miku-llm:8080/v1",
            "qwen",
            "",
        )
        self.assertEqual("http://miku-llm:8080/v1", url)
        self.assertEqual("qwen", model)
        self.assertEqual("", key)

    def test_api_mode_uses_the_saved_provider(self):
        url, model, key = _local_or_saved(
            "api",
            "https://external/v1",
            "gemini",
            "secret",
            "http://miku-llm:8080/v1",
            "qwen",
            "",
        )
        self.assertEqual("https://external/v1", url)
        self.assertEqual("gemini", model)
        self.assertEqual("secret", key)


class AgentRuntimeServiceTests(unittest.TestCase):
    def _post(self, path: str, payload: dict, token: str = "runtime-secret"):
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                return await client.post(
                    path,
                    headers={"X-Agent-Runtime-Token": token},
                    json=payload,
                )

        return asyncio.run(call())

    def test_token_is_required(self):
        with patch("app.core.agent_runtime.RUNTIME_TOKEN", "runtime-secret"):
            verify_runtime_token("runtime-secret")
            with self.assertRaises(HTTPException):
                verify_runtime_token("wrong")
        with patch("app.core.agent_runtime.RUNTIME_TOKEN", ""):
            with self.assertRaises(HTTPException):
                verify_runtime_token("")

    def test_health_needs_no_token(self):
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                return await client.get("/health")

        response = asyncio.run(call())
        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.json()["status"])

    def test_fetch_rejects_unsafe_urls_with_a_sanitized_error(self):
        with patch("app.core.agent_runtime.RUNTIME_TOKEN", "runtime-secret"):
            response = self._post("/v1/fetch", {"url": "https://127.0.0.1/admin"})
        self.assertEqual(422, response.status_code)
        self.assertEqual("The URL could not be read", response.json()["detail"])

    def test_fetch_returns_readable_text(self):
        expected = AgentFetchResult(
            url="https://example.org/a",
            final_url="https://example.org/a",
            title="Глава 3",
            content_type="text/html",
            text="Первый абзац.",
            bytes_read=120,
        )
        with (
            patch("app.core.agent_runtime.RUNTIME_TOKEN", "runtime-secret"),
            patch("app.core.agent_runtime.fetch_public_text", _async_return(expected)),
        ):
            response = self._post("/v1/fetch", {"url": "https://example.org/a"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("Первый абзац.", response.json()["text"])

    def test_tools_proxy_requires_a_reachable_application(self):
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                return await client.get("/v1/tools", headers={"X-Agent-Runtime-Token": "runtime-secret"})

        with patch("app.core.agent_runtime.RUNTIME_TOKEN", "runtime-secret"):
            response = asyncio.run(call())
        self.assertEqual(503, response.status_code)

    def test_tools_proxy_returns_the_application_catalog(self):
        catalog = {"consumer_id": "miku", "tools": [{"name": "read"}]}

        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                return await client.get("/v1/tools", headers={"X-Agent-Runtime-Token": "runtime-secret"})

        with (
            patch("app.core.agent_runtime.RUNTIME_TOKEN", "runtime-secret"),
            patch("app.core.agent_runtime.INTERNAL_KEY", "internal-secret"),
            patch("app.core.agent_runtime.WEB_INTERNAL_URL", "http://web:8000"),
            patch("app.core.agent_runtime.httpx.AsyncClient", _catalog_client(catalog)),
        ):
            response = asyncio.run(call())
        self.assertEqual(200, response.status_code)
        self.assertEqual(catalog, response.json())


def _catalog_client(catalog: dict):
    """Stand in for the web process the sidecar talks to."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(
        return_value=httpx.Response(200, json=catalog, request=httpx.Request("GET", "http://web"))
    )
    return MagicMock(return_value=client)


def _async_return(value):
    async def call(*args, **kwargs):
        return value

    return call


if __name__ == "__main__":
    unittest.main()
