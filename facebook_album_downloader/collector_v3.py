from __future__ import annotations

import time

from .collector import MediaCollector as BaseMediaCollector, direct_media_id, is_photo_url
from .manifest import extract_set_id, normalize_photo_id
from .models import Photo


class MediaCollector(BaseMediaCollector):
    """Post-aware collector with stable, image-driven viewer traversal."""

    async def collect(self, progress_hook=None) -> list[Photo]:
        all_links = await self._photo_links()
        overflow = await self._overflow_count()
        links = self._scope_links_to_post_set(all_links) if overflow else all_links
        if not overflow:
            links = await self._collect_scrolled_links()

        if len(links) != len(all_links) and overflow:
            print(
                f"Detected {len(all_links)} photo link(s) in the source DOM; "
                f"{len(links)} belong to the post media set."
            )
        else:
            print(f"Detected {len(links)} photo link(s) in the source DOM.")

        expected = len(links) + overflow if overflow and links else None
        discovered: list[Photo] = []

        if overflow:
            suffix = f"; expecting {expected} media item(s)" if expected else ""
            print(f"Detected +{overflow} hidden-media overflow{suffix}.")
            opened = await self._open_visible_photo_in_place(links)
            if not opened:
                print("Visible photo click did not enter a media viewer; trying the +N tile.")
                opened = await self._open_overflow_verified(overflow)
            if opened:
                await self.page.wait_for_timeout(500)
                discovered = await self._traverse_stable(progress_hook, expected)

            if expected and len(discovered) < expected:
                await self._goto(self.source_url)
                await self.page.wait_for_timeout(400)
                opened = await self._open_overflow_verified(overflow)
                if not opened:
                    opened = await self._open_visible_photo_in_place(links)
                if opened:
                    retry = await self._traverse_stable(progress_hook, expected)
                    discovered = self._merge_unique_images(discovered, retry, expected)

        if not discovered and links:
            if await self._open_visible_photo_in_place(links):
                discovered = await self._traverse_stable(progress_hook, expected)
            if not discovered:
                await self._goto(links[0])
                discovered = await self._traverse_stable(progress_hook, expected)

        await self._drain()
        graph = self._graph_records_for(discovered, links)
        if graph:
            print(f"GraphQL media discovery: {len(graph)} photo record(s).")
        if not expected or len(discovered) < expected:
            discovered = self._merge_unique_images(discovered, graph, expected)

        if not expected or len(discovered) < expected:
            seen_images = {self._image_key(p) for p in discovered if self._image_key(p)}
            seen_ids = {p.media_id for p in discovered if p.media_id}
            seeds = list(dict.fromkeys(links + [p.facebook_url for p in graph if p.facebook_url]))
            for url in seeds:
                if expected and len(discovered) >= expected:
                    break
                key = normalize_photo_id(url)
                if key and key in seen_ids:
                    continue
                p = await self.extract_single(url)
                if not p:
                    continue
                image_key = self._image_key(p)
                if image_key and image_key in seen_images:
                    continue
                if image_key:
                    seen_images.add(image_key)
                if p.media_id:
                    seen_ids.add(p.media_id)
                discovered.append(p)
                if progress_hook:
                    await progress_hook(discovered)

        discovered = self._dedupe_by_image(discovered)
        if expected and len(discovered) > expected:
            discovered = discovered[:expected]
        if expected and len(discovered) < expected:
            await self._print_diagnostic(expected, len(discovered))

        for i, p in enumerate(discovered, 1):
            p.index = i
            p.filename = f"{i}.jpg"
        return discovered

    @staticmethod
    def _scope_links_to_post_set(links: list[str]) -> list[str]:
        groups: dict[str, list[str]] = {}
        for url in links:
            sid = extract_set_id(url)
            if sid and sid.startswith("pcb."):
                groups.setdefault(sid, []).append(url)
        if not groups:
            return links
        _, scoped = max(groups.items(), key=lambda item: len(item[1]))
        return list(dict.fromkeys(scoped)) if len(scoped) >= 2 else links

    @staticmethod
    def _image_key(photo: Photo) -> str:
        return direct_media_id(photo.direct_url) if photo.direct_url else ""

    @classmethod
    def _dedupe_by_image(cls, photos: list[Photo]) -> list[Photo]:
        out: list[Photo] = []
        seen_images: set[str] = set()
        fallback_ids: set[str] = set()
        for p in photos:
            image_key = cls._image_key(p)
            if image_key:
                if image_key in seen_images:
                    continue
                seen_images.add(image_key)
            else:
                media_id = p.media_id or normalize_photo_id(p.facebook_url)
                if media_id and media_id in fallback_ids:
                    continue
                if media_id:
                    fallback_ids.add(media_id)
            out.append(p)
        return out

    @classmethod
    def _merge_unique_images(cls, base: list[Photo], extras: list[Photo], limit: int | None = None) -> list[Photo]:
        out = cls._dedupe_by_image(base)
        seen_images = {cls._image_key(p) for p in out if cls._image_key(p)}
        seen_ids = {p.media_id for p in out if p.media_id}
        for p in extras:
            if limit and len(out) >= limit:
                break
            image_key = cls._image_key(p)
            if image_key and image_key in seen_images:
                continue
            if not image_key and p.media_id and p.media_id in seen_ids:
                continue
            if image_key:
                seen_images.add(image_key)
            if p.media_id:
                seen_ids.add(p.media_id)
            out.append(p)
        return out

    async def _displayed_media(self) -> dict | None:
        try:
            return await self.page.evaluate(r"""
                () => {
                  const visible = el => {
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    return r.width >= 180 && r.height >= 180 && r.bottom > 0 && r.right > 0 &&
                      r.top < innerHeight && r.left < innerWidth && s.display !== 'none' &&
                      s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
                  };
                  const bad = src => /(?:-1\/|s100x100|p100x100|s50x50|p50x50|s60x60|p60x60|s160x160|p160x160|dst-jpg_s|dst-jpg_p)/i.test(src);
                  const selectors = [
                    'img[data-visualcompletion="media-vc-image"]',
                    '[data-pagelet*="MediaViewer"] img',
                    '[role="dialog"] img'
                  ];
                  const seen = new Set(), items = [];
                  selectors.forEach((selector, rank) => {
                    for (const img of document.querySelectorAll(selector)) {
                      if (seen.has(img) || !visible(img)) continue;
                      seen.add(img);
                      const src = img.currentSrc || img.src || '';
                      if (!(src.includes('scontent') || src.includes('fbcdn.net')) || bad(src)) continue;
                      const r = img.getBoundingClientRect();
                      const natural = (img.naturalWidth || 0) * (img.naturalHeight || 0);
                      const rendered = r.width * r.height;
                      const a = img.closest('a[href]');
                      items.push({
                        src, href: a ? a.href : '', rendered, natural,
                        score: rendered * 1000000 + natural + (3-rank) * 1000
                      });
                    }
                  });
                  items.sort((a,b) => b.score-a.score);
                  return items[0] || null;
                }
            """)
        except Exception:
            return None

    async def _current_record(self) -> Photo | None:
        media = await self._displayed_media()
        if not media:
            return None
        direct = media.get("src", "")
        fb_url = self.page.url if is_photo_url(self.page.url) else media.get("href", "")
        if not is_photo_url(fb_url):
            links = self._scope_links_to_post_set(await self._photo_links())
            fb_url = links[0] if links else self.page.url
        media_id = normalize_photo_id(fb_url) if is_photo_url(fb_url) else ""
        return Photo(0, fb_url, direct, media_id=media_id,
                     extracted_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    async def _fingerprint(self) -> str:
        media = await self._displayed_media()
        return direct_media_id(media.get("src", "")) if media else ""

    async def _traverse_stable(self, hook, expected: int | None) -> list[Photo]:
        records: list[Photo] = []
        seen_images: set[str] = set()
        repeated = 0
        for _ in range(self.max_photos):
            p = await self._wait_current_record()
            if not p:
                break
            image_key = self._image_key(p)
            if not image_key:
                break
            if image_key in seen_images:
                repeated += 1
                if repeated >= 2:
                    break
            else:
                repeated = 0
                seen_images.add(image_key)
                p.index = len(records) + 1
                p.filename = f"{p.index}.jpg"
                records.append(p)
                suffix = f"/{expected}" if expected else ""
                print(f"Viewer traversal: {len(records)}{suffix} media item(s)", end="\r", flush=True)
                if hook:
                    await hook(records)
            if expected and len(records) >= expected:
                break
            if not await self._advance_image(image_key, p.media_id):
                break
        if records:
            suffix = f"/{expected}" if expected else ""
            print(f"Viewer traversal complete: {len(records)}{suffix} media item(s).")
        return records

    async def _wait_current_record(self, timeout_ms: int = 4000) -> Photo | None:
        end = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < end:
            p = await self._current_record()
            if p and p.direct_url:
                return p
            await self.page.wait_for_timeout(120)
        return None

    async def _advance_image(self, before_image: str, before_media_id: str) -> bool:
        for selector in (
            '[aria-label="Next photo"]', '[aria-label="Next"]',
            'button[aria-label*="next" i]', '[role="button"][aria-label*="next" i]',
            'a[aria-label*="next" i]', '[title*="next" i]',
        ):
            try:
                loc = self.page.locator(selector)
                for i in range(min(await loc.count(), 10)):
                    x = loc.nth(i)
                    if not await x.is_visible():
                        continue
                    box = await x.bounding_box()
                    width = await self.page.evaluate("innerWidth")
                    if box and box["x"] < width * 0.45:
                        continue
                    await x.click(timeout=1200)
                    if await self._wait_image_changed(before_image, before_media_id, 4500):
                        return True
            except Exception:
                pass
        try:
            await self.page.locator("body").focus()
            await self.page.keyboard.press("ArrowRight")
            if await self._wait_image_changed(before_image, before_media_id, 4000):
                return True
        except Exception:
            pass
        return False

    async def _wait_image_changed(self, before_image: str, before_media_id: str, timeout_ms: int) -> bool:
        end = time.monotonic() + timeout_ms / 1000
        changed = False
        while time.monotonic() < end:
            await self.page.wait_for_timeout(120)
            current = await self._fingerprint()
            if current and current != before_image:
                changed = True
                break
        if not changed:
            return False
        if before_media_id:
            settle_end = time.monotonic() + 1.5
            while time.monotonic() < settle_end:
                current_id = normalize_photo_id(self.page.url) if is_photo_url(self.page.url) else ""
                if current_id and current_id != before_media_id:
                    break
                await self.page.wait_for_timeout(100)
        await self.page.wait_for_timeout(120)
        return True

    async def _viewer_signature(self) -> dict:
        try:
            return await self.page.evaluate(r"""
                () => {
                  const visible = el => {
                    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                    return r.width>0 && r.height>0 && r.bottom>0 && r.right>0 &&
                      r.top<innerHeight && r.left<innerWidth && s.visibility!=='hidden' && s.display!=='none';
                  };
                  const mediaImgs=[...document.querySelectorAll(
                    'img[data-visualcompletion="media-vc-image"], [data-pagelet*="MediaViewer"] img'
                  )].filter(visible).length;
                  const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible).length;
                  const nextish=[...document.querySelectorAll('[aria-label],button,[role="button"],a')]
                    .filter(visible)
                    .map(el => (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim())
                    .filter(x => /next|right|forward/i.test(x)).slice(0,8);
                  return {url:location.href, mediaImgs, dialogs, nextish};
                }
            """)
        except Exception:
            return {"url": self.page.url, "mediaImgs": 0, "dialogs": 0, "nextish": []}

    async def _open_visible_photo_in_place(self, links: list[str]) -> bool:
        wanted = {normalize_photo_id(x) for x in links if normalize_photo_id(x)}
        try:
            target = await self.page.evaluate(r"""
                wanted => {
                  const visible = el => {
                    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                    return r.width>80 && r.height>80 && r.bottom>0 && r.right>0 &&
                      r.top<innerHeight && r.left<innerWidth && s.visibility!=='hidden' && s.display!=='none';
                  };
                  const anchors=[...document.querySelectorAll('a[href]')].filter(a => visible(a) &&
                    (a.href.includes('fbid=') || a.href.includes('/photo/') || a.href.includes('/photos/')));
                  for (const a of anchors) {
                    const m=a.href.match(/[?&]fbid=(\d+)/);
                    const id=m ? 'fbid_'+m[1] : '';
                    if (wanted.length && id && !wanted.includes(id)) continue;
                    const r=a.getBoundingClientRect();
                    return {x:r.left+r.width/2,y:r.top+r.height/2,href:a.href};
                  }
                  return null;
                }
            """, list(wanted))
        except Exception:
            target = None
        if not target:
            return False
        before = await self._viewer_signature()
        try:
            await self.page.mouse.click(float(target["x"]), float(target["y"]))
        except Exception:
            return False
        return await self._wait_for_viewer_transition(before, require_overflow_drop=False)

    async def _open_overflow_verified(self, overflow: int) -> bool:
        try:
            target = await self.page.evaluate(r"""
                () => {
                  const hits=[];
                  for (const el of document.querySelectorAll('body *')) {
                    const text=(el.textContent||'').trim(), m=text.match(/^\+(\d{1,5})$/);
                    if (!m || el.children.length>4) continue;
                    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                    if (r.width<16 || r.height<16 || r.bottom<=0 || r.right<=0 ||
                        r.top>=innerHeight || r.left>=innerWidth || s.display==='none' || s.visibility==='hidden') continue;
                    const t=el.closest('a[href],[role="button"],button') || el.parentElement || el;
                    const tr=t.getBoundingClientRect();
                    hits.push({n:Number(m[1]),area:tr.width*tr.height,x:tr.left+tr.width/2,y:tr.top+tr.height/2});
                  }
                  hits.sort((a,b)=>b.n-a.n || b.area-a.area);
                  return hits[0] || null;
                }
            """)
        except Exception:
            target = None
        if not target:
            return False
        before = await self._viewer_signature()
        try:
            await self.page.mouse.click(float(target["x"]), float(target["y"]))
        except Exception:
            return False
        opened = await self._wait_for_viewer_transition(before, require_overflow_drop=True, overflow=overflow)
        if opened:
            print("Opened hidden-media viewer from the +N tile.")
        return opened

    async def _wait_for_viewer_transition(self, before: dict, *, require_overflow_drop: bool,
                                          overflow: int = 0) -> bool:
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            await self.page.wait_for_timeout(180)
            after = await self._viewer_signature()
            dropped = (await self._overflow_count()) < overflow if require_overflow_drop else False
            if (after.get("url") != before.get("url") or
                    after.get("mediaImgs", 0) > before.get("mediaImgs", 0) or dropped):
                return True
        return False

    async def _print_diagnostic(self, expected: int, found: int) -> None:
        sig = await self._viewer_signature()
        try:
            photo_links = len(await self._photo_links())
        except Exception:
            photo_links = -1
        print(
            "Traversal diagnostic: "
            f"found={found}/{expected}, url={sig.get('url')}, "
            f"media_viewer_images={sig.get('mediaImgs')}, dialogs={sig.get('dialogs')}, "
            f"overflow={await self._overflow_count()}, visible_photo_links={photo_links}, "
            f"graphql_photos={len(self._graphql)}, next_controls={sig.get('nextish')}"
        )
