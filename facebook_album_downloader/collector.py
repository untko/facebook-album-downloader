from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

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
    s = (url or "").lower()
    return "facebook.com" in s and any(x in s for x in PHOTO_HINTS)


def direct_media_id(url: str) -> str:
    if not url:
        return ""
    base = url.split("?", 1)[0]
    return "cdn_" + hashlib.sha1(base.encode(), usedforsecurity=False).hexdigest()[:16]


def _is_media(url: str) -> bool:
    s = (url or "").lower()
    return any(x in s for x in CDN_HINTS) and not any(x in s for x in AVATAR_HINTS)


def extract_graphql_photo_records(payload) -> list[Photo]:
    found: dict[str, Photo] = {}

    def photo_from(node: dict) -> Photo | None:
        if str(node.get("__typename") or "").lower() != "photo":
            return None
        fb_url = ""
        images: list[tuple[int, str]] = []

        def walk(v):
            nonlocal fb_url
            if isinstance(v, dict):
                w = v.get("width") if isinstance(v.get("width"), int) else 0
                h = v.get("height") if isinstance(v.get("height"), int) else 0
                for k, x in v.items():
                    if isinstance(x, str):
                        if not fb_url and is_photo_url(x):
                            fb_url = x.replace("\\/", "/")
                        if k in {"uri", "url", "src"} and _is_media(x):
                            images.append((w * h, x.replace("\\/", "/")))
                    else:
                        walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(node)
        if not fb_url:
            return None
        direct = max(images, key=lambda x: x[0])[1] if images else ""
        media_id = normalize_photo_id(fb_url)
        raw_id = str(node.get("id") or node.get("legacy_fbid") or node.get("fbid") or "")
        if not media_id and raw_id.isdigit():
            media_id = f"fbid_{raw_id}"
        return Photo(0, fb_url, direct, media_id=media_id,
                     extracted_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def walk(v):
        if isinstance(v, dict):
            p = photo_from(v)
            if p:
                key = p.media_id or normalize_photo_id(p.facebook_url)
                if key and (key not in found or (not found[key].direct_url and p.direct_url)):
                    found[key] = p
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(payload)
    return list(found.values())


class MediaCollector:
    def __init__(self, page: Page, *, timeout_seconds: float = 30,
                 max_scrolls: int = 60, scroll_delay: float = 1.0,
                 max_photos: int = 5000) -> None:
        self.page = page
        self.timeout_ms = int(timeout_seconds * 1000)
        self.max_scrolls = max_scrolls
        self.scroll_delay_ms = int(scroll_delay * 1000)
        self.max_photos = max_photos
        self.source_url = ""
        self.title = "Facebook_Album"
        self._network_images: list[str] = []
        self._graphql: dict[str, Photo] = {}
        self._tasks: set[asyncio.Task] = set()
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            if response.request.resource_type == "image" and _is_media(response.url):
                self._network_images.append(response.url)
                self._network_images[:] = self._network_images[-150:]
            if "graphql" in response.url.lower():
                task = asyncio.create_task(self._read_graphql(response))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception:
            pass

    async def _read_graphql(self, response) -> None:
        try:
            text = await response.text()
        except Exception:
            return
        if '"Photo"' not in text:
            return
        payloads = []
        try:
            payloads = [json.loads(text)]
        except json.JSONDecodeError:
            for line in text.splitlines():
                try:
                    payloads.append(json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    pass
        for payload in payloads:
            for p in extract_graphql_photo_records(payload):
                key = p.media_id or normalize_photo_id(p.facebook_url)
                if key and (key not in self._graphql or (not self._graphql[key].direct_url and p.direct_url)):
                    self._graphql[key] = p

    async def _drain(self, timeout: float = 1.5) -> None:
        if self._tasks:
            try:
                await asyncio.wait(list(self._tasks), timeout=timeout)
            except Exception:
                pass

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
        if "login" in self.page.url.lower() or "checkpoint" in self.page.url.lower():
            raise RuntimeError("Facebook requires authentication for this content. Run with --login first.")
        self.title = await self._extract_title()
        return self.title

    async def _extract_title(self) -> str:
        try:
            og = self.page.locator('meta[property="og:title"]')
            if await og.count():
                value = await og.first.get_attribute("content")
                if value:
                    return self._clean_title(value)
        except Exception:
            pass
        try:
            return self._clean_title(await self.page.title())
        except Exception:
            sid = extract_set_id(self.source_url)
            return f"Facebook_{sid}" if sid else "Facebook_Album"

    @staticmethod
    def _clean_title(title: str) -> str:
        value = re.sub(r"^\(\d+\)\s*", "", title or "")
        value = re.sub(r"\s*[|\-–—]\s*Facebook.*$", "", value, flags=re.I).strip()
        return value or "Facebook_Album"

    async def collect(self, progress_hook: ProgressHook | None = None) -> list[Photo]:
        initial = await self._photo_links()
        overflow = await self._overflow_count()
        links = initial if overflow else await self._collect_scrolled_links()
        print(f"Detected {len(links)} photo link(s) in the source DOM.")

        expected = len(links) + overflow if overflow else None
        discovered: list[Photo] = []
        if overflow:
            print(f"Detected +{overflow} hidden-media overflow; expecting about {expected} media item(s).")
            if await self._open_overflow():
                await self.page.wait_for_timeout(900)
                await self._drain()
                print("Opened hidden-media viewer from the +N tile.")
                discovered = await self._traverse_open(progress_hook, expected)

        if not discovered and links:
            await self._goto(links[0])
            discovered = await self._traverse_open(progress_hook, expected)

        await self._drain()
        graph = self._graph_records_for(discovered, links)
        if graph:
            print(f"GraphQL media discovery: {len(graph)} photo record(s).")
            discovered = self._merge(discovered, graph)
            if progress_hook:
                await progress_hook(discovered)

        known = {p.media_id or normalize_photo_id(p.facebook_url) for p in discovered}
        seeds = list(dict.fromkeys(links + [p.facebook_url for p in graph if p.facebook_url]))
        for url in seeds:
            key = normalize_photo_id(url)
            if key and key in known:
                continue
            p = await self.extract_single(url)
            if not p:
                continue
            key = p.media_id or normalize_photo_id(p.facebook_url)
            if key and key in known:
                continue
            p.index = len(discovered) + 1
            discovered.append(p)
            if key:
                known.add(key)
            if progress_hook:
                await progress_hook(discovered)

        for i, p in enumerate(discovered, 1):
            p.index = i
            p.filename = p.filename or f"{i}.jpg"
        return discovered

    async def _overflow_count(self) -> int:
        try:
            return int(await self.page.evaluate(r"""
                () => {
                  let best = 0;
                  for (const el of document.querySelectorAll('body *')) {
                    const text = (el.textContent || '').trim();
                    const m = text.match(/^\+(\d{1,5})$/);
                    if (!m || el.children.length > 4) continue;
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    if (r.width < 16 || r.height < 16 || r.bottom <= 0 || r.right <= 0 ||
                        r.top >= innerHeight || r.left >= innerWidth || s.display === 'none' || s.visibility === 'hidden') continue;
                    best = Math.max(best, Number(m[1]));
                  }
                  return best;
                }
            """))
        except Exception:
            return 0

    async def _open_overflow(self) -> bool:
        try:
            return bool(await self.page.evaluate(r"""
                () => {
                  const hits = [];
                  for (const el of document.querySelectorAll('body *')) {
                    const text = (el.textContent || '').trim();
                    const m = text.match(/^\+(\d{1,5})$/);
                    if (!m || el.children.length > 4) continue;
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    if (r.width < 16 || r.height < 16 || r.bottom <= 0 || r.right <= 0 ||
                        r.top >= innerHeight || r.left >= innerWidth || s.display === 'none' || s.visibility === 'hidden') continue;
                    hits.push({el, n:Number(m[1]), area:r.width*r.height});
                  }
                  hits.sort((a,b) => b.n-a.n || b.area-a.area);
                  if (!hits.length) return false;
                  const el = hits[0].el;
                  (el.closest('a[href],[role="button"],button') || el.parentElement || el).click();
                  return true;
                }
            """))
        except Exception:
            return False

    async def _photo_links(self) -> list[str]:
        try:
            hrefs = await self.page.locator('a[href*="fbid="],a[href*="/photo/"],a[href*="/photos/"]').evaluate_all(
                "els => els.map(a => a.href)"
            )
        except Exception:
            return []
        out, seen = [], set()
        for href in hrefs:
            href = urljoin("https://www.facebook.com", href)
            key = normalize_photo_id(href)
            if is_photo_url(href) and key and key not in seen:
                seen.add(key)
                out.append(href)
        return out

    async def _collect_scrolled_links(self) -> list[str]:
        out = await self._photo_links()
        seen = {normalize_photo_id(x) for x in out}
        stagnant = 0
        for _ in range(self.max_scrolls):
            before = len(out)
            await self.page.evaluate("""
                () => {
                  for (const el of document.querySelectorAll('*')) {
                    const s=getComputedStyle(el);
                    if ((s.overflowY==='auto'||s.overflowY==='scroll') && el.scrollHeight>el.clientHeight+100) el.scrollTop=el.scrollHeight;
                  }
                  scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
                }
            """)
            await self.page.wait_for_timeout(self.scroll_delay_ms)
            for href in await self._photo_links():
                key = normalize_photo_id(href)
                if key and key not in seen:
                    seen.add(key); out.append(href)
            stagnant = stagnant + 1 if len(out) == before else 0
            if stagnant >= 4:
                break
        return out

    async def _traverse_open(self, hook: ProgressHook | None, expected: int | None) -> list[Photo]:
        records: list[Photo] = []
        seen: set[str] = set()
        start_set = extract_set_id(self.page.url)
        failures = 0

        for _ in range(self.max_photos):
            await self._drain(0.2)
            p = await self._current_record()
            if not p:
                failures += 1
                if failures >= 2:
                    break
                if not await self._advance(""):
                    break
                continue

            cur_set = extract_set_id(p.facebook_url)
            if not start_set and cur_set:
                start_set = cur_set
            if start_set and cur_set and cur_set != start_set:
                break

            base = p.media_id or normalize_photo_id(p.facebook_url)
            cdn = direct_media_id(p.direct_url)
            key = f"{base}|{cdn}" if base and cdn else (base or cdn)
            if key and key not in seen:
                seen.add(key); failures = 0
                p.index = len(records) + 1; p.filename = f"{p.index}.jpg"
                records.append(p)
                suffix = f"/{expected}" if expected else ""
                print(f"Viewer traversal: {len(records)}{suffix} media item(s)", end="\r", flush=True)
                if hook:
                    await hook(records)

            if expected and len(records) >= expected:
                break
            before = await self._fingerprint()
            if not await self._advance(before):
                failures += 1
                if failures >= 2:
                    break

        if records:
            suffix = f"/{expected}" if expected else ""
            print(f"Viewer traversal complete: {len(records)}{suffix} media item(s).")
        return records

    async def _fingerprint(self) -> str:
        p = await self._current_record()
        return "|".join((p.media_id, p.facebook_url, p.direct_url)) if p else self.page.url

    async def _advance(self, before: str) -> bool:
        for selector in (
            '[aria-label="Next photo"]', '[aria-label="Next"]',
            'button[aria-label*="next" i]', '[role="button"][aria-label*="next" i]',
            'a[aria-label*="next" i]',
        ):
            try:
                loc = self.page.locator(selector)
                for i in range(min(await loc.count(), 8)):
                    x = loc.nth(i)
                    if not await x.is_visible():
                        continue
                    box = await x.bounding_box()
                    width = await self.page.evaluate("innerWidth")
                    if box and box["x"] < width * 0.45:
                        continue
                    await x.click(timeout=1200)
                    if not before or await self._wait_changed(before, 3500):
                        return True
            except Exception:
                pass
        try:
            await self.page.keyboard.press("ArrowRight")
            if not before or await self._wait_changed(before, 3000):
                return True
        except Exception:
            pass
        return False

    async def _wait_changed(self, before: str, timeout_ms: int) -> bool:
        end = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < end:
            await self.page.wait_for_timeout(180)
            if (await self._fingerprint()) != before:
                return True
        return False

    async def _current_record(self) -> Photo | None:
        try:
            c = await self.page.evaluate("""
                () => {
                  const groups = [
                    [...document.querySelectorAll('img[data-visualcompletion="media-vc-image"]')],
                    [...document.querySelectorAll('[data-pagelet*="MediaViewer"] img')],
                    [...document.querySelectorAll('[role="dialog"] img')]
                  ];
                  for (const group of groups) {
                    const xs = group.map(img => {
                      const src=img.currentSrc||img.src||'', w=img.naturalWidth||img.width||0, h=img.naturalHeight||img.height||0;
                      const r=img.getBoundingClientRect(), a=img.closest('a[href]');
                      return {src,w,h,area:w*h,href:a?a.href:'',visible:r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth};
                    }).filter(x => x.visible && x.w>=250 && x.h>=250 && (x.src.includes('scontent')||x.src.includes('fbcdn.net')))
                      .sort((a,b)=>b.area-a.area);
                    if (xs.length) return xs[0];
                  }
                  return null;
                }
            """)
        except Exception:
            c = None
        direct = c.get("src", "") if c else ""
        anchor = c.get("href", "") if c else ""
        if not direct and self._network_images:
            direct = self._network_images[-1]
        if direct and not _is_media(direct):
            direct = ""
        fb_url = anchor if is_photo_url(anchor) else self.page.url
        if not is_photo_url(fb_url):
            links = await self._photo_links()
            fb_url = links[0] if links else fb_url
        media_id = normalize_photo_id(fb_url) if is_photo_url(fb_url) else direct_media_id(direct)
        if not direct and not media_id:
            return None
        return Photo(0, fb_url, direct, media_id=media_id,
                     extracted_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def _graph_records_for(self, discovered: list[Photo], links: list[str]) -> list[Photo]:
        records = list(self._graphql.values())
        sets = {extract_set_id(p.facebook_url) for p in discovered if extract_set_id(p.facebook_url)}
        sets.update(extract_set_id(x) for x in links if extract_set_id(x))
        if sets:
            filtered = [p for p in records if extract_set_id(p.facebook_url) in sets]
            if filtered:
                records = filtered
        return records

    @staticmethod
    def _merge(base: list[Photo], extras: list[Photo]) -> list[Photo]:
        out = list(base)
        by_id = {p.media_id or normalize_photo_id(p.facebook_url): p for p in out}
        for p in extras:
            key = p.media_id or normalize_photo_id(p.facebook_url)
            if not key:
                continue
            if key in by_id:
                if not by_id[key].direct_url and p.direct_url:
                    by_id[key].direct_url = p.direct_url
            else:
                p.index = len(out) + 1
                out.append(p); by_id[key] = p
        return out

    async def extract_single(self, facebook_url: str) -> Photo | None:
        self._network_images.clear()
        await self._goto(facebook_url)
        for _ in range(8):
            p = await self._current_record()
            if p and p.direct_url:
                p.facebook_url = facebook_url
                p.media_id = normalize_photo_id(facebook_url) or p.media_id
                return p
            await self.page.wait_for_timeout(300)
        return None
