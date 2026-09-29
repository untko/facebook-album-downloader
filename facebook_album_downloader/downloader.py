from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import BrowserContext

from .models import Photo

RefreshCallback = Callable[[Photo], Awaitable[str | None]]
SaveCallback = Callable[[], Awaitable[None]]


def extension_from_content_type(content_type: str, fallback: str = "jpg") -> str:
    lower = (content_type or "").lower()
    if "jpeg" in lower or "jpg" in lower:
        return "jpg"
    if "png" in lower:
        return "png"
    if "webp" in lower:
        return "webp"
    if "gif" in lower:
        return "gif"
    if "avif" in lower:
        return "avif"
    return fallback


class ImageDownloader:
    def __init__(
        self,
        context: BrowserContext,
        *,
        output_dir: str,
        workers: int = 6,
        timeout_seconds: float = 30,
        resume: bool = True,
    ) -> None:
        self.context = context
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_ms = int(timeout_seconds * 1000)
        self.resume = resume
        self._sem = asyncio.Semaphore(max(1, workers))

    async def download_all(
        self,
        photos: list[Photo],
        *,
        refresh: RefreshCallback | None = None,
        save: SaveCallback | None = None,
    ) -> tuple[int, int, int]:
        results = await asyncio.gather(*(self._download_one(photo) for photo in photos))
        expired = [photo for photo, status in zip(photos, results) if status == "expired"]

        if expired and refresh:
            for photo in expired:
                fresh = await refresh(photo)
                if fresh:
                    photo.direct_url = fresh
                    await self._download_one(photo, force=True)
                if save:
                    await save()

        if save:
            await save()

        successful = sum(1 for p in photos if p.downloaded)
        skipped = sum(1 for status in results if status == "skipped")
        failed = len(photos) - successful
        return successful, failed, skipped

    async def _download_one(self, photo: Photo, *, force: bool = False) -> str:
        if not force and self.resume:
            existing = self._existing_file(photo)
            if existing:
                photo.filename = existing.name
                photo.downloaded = True
                return "skipped"

        if not photo.direct_url:
            return "missing"

        async with self._sem:
            for attempt in range(3):
                response = None
                try:
                    response = await self.context.request.get(
                        photo.direct_url,
                        timeout=self.timeout_ms,
                        fail_on_status_code=False,
                    )
                    if response.status == 200:
                        body = await response.body()
                        content_type = response.headers.get("content-type", "")
                        fallback = self._filename_extension(photo.filename) or "jpg"
                        ext = extension_from_content_type(content_type, fallback)
                        filename = photo.filename or f"{photo.index}.{ext}"
                        if "." not in filename:
                            filename = f"{filename}.{ext}"
                        elif not photo.downloaded and self._filename_extension(filename) not in {"jpg", "jpeg", "png", "webp", "gif", "avif"}:
                            filename = f"{photo.index}.{ext}"
                        target = self.output_dir / filename
                        part = target.with_name(target.name + ".part")
                        part.write_bytes(body)
                        os.replace(part, target)
                        photo.filename = target.name
                        photo.downloaded = True
                        return "downloaded"
                    if response.status in {403, 404, 410}:
                        return "expired"
                except Exception:
                    pass
                finally:
                    if response:
                        try:
                            await response.dispose()
                        except Exception:
                            pass
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
        return "failed"

    def _existing_file(self, photo: Photo) -> Path | None:
        names: list[str] = []
        if photo.filename:
            names.append(photo.filename)
        names.extend(f"{photo.index}.{ext}" for ext in ("jpg", "jpeg", "png", "webp", "gif", "avif"))
        for name in names:
            candidate = self.output_dir / name
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    @staticmethod
    def _filename_extension(filename: str) -> str:
        if "." not in (filename or ""):
            return ""
        return filename.rsplit(".", 1)[-1].lower()
