from __future__ import annotations

import time

from .collector_v3 import MediaCollector as StableMediaCollector
from .manifest import extract_set_id
from .models import Photo


class MediaCollector(StableMediaCollector):
    """Lock multi-photo traversal to the post that owns the visible +N tile."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.target_set_id = ""
        self.expected_count: int | None = None
        self.complete = False

    async def _post_media_context(self) -> dict | None:
        try:
            return await self.page.evaluate(r"""
                () => {
                  const visible = el => {
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    return r.width >= 12 && r.height >= 12 && r.bottom > 0 && r.right > 0 &&
                      r.top < innerHeight && r.left < innerWidth && s.display !== 'none' &&
                      s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
                  };
                  const setId = href => {
                    try { return new URL(href, location.href).searchParams.get('set') || ''; }
                    catch (_) { return ''; }
                  };
                  const photoAnchor = a => {
                    if (!a || !a.href) return false;
                    const h = a.href;
                    return h.includes('fbid=') || h.includes('/photo/') || h.includes('/photos/');
                  };
                  const largeTile = a => {
                    if (!visible(a) || !photoAnchor(a)) return false;
                    const r = a.getBoundingClientRect();
                    return r.width >= 90 && r.height >= 90 && !!a.querySelector('img');
                  };

                  const overflowHits = [];
                  for (const el of document.querySelectorAll('body *')) {
                    const text = (el.textContent || '').trim();
                    const m = text.match(/^\+(\d{1,5})$/);
                    if (!m || el.children.length > 4 || !visible(el)) continue;
                    const clickable = el.closest('a[href],[role="button"],button') || el.parentElement || el;
                    const r = clickable.getBoundingClientRect();
                    if (r.width < 20 || r.height < 20) continue;
                    const anchor = el.closest('a[href]') || (clickable.closest ? clickable.closest('a[href]') : null);
                    overflowHits.push({el, clickable, anchor, n:Number(m[1]), area:r.width*r.height});
                  }
                  if (!overflowHits.length) return null;
                  overflowHits.sort((a,b) => b.n-a.n || b.area-a.area);
                  const hit = overflowHits[0];

                  let targetSet = hit.anchor ? setId(hit.anchor.href) : '';
                  let node = hit.el;
                  let chosen = null;
                  for (let depth=0; depth<16 && node; depth++, node=node.parentElement) {
                    const all = [...node.querySelectorAll('a[href]')].filter(largeTile);
                    if (!all.length) continue;
                    if (!targetSet) {
                      const counts = new Map();
                      for (const a of all) {
                        const sid = setId(a.href);
                        if (sid && sid.startsWith('pcb.')) counts.set(sid, (counts.get(sid)||0)+1);
                      }
                      const ranked = [...counts.entries()].sort((a,b)=>b[1]-a[1]);
                      if (ranked.length) targetSet = ranked[0][0];
                    }
                    const same = all.filter(a => !targetSet || setId(a.href) === targetSet);
                    if (same.length >= 2 && same.length <= 8) {
                      chosen = {node, anchors:same};
                      break;
                    }
                  }
                  if (!chosen) return null;

                  const uniq = [], seen = new Set();
                  for (const a of chosen.anchors) {
                    const href = a.href;
                    const m = href.match(/[?&]fbid=(\d+)/);
                    const key = m ? m[1] : href;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const r = a.getBoundingClientRect();
                    uniq.push({href, x:r.left+r.width/2, y:r.top+r.height/2, area:r.width*r.height});
                  }
                  uniq.sort((a,b)=>b.area-a.area);
                  const cr = hit.clickable.getBoundingClientRect();
                  return {
                    overflow: hit.n,
                    set_id: targetSet,
                    links: uniq.map(x=>x.href),
                    first_point: uniq.length ? {x:uniq[0].x,y:uniq[0].y} : null,
                    overflow_point: {x:cr.left+cr.width/2,y:cr.top+cr.height/2}
                  };
                }
            """)
        except Exception:
            return None

    async def _wait_target_viewer(self, before: dict, target_set: str, timeout_s: float = 6.0) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            await self.page.wait_for_timeout(150)
            current_set = extract_set_id(self.page.url)
            after = await self._viewer_signature()
            transitioned = (
                after.get("url") != before.get("url") or
                after.get("mediaImgs", 0) > before.get("mediaImgs", 0)
            )
            if transitioned:
                if target_set and current_set and current_set != target_set:
                    return False
                if await self._displayed_media():
                    return True
        return False

    async def _click_context_point(self, point: dict | None, target_set: str) -> bool:
        if not point:
            return False
        before = await self._viewer_signature()
        try:
            await self.page.mouse.click(float(point["x"]), float(point["y"]))
        except Exception:
            return False
        return await self._wait_target_viewer(before, target_set)

    async def collect(self, progress_hook=None) -> list[Photo]:
        context = await self._post_media_context()
        if not context or not context.get("overflow"):
            result = await super().collect(progress_hook=progress_hook)
            self.complete = bool(result)
            return result

        overflow = int(context["overflow"])
        links = list(dict.fromkeys(context.get("links") or []))
        self.target_set_id = str(context.get("set_id") or "")
        self.expected_count = len(links) + overflow if links else None

        all_links = await self._photo_links()
        print(
            f"Detected {len(all_links)} photo link(s) in the source DOM; "
            f"{len(links)} are visible tiles in the +{overflow} post collage."
        )
        if self.target_set_id:
            print(f"Locked post media set: {self.target_set_id}")
        suffix = f"; expecting {self.expected_count} media item(s)" if self.expected_count else ""
        print(f"Detected +{overflow} hidden-media overflow{suffix}.")

        discovered: list[Photo] = []
        opened = await self._click_context_point(context.get("overflow_point"), self.target_set_id)
        if not opened:
            print("Scoped +N click did not open the target viewer; trying a photo tile from the same collage.")
            await self._goto(self.source_url)
            await self.page.wait_for_timeout(350)
            context = await self._post_media_context() or context
            opened = await self._click_context_point(context.get("first_point"), self.target_set_id)

        if opened:
            current_set = extract_set_id(self.page.url)
            if self.target_set_id and current_set and current_set != self.target_set_id:
                print(f"Refusing unrelated media set {current_set}; expected {self.target_set_id}.")
                return []
            discovered = await self._traverse_stable(progress_hook, self.expected_count)

        await self._drain()
        graph = [p for p in self._graphql.values()
                 if not self.target_set_id or extract_set_id(p.facebook_url) == self.target_set_id]
        if graph:
            print(f"GraphQL media discovery in locked set: {len(graph)} photo record(s).")
        if not self.expected_count or len(discovered) < self.expected_count:
            discovered = self._merge_unique_images(discovered, graph, self.expected_count)

        discovered = [p for p in self._dedupe_by_image(discovered)
                      if not self.target_set_id or not extract_set_id(p.facebook_url)
                      or extract_set_id(p.facebook_url) == self.target_set_id]
        if self.expected_count and len(discovered) > self.expected_count:
            discovered = discovered[:self.expected_count]

        self.complete = bool(discovered) and (
            self.expected_count is None or len(discovered) == self.expected_count
        )
        if self.expected_count and not self.complete:
            await self._print_diagnostic(self.expected_count, len(discovered))

        for i, p in enumerate(discovered, 1):
            p.index = i
            p.filename = f"{i}.jpg"
        return discovered

    async def _current_record(self) -> Photo | None:
        p = await super()._current_record()
        if not p:
            return None
        if self.target_set_id:
            current_set = extract_set_id(self.page.url)
            record_set = extract_set_id(p.facebook_url)
            if current_set and current_set != self.target_set_id:
                return None
            if record_set and record_set != self.target_set_id:
                return None
        return p
