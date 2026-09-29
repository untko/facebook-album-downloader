from __future__ import annotations

import time

from .collector import MediaCollector as BaseMediaCollector
from .manifest import normalize_photo_id
from .models import Photo


class MediaCollector(BaseMediaCollector):
    """Post-aware collector with verified in-place Facebook viewer navigation.

    Facebook multi-photo posts often expose only five anchors in the DOM and keep
    the rest behind a +N tile. Navigating directly to one anchor loses the post's
    attachment sequence, so this collector preserves UI context with real mouse
    clicks and verifies that a photo viewer actually opened.
    """

    async def collect(self, progress_hook=None) -> list[Photo]:
        links = await self._photo_links()
        overflow = await self._overflow_count()
        if not overflow:
            links = await self._collect_scrolled_links()
        print(f"Detected {len(links)} photo link(s) in the source DOM.")

        expected = len(links) + overflow if overflow else None
        discovered: list[Photo] = []

        if overflow:
            print(f"Detected +{overflow} hidden-media overflow; expecting about {expected} media item(s).")

            # Clicking a visible photo is usually more reliable than clicking the +N
            # overlay because it definitely enters the Comet media viewer while keeping
            # the current post's attachment context.
            opened = await self._open_visible_photo_in_place(links)
            if not opened:
                print("Visible photo click did not enter a media viewer; trying the +N tile.")
                opened = await self._open_overflow_verified(overflow)

            if opened:
                await self.page.wait_for_timeout(700)
                await self._drain()
                discovered = await self._traverse_open(progress_hook, expected)

            # One alternate path is worth trying because Facebook can render +N as an
            # intermediate surface rather than the actual viewer.
            if expected and len(discovered) < expected:
                await self._goto(self.source_url)
                retry_links = await self._photo_links() or links
                opened = await self._open_overflow_verified(overflow)
                if not opened:
                    opened = await self._open_visible_photo_in_place(retry_links)
                if opened:
                    await self.page.wait_for_timeout(700)
                    retry = await self._traverse_open(progress_hook, expected)
                    discovered = self._merge(discovered, retry)

        if not discovered and links:
            if await self._open_visible_photo_in_place(links):
                discovered = await self._traverse_open(progress_hook, expected)
            if not discovered:
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

        if expected and len(discovered) < expected:
            await self._print_diagnostic(expected, len(discovered))

        for i, p in enumerate(discovered, 1):
            p.index = i
            p.filename = p.filename or f"{i}.jpg"
        return discovered

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
                    return r.width>20 && r.height>20 && r.bottom>0 && r.right>0 &&
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

    async def _advance(self, before: str) -> bool:
        # Accessible next controls, if present.
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
                    if not before or await self._wait_changed(before, 3500):
                        return True
            except Exception:
                pass

        # Unlabelled Comet arrow: choose the clickable control closest to right-middle.
        try:
            point = await self.page.evaluate(r"""
                () => {
                  const w=innerWidth,h=innerHeight;
                  const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return
                    r.width>=20&&r.height>=20&&r.width<=180&&r.height<=180&&r.bottom>0&&r.right>0&&
                    r.top<h&&r.left<w&&s.display!=='none'&&s.visibility!=='hidden'};
                  const xs=[...document.querySelectorAll('button,[role="button"],a')].filter(visible)
                    .map(el=>{const r=el.getBoundingClientRect();return {r,score:Math.abs((r.top+r.bottom)/2-h/2)+(w-r.right)*0.35}})
                    .filter(x=>x.r.left>w*0.60&&(x.r.top+x.r.bottom)/2>h*0.18&&(x.r.top+x.r.bottom)/2<h*0.82)
                    .sort((a,b)=>a.score-b.score);
                  if(!xs.length)return null; const r=xs[0].r; return {x:r.left+r.width/2,y:r.top+r.height/2};
                }
            """)
            if point:
                await self.page.mouse.click(float(point["x"]), float(point["y"]))
                if not before or await self._wait_changed(before, 3000):
                    return True
        except Exception:
            pass

        # Edge-click fallback, then keyboard fallback.
        try:
            vp = await self.page.evaluate("({w:innerWidth,h:innerHeight})")
            for xfrac in (0.94, 0.90, 0.86):
                await self.page.mouse.click(vp["w"] * xfrac, vp["h"] * 0.50)
                if not before or await self._wait_changed(before, 2200):
                    return True
        except Exception:
            pass
        try:
            await self.page.locator("body").focus()
            await self.page.keyboard.press("ArrowRight")
            if not before or await self._wait_changed(before, 3000):
                return True
        except Exception:
            pass
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
