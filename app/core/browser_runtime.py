import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.module_types import BrowserPolicySpec, browser_policy_fingerprint

MAX_LIVE_SESSIONS = 2
VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class BrowserSession:
    id: str
    module_id: str
    policy: BrowserPolicySpec
    playwright: Any
    browser: Any
    context: Any
    page: Any
    xvfb: asyncio.subprocess.Process | None
    display: str | None
    mode: str
    last_activity: float
    watchdog: asyncio.Task | None = None
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserRuntime:
    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _allowed_url(policy: BrowserPolicySpec, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "blob", "data"}:
            return True
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme in {"https", "wss"} and any(
            host == allowed or host.endswith(f".{allowed}") for allowed in policy.allowed_hosts
        )

    async def start(
        self,
        module_id: str,
        policy: BrowserPolicySpec,
        *,
        locale: str = "en-US",
        restore_snapshot: bool = True,
        mode: str = "interactive",
        storage_state: dict | None = None,
    ) -> dict:
        async with self._lock:
            if mode not in policy.allowed_modes:
                raise ValueError(f"Browser policy does not allow {mode!r} mode")
            for session_id, session in list(self._sessions.items()):
                if session.policy.id == policy.id:
                    await self._close_locked(session_id)
            if len(self._sessions) >= MAX_LIVE_SESSIONS:
                raise RuntimeError("Browser session limit reached")
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError("Browser runtime dependencies are not installed") from exc

            display = None
            xvfb = None
            if mode == "interactive":
                used_displays = {session.display for session in self._sessions.values()}
                display = next(
                    f":{number}"
                    for number in range(99, 99 + MAX_LIVE_SESSIONS)
                    if f":{number}" not in used_displays
                )
                xvfb = await asyncio.create_subprocess_exec(
                    "Xvfb",
                    display,
                    "-screen",
                    "0",
                    f"{VIEWPORT['width']}x{VIEWPORT['height']}x24",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            playwright = None
            try:
                if xvfb:
                    await asyncio.sleep(0.25)
                    if xvfb.returncode is not None:
                        raise RuntimeError("Could not start the isolated browser display")
                playwright = await async_playwright().start()
                browser_env = {
                    "HOME": "/tmp",
                    "LANG": locale,
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                }
                if display:
                    browser_env["DISPLAY"] = display
                proxy_url = os.getenv("BROWSER_PROXY_URL")
                browser_args = [
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-crash-reporter",
                    "--disable-crashpad",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ]
                if proxy_url:
                    browser_args.extend(
                        (
                            f"--proxy-server={proxy_url}",
                            "--proxy-bypass-list=<-loopback>",
                        )
                    )
                browser = await playwright.chromium.launch(
                    executable_path=os.getenv("CHROMIUM_EXECUTABLE", "/usr/bin/chromium"),
                    headless=mode == "headless",
                    chromium_sandbox=True,
                    env=browser_env,
                    args=browser_args,
                )
                context = await browser.new_context(
                    viewport=VIEWPORT,
                    locale=locale,
                    storage_state=storage_state if restore_snapshot else None,
                    service_workers="allow",
                )
                await context.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });
""")

                async def route_request(route):
                    if self._allowed_url(policy, route.request.url):
                        await route.continue_()
                    else:
                        await route.abort()

                await context.route("**/*", route_request)

                async def route_web_socket(web_socket):
                    if self._allowed_url(policy, web_socket.url):
                        web_socket.connect_to_server()
                    else:
                        await web_socket.close(code=1008, reason="Blocked by browser policy")

                await context.route_web_socket("**/*", route_web_socket)
                page = await context.new_page()
                await page.goto(policy.start_url, wait_until="domcontentloaded", timeout=60000)
            except BaseException:
                if playwright:
                    await playwright.stop()
                if xvfb and xvfb.returncode is None:
                    xvfb.terminate()
                    await xvfb.wait()
                raise

            session = BrowserSession(
                id=secrets.token_urlsafe(24),
                module_id=module_id,
                policy=policy,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                xvfb=xvfb,
                display=display,
                mode=mode,
                last_activity=time.monotonic(),
            )
            self._sessions[session.id] = session
            session.watchdog = asyncio.create_task(self._watchdog(session.id))
            return await self.status(session.id)

    async def _watchdog(self, session_id: str) -> None:
        while True:
            await asyncio.sleep(30)
            async with self._lock:
                session = self._sessions.get(session_id)
                if not session:
                    return
                if time.monotonic() - session.last_activity >= session.policy.idle_timeout_seconds:
                    await self._close_locked(session_id)
                    return

    def _require(self, session_id: str, *, touch: bool = True) -> BrowserSession:
        session = self._sessions.get(session_id)
        if not session:
            raise LookupError("Browser session is not active")
        if touch:
            session.last_activity = time.monotonic()
        return session

    async def status(self, session_id: str) -> dict:
        session = self._require(session_id, touch=False)
        async with session.operation_lock:
            try:
                cookies = await session.context.cookies()
            except Exception:
                cookies = []

            present_cookie_names = {cookie.get("name") for cookie in cookies}
            can_snapshot = not session.policy.required_cookie_names or bool(
                present_cookie_names & set(session.policy.required_cookie_names)
            )

            # Безопасное получение title во время смены страницы:
            try:
                title = await session.page.title()
            except Exception:
                title = "Loading..."

            try:
                url = session.page.url
            except Exception:
                url = ""

            return {
                "session_id": session.id,
                "module_id": session.module_id,
                "policy_id": session.policy.id,
                "policy_fingerprint": browser_policy_fingerprint(session.policy),
                "active": True,
                "title": title,
                "url": url,
                "viewport": VIEWPORT,
                "can_snapshot": can_snapshot,
                "mode": session.mode,
            }

    async def screenshot(self, session_id: str) -> bytes:
        session = self._require(session_id, touch=False)
        async with session.operation_lock:
            try:
                return await session.page.screenshot(type="jpeg", quality=72)
            except Exception:
                await asyncio.sleep(0.2)
                return await session.page.screenshot(type="jpeg", quality=72)

    async def click(self, session_id: str, x: float, y: float) -> None:
        session = self._require(session_id)
        async with session.operation_lock:
            await session.page.mouse.click(
                max(0, min(float(x), VIEWPORT["width"])),
                max(0, min(float(y), VIEWPORT["height"])),
            )

    async def type_text(self, session_id: str, text: str) -> None:
        session = self._require(session_id)
        async with session.operation_lock:
            await session.page.keyboard.insert_text(text)

    async def press(self, session_id: str, key: str) -> None:
        session = self._require(session_id)
        if key not in {
            "Tab",
            "Enter",
            "Backspace",
            "Escape",
            "ArrowUp",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
        }:
            raise ValueError("Unsupported browser key")
        async with session.operation_lock:
            await session.page.keyboard.press(key)

    async def scroll(self, session_id: str, delta_y: float) -> None:
        session = self._require(session_id)
        async with session.operation_lock:
            await session.page.mouse.wheel(0, max(-2000, min(float(delta_y), 2000)))

    async def navigate(self, session_id: str, url: str) -> dict:
        session = self._require(session_id)
        if not self._allowed_url(session.policy, url):
            raise ValueError("Navigation URL is outside the browser policy")
        async with session.operation_lock:
            await session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return await self.status(session_id)

    async def query(self, session_id: str, selector: str, *, limit: int = 20) -> list[dict]:
        session = self._require(session_id)
        async with session.operation_lock:
            async with asyncio.timeout(15):
                locator = session.page.locator(selector)
                count = min(await locator.count(), limit)
                result = []
                for index in range(count):
                    item = locator.nth(index)
                    text = await item.text_content()
                    html = await item.inner_html()
                    result.append(
                        {
                            "text": text[:50000] if text else text,
                            "html": html[:100000],
                        }
                    )
                return result

    async def export_storage_state(self, session_id: str) -> dict:
        session = self._require(session_id)
        if not session.policy.persist_snapshot:
            raise ValueError("Browser policy does not allow persistent snapshots")
        async with session.operation_lock:
            storage_state = await session.context.storage_state()
        present_cookie_names = {cookie.get("name") for cookie in storage_state.get("cookies", [])}
        if session.policy.required_cookie_names and not (
            present_cookie_names & set(session.policy.required_cookie_names)
        ):
            raise ValueError("Required account cookies are not present")
        return storage_state

    async def close(self, session_id: str | None = None) -> None:
        async with self._lock:
            if session_id is None:
                for active_id in list(self._sessions):
                    await self._close_locked(active_id)
                return
            await self._close_locked(session_id)

    async def close_policy(self, policy_id: str) -> None:
        async with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.policy.id == policy_id:
                    await self._close_locked(session_id)

    async def _close_locked(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if not session:
            raise LookupError("Browser session is not active")
        if session.watchdog and session.watchdog is not asyncio.current_task():
            session.watchdog.cancel()
        try:
            await session.browser.close()
        finally:
            await session.playwright.stop()
            if session.xvfb and session.xvfb.returncode is None:
                session.xvfb.terminate()
                await session.xvfb.wait()


browser_runtime = BrowserRuntime()


async def shutdown_browser_runtime() -> None:
    await browser_runtime.close()
