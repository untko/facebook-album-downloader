from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .manifest import extract_set_id, normalize_photo_id
from .models import Photo

ProgressHook = Callable[[list[Photo]], Awaitable[None]]

PHOTO_HINTS = ("/photo/", "/photos/", "photo.php", "fbid=")
CDN_HINTS = ("scontent", "fbcdn.net")
AVATAR_HINTS = (
    "/t39.30808-1/", "/t1.6435-1/", "/t1.15752-1/",
    "s100x100", "p100x100", "s50x50", "p50x50", "s60x60", "p60x60",
    "s160x160", "p160x160", "dst-jpg_s", "dst-jpg_p",
)


def is_photo_url(url: str) -> bool:
    lower = (url or "").lower()
    return "facebook.com" in lower and any(hint in lower for hint in PHOTO_HINTS)


def direct_media_id(url: str) -> str:
    if not url:
        return ""
    digest = hashlib.sha1(url.split("?", 1)[0].encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"cdn_{digest}"


def _is_likely_media(url: str) -> bool:
    lower = (url or "").lower()
    return any(h in lower for h in CDN_HINTS) and not any(h in lower for h in AVATAR_HINTS)


class MediaCollector:
    def __init__(
        self,
        page: Page,
        *,
        timeout_seconds: float = 30,
        max_scrolls: int = 60,
        scroll_delay: float = 1.0,
        max_photos: int = 5000,
    ) -> None:
        self.page = page
        self.timeout_ms = int(timeout_seconds * 1000)
        self.max_scrolls = max_scrolls
        self.scroll_delay_ms = int(scroll_delay * 1000)
        self.max_photos = max_photos
        self.source_url = ""
        self.title = "Facebook_Album"
        self._recent_network_images: list[str] = []
        self.page.on("response", self._capture_image_response)

    def _capture_image_response(self, response) -> None:
        try:
            if response.request.resource_type == "image" and _is_likely_media(response.url):
                self._recent_network_images.append(response.url)
                if len(self._recent_network_images) > 100:
                    del self._recent_network_images[:-100]
        except Exception:
            return

    async def _goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            try:
                await self.page.evaluate("window.stop()")
            except Exception:
                pass
        await self.page.wait_for_timeout(600)

    async def open(self, source_url: str) -> str:
        self.source_url = source_url
        await self._goto(source_url)
        lower_url = self.page.url.lower()
        if "login" in lower_url or "checkpoint" in lower_url:
            raise RuntimeError("Facebook requires authentication for this content. Run with --login first.")
        self.title = await self._extract_title()
        return self.title

    async def _extract_title(self) -> str:
        try:
            og = self.page.locator('meta[property="og:title"]')
            if await og.count():
                content = await og.first.get_attribute("content")
                if content and 2 < len(content.strip()) <= 160:
                    return self._clean_title(content)
        except Exception:
            pass
        try:
            title = await self.page.title()
            if title:
                return self._clean_title(title)
        except Exception:
            pass
        set_id = extract_set_id(self.source_url)
        return f"Facebook_{set_id}" if set_id else "Facebook_Album"

    @staticmethod
    def _clean_title(title: str) -> str:
        value = re.sub(r"^\(\d+\)\s*", "", title or "")
        value = re.sub(r"\s*[|\-–—]\s*Facebook.*$", "", value, flags=re.I).strip()
        return value or "Facebook_Album"

    async def collect(self, progress_hook: ProgressHook | None = None) -> list[Photo]:
        links = await self._collect_visible_and_scrolled_links()
        print(f"Detected {len(links)} photo link(s) in the source DOM.")

        discovered: list[Photo] = []
        if links:
            viewer_records = await self._traverse_viewer(links[0], progress_hook=progress_hook)
            discovered.extend(viewer_records)

        known_ids = {p.media_id or normalize_photo_id(p.facebook_url) for p in discovered}
        unresolved_links = [u for u in links if normalize_photo_id(u) not in known_ids]

        for link in unresolved_links:
            record = await self.extract_single(link)
            if not record:
                continue
            key = record.media_id or normalize_photo_id(record.facebook_url)
            if key and key in known_ids:
                continue
            record.index = len(discovered) + 1
            discovered.append(record)
            if key:
                known_ids.add(key)
            if progress_hook:
                await progress_hook(discovered)

        for i, photo in enumerate(discovered, start=1):
            photo.index = i
            if not photo.filename:
                photo.filename = f"{i}.jpg"
        return discovered

    async def _collect_visible_and_scrolled_links(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        stagnant = 0

        async def collect_dom() -> int:
            hrefs = await self.page.locator("a[href]").evaluate_all("els => els.map(a => a.href)")
            added = 0
            for href in hrefs:
                absolute = urljoin("https://www.facebook.com", href)
                if is_photo_url(absolute):
                    key = normalize_photo_id(absolute)
                    if key and key not in seen:
                        seen.add(key)
                        ordered.append(absolute)
                        added += 1
            return added

        await collect_dom()
        for _ in range(self.max_scrolls):
            before = len(ordered)
            await self.page.evaluate(
                """
                () => {
                  for (const el of document.querySelectorAll('*')) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 100) {
                      el.scrollTop = el.scrollHeight;
                    }
                  }
                  window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
                }
                """
            )
            try:
                await self.page.keyboard.press("End")
            except Exception:
                pass
            await self.page.wait_for_timeout(self.scroll_delay_ms)
            await collect_dom()
            if len(ordered) == before:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 4:
                break
        return ordered

    async def _traverse_viewer(self, first_url: str, progress_hook: ProgressHook | None = None) -> list[Photo]:
        await self._goto(first_url)
        start_set = extract_set_id(self.page.url) or extract_set_id(first_url)
        records: list[Photo] = []
        seen: set[str] = set()

        for _ in range(self.max_photos):
            record = await self._extract_current_record()
            if not record:
                break

            current_set = extract_set_id(record.facebook_url)
            if start_set and current_set and current_set != start_set:
                break

            base_id = record.media_id or normalize_photo_id(record.facebook_url)
            cdn_id = direct_media_id(record.direct_url) if record.direct_url else ""
            key = f"{base_id}|{cdn_id}" if base_id and cdn_id else (base_id or cdn_id)
            if not key or key in seen:
                break

            seen.add(key)
            record.index = len(records) + 1
            record.filename = f"{record.index}.jpg"
            records.append(record)
            print(f"Viewer traversal: {len(records)} media item(s)", end="\r", flush=True)
            if progress_hook:
                await progress_hook(records)

            before = await self._viewer_fingerprint()
            if not await self._advance_viewer(before):
                break

        if records:
            print(f"Viewer traversal complete: {len(records)} media item(s).")
        return records

    async def _viewer_fingerprint(self) -> str:
        record = await self._extract_current_record()
        if not record:
            return self.page.url
        return "|".join((record.media_id, record.facebook_url, record.direct_url))

    async def _advance_viewer(self, before: str) -> bool:
        try:
            await self.page.keyboard.press("ArrowRight")
        except Exception:
            pass
        if await self._wait_for_fingerprint_change(before, 3500):
            return True

        clicked = await self.page.evaluate(
            """
            () => {
              const w = innerWidth, h = innerHeight;
              const els = [...document.querySelectorAll('[role="button"], button, a')];
              const candidates = els.map(el => {
                const r = el.getBoundingClientRect();
                const visible = r.width >= 24 && r.height >= 24 && r.bottom > 0 && r.right > 0 && r.top < h && r.left < w;
                return {el, r, visible};
              }).filter(x => x.visible && x.r.left > w * 0.68 && x.r.top < h * 0.78 && x.r.bottom > h * 0.22)
                .sort((a,b) => Math.abs((a.r.top+a.r.bottom)/2-h/2)-Math.abs((b.r.top+b.r.bottom)/2-h/2));
              if (!candidates.length) return false;
              candidates[0].el.click();
              return true;
            }
            """
        )
        if not clicked:
            return False
        return await self._wait_for_fingerprint_change(before, 3500)

    async def _wait_for_fingerprint_change(self, before: str, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            await self.page.wait_for_timeout(200)
            after = await self._viewer_fingerprint()
            if after and after != before:
                return True
        return False

    async def _extract_current_record(self) -> Photo | None:
        candidate = await self.page.evaluate(
            """
            () => {
              const imgs = [...document.images].map(img => {
                const src = img.currentSrc || img.src || '';
                const w = img.naturalWidth || img.width || 0;
                const h = img.naturalHeight || img.height || 0;
                const r = img.getBoundingClientRect();
                const visible = r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
                return {src, w, h, area: w*h, visible};
              }).filter(x => x.visible && x.w >= 300 && x.h >= 300 && (x.src.includes('scontent') || x.src.includes('fbcdn.net')))
                .sort((a,b) => b.area-a.area);
              return imgs.length ? imgs[0] : null;
            }
            """
        )

        direct_url = candidate.get("src", "") if candidate else ""
        if not direct_url and self._recent_network_images:
            direct_url = self._recent_network_images[-1]
        if direct_url and not _is_likely_media(direct_url):
            direct_url = ""

        fb_url = self.page.url
        if not is_photo_url(fb_url):
            fb_url = await self._best_photo_anchor() or fb_url
        media_id = normalize_photo_id(fb_url) if is_photo_url(fb_url) else direct_media_id(direct_url)

        if not direct_url and not media_id:
            return None
        return Photo(
            index=0,
            facebook_url=fb_url,
            direct_url=direct_url,
            media_id=media_id,
            extracted_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    async def _best_photo_anchor(self) -> str:
        try:
            hrefs = await self.page.locator('a[href*="fbid="], a[href*="/photo/"], a[href*="/photos/"]').evaluate_all(
                "els => els.map(a => a.href)"
            )
        except Exception:
            return ""
        current_set = extract_set_id(self.page.url)
        for href in hrefs:
            if current_set and extract_set_id(href) == current_set:
                return href
        return hrefs[0] if hrefs else ""

    async def extract_single(self, facebook_url: str) -> Photo | None:
        self._recent_network_images.clear()
        await self._goto(facebook_url)
        for _ in range(8):
            record = await self._extract_current_record()
            if record and record.direct_url:
                record.facebook_url = facebook_url
                record.media_id = normalize_photo_id(facebook_url) or record.media_id
                return record
            await self.page.wait_for_timeout(300)
        return None
