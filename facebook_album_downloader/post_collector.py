from __future__ import annotations

from .collector_v4 import MediaCollector as ScopedMediaCollector


class MediaCollector(ScopedMediaCollector):
    """Collector for Facebook posts with scoped +N overflow handling.

    In Comet multi-photo posts the +N label occupies one media tile. Facebook may
    render that label in a wrapper whose bounding box does not overlap the underlying
    photo anchor, so geometric covered-tile detection can be ambiguous.
    """

    async def _post_media_context(self) -> dict | None:
        context = await super()._post_media_context()
        if not context:
            return context

        links = context.get("links") or []
        set_id = str(context.get("set_id") or "")
        overflow = int(context.get("overflow") or 0)
        covered = int(context.get("covered_tiles") or 0)

        # For Facebook post media sets, +N represents the media occupying the
        # overlay tile plus the remaining hidden media. If DOM geometry cannot
        # associate the overlay with its underlying anchor, infer one covered tile.
        if overflow > 0 and len(links) >= 2 and set_id.startswith("pcb.") and covered == 0:
            context["covered_tiles"] = 1
            context["covered_tiles_inferred"] = True

        return context
