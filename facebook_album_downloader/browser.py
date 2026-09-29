from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


class BrowserSession:
    def __init__(
        self,
        *,
        browser_name: str = "chromium",
        headless: bool = False,
        state_path: str = "facebook_cookies.json",
        use_state: bool = True,
        timeout_seconds: float = 30,
    ) -> None:
        self.browser_name = browser_name
        self.headless = headless
        self.state_path = state_path
        self.use_state = use_state
        self.timeout_ms = int(timeout_seconds * 1000)
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._legacy_cookies: list[dict] = []

    async def start(self) -> "BrowserSession":
        self.playwright = await async_playwright().start()
        launcher = getattr(self.playwright, self.browser_name)
        self.browser = await launcher.launch(headless=self.headless)

        state = None
        if self.use_state:
            state = self._read_state()

        if isinstance(state, dict) and isinstance(state.get("cookies"), list):
            self.context = await self.browser.new_context(storage_state=state)
        else:
            self.context = await self.browser.new_context()
            if isinstance(state, list):
                self._legacy_cookies = self._convert_legacy_cookies(state)
                if self._legacy_cookies:
                    await self.context.add_cookies(self._legacy_cookies)

        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        self.page = await self.context.new_page()
        return self

    def _read_state(self) -> dict | list | None:
        path = Path(self.state_path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _convert_legacy_cookies(cookies: list[dict]) -> list[dict]:
        converted: list[dict] = []
        for cookie in cookies:
            if not isinstance(cookie, dict) or not cookie.get("name"):
                continue
            item = {
                "name": str(cookie["name"]),
                "value": str(cookie.get("value", "")),
                "domain": str(cookie.get("domain") or ".facebook.com"),
                "path": str(cookie.get("path") or "/"),
                "secure": bool(cookie.get("secure", False)),
                "httpOnly": bool(cookie.get("httpOnly", False)),
            }
            expiry = cookie.get("expires", cookie.get("expiry"))
            if expiry not in (None, ""):
                try:
                    item["expires"] = float(expiry)
                except (TypeError, ValueError):
                    pass
            same_site = cookie.get("sameSite")
            if same_site in {"Strict", "Lax", "None"}:
                item["sameSite"] = same_site
            converted.append(item)
        return converted

    async def save_state(self) -> None:
        if self.use_state and self.context:
            Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=self.state_path)

    async def is_authenticated(self) -> bool:
        if not self.context:
            return False
        cookies = await self.context.cookies("https://www.facebook.com")
        return any(c.get("name") == "c_user" and c.get("value") for c in cookies)

    async def has_login_wall(self) -> bool:
        """Detect Facebook's in-page authentication gate, not just URL redirects.

        Public posts can render a few attachments while an unauthenticated modal such
        as "See more on Facebook" blocks the media viewer. In that case the page URL
        remains a post/photo URL, so URL-only login detection is insufficient.
        """
        if not self.page:
            return False
        try:
            return bool(await self.page.evaluate(
                """
                () => {
                  const body = (document.body?.innerText || '').toLowerCase();
                  const textGate = body.includes('see more on facebook') ||
                                   body.includes('see more from') ||
                                   body.includes('log in to facebook');
                  const visiblePassword = [...document.querySelectorAll('input[type="password"]')]
                    .some(el => {
                      const r = el.getBoundingClientRect();
                      const s = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
                             r.top < innerHeight && r.left < innerWidth &&
                             s.display !== 'none' && s.visibility !== 'hidden';
                    });
                  const visibleLoginDialog = [...document.querySelectorAll('[role="dialog"]')]
                    .some(el => {
                      const t = (el.innerText || '').toLowerCase();
                      return t.includes('log in') && (t.includes('password') || t.includes('email'));
                    });
                  return textGate && (visiblePassword || visibleLoginDialog);
                }
                """
            ))
        except Exception:
            return False

    async def login(self, timeout_seconds: int = 300) -> bool:
        if not self.page:
            raise RuntimeError("BrowserSession has not been started")
        await self.page.goto("https://www.facebook.com/login.php", wait_until="domcontentloaded")
        print("Log in to Facebook in the browser window. Waiting for the authenticated session...")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await self.is_authenticated():
                lower = self.page.url.lower()
                blocked = ("login", "checkpoint", "two_step", "recover", "confirm")
                if not any(token in lower for token in blocked):
                    await self.save_state()
                    print(f"Saved Playwright session state to {self.state_path}")
                    return True
            await self.page.wait_for_timeout(1000)
        return False

    async def close(self) -> None:
        if self.context:
            if self.use_state:
                try:
                    await self.save_state()
                except Exception:
                    pass
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
