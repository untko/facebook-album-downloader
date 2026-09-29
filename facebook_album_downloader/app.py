from __future__ import annotations

from pathlib import Path

from .browser import BrowserSession
from .collector import MediaCollector
from .downloader import ImageDownloader
from .manifest import detect_downloaded, load_manifest, manifest_path, merge_photos, sanitize_filename, save_manifest
from .models import Photo


async def run(
    source: str | None,
    *,
    output: str = "downloadedImgs",
    login: bool = False,
    headless: bool = False,
    state_path: str = "facebook_cookies.json",
    use_state: bool = True,
    urls_file: str | None = None,
    resume: bool = True,
    urls_only: bool = False,
    max_scrolls: int = 60,
    scroll_delay: float = 1.0,
    max_photos: int = 5000,
    workers: int = 6,
    browser_name: str = "chromium",
    timeout_seconds: float = 30,
) -> bool:
    if login and headless:
        headless = False

    if source and source.lower().endswith(".json") and Path(source).exists():
        return await _run_from_manifest(
            source, output=output, login=login, headless=headless,
            state_path=state_path, use_state=use_state, resume=resume,
            urls_only=urls_only, workers=workers, browser_name=browser_name,
            timeout_seconds=timeout_seconds, max_scrolls=max_scrolls,
            scroll_delay=scroll_delay, max_photos=max_photos,
        )

    if not source:
        if not login:
            return False
        async with BrowserSession(
            browser_name=browser_name, headless=False, state_path=state_path,
            use_state=use_state, timeout_seconds=timeout_seconds,
        ) as session:
            return await session.login()

    async with BrowserSession(
        browser_name=browser_name, headless=headless, state_path=state_path,
        use_state=use_state, timeout_seconds=timeout_seconds,
    ) as session:
        assert session.page and session.context
        if login and not await session.is_authenticated():
            if not await session.login():
                return False

        collector = MediaCollector(
            session.page, timeout_seconds=timeout_seconds, max_scrolls=max_scrolls,
            scroll_delay=scroll_delay, max_photos=max_photos,
        )
        title = sanitize_filename(await collector.open(source))
        output_dir = Path(output) / title
        output_dir.mkdir(parents=True, exist_ok=True)
        target_manifest = manifest_path(output_dir, urls_file)
        saved_data = load_manifest(target_manifest) if resume else None
        saved_photos = saved_data["photos"] if saved_data else []

        async def persist_partial(discovered: list[Photo]) -> None:
            copies = [Photo.from_dict(p.to_dict(), p.index) for p in discovered]
            save_manifest(target_manifest, source, title, merge_photos(copies, saved_photos))

        discovered = await collector.collect(progress_hook=persist_partial)
        if not discovered and not saved_photos:
            print("No media found. The content may be inaccessible or Facebook's viewer structure may have changed.")
            return False

        photos = merge_photos(discovered, saved_photos)
        if resume:
            found, pending = detect_downloaded(photos, output_dir)
            if found:
                print(f"Resume: {found} file(s) already present; {pending} pending.")
        save_manifest(target_manifest, source, title, photos)
        print(f"Manifest: {target_manifest}")

        if urls_only:
            return bool(photos)

        downloader = ImageDownloader(
            session.context, output_dir=str(output_dir), workers=workers,
            timeout_seconds=timeout_seconds, resume=resume,
        )

        async def refresh(photo: Photo) -> str | None:
            if not photo.facebook_url:
                return None
            fresh = await collector.extract_single(photo.facebook_url)
            return fresh.direct_url if fresh else None

        async def persist() -> None:
            save_manifest(target_manifest, source, title, photos)

        successful, failed, skipped = await downloader.download_all(
            photos, refresh=refresh, save=persist
        )
        print(f"Download summary: {successful} completed ({skipped} skipped), {failed} failed")
        return successful > 0 and failed == 0


async def _run_from_manifest(
    manifest_file: str,
    *,
    output: str,
    login: bool,
    headless: bool,
    state_path: str,
    use_state: bool,
    resume: bool,
    urls_only: bool,
    workers: int,
    browser_name: str,
    timeout_seconds: float,
    max_scrolls: int,
    scroll_delay: float,
    max_photos: int,
) -> bool:
    data = load_manifest(manifest_file)
    if not data or not data["photos"]:
        print(f"Could not load media from {manifest_file}")
        return False
    photos: list[Photo] = data["photos"]
    title = sanitize_filename(data.get("album_title") or Path(manifest_file).parent.name)
    source_url = data.get("album_url") or ""
    output_dir = Path(manifest_file).parent
    if str(output_dir) in {"", "."}:
        output_dir = Path(output) / title
    output_dir.mkdir(parents=True, exist_ok=True)

    if resume:
        found, pending = detect_downloaded(photos, output_dir)
        if pending == 0 and found == len(photos):
            print(f"All {found} media files are already downloaded.")
            return True
    if urls_only:
        return True

    async with BrowserSession(
        browser_name=browser_name, headless=headless, state_path=state_path,
        use_state=use_state, timeout_seconds=timeout_seconds,
    ) as session:
        assert session.page and session.context
        if login and not await session.is_authenticated():
            if not await session.login():
                return False
        collector = MediaCollector(
            session.page, timeout_seconds=timeout_seconds, max_scrolls=max_scrolls,
            scroll_delay=scroll_delay, max_photos=max_photos,
        )
        for photo in photos:
            if not photo.direct_url and photo.facebook_url:
                fresh = await collector.extract_single(photo.facebook_url)
                if fresh:
                    photo.direct_url = fresh.direct_url
                    save_manifest(manifest_file, source_url, title, photos)

        downloader = ImageDownloader(
            session.context, output_dir=str(output_dir), workers=workers,
            timeout_seconds=timeout_seconds, resume=resume,
        )

        async def refresh(photo: Photo) -> str | None:
            if not photo.facebook_url:
                return None
            fresh = await collector.extract_single(photo.facebook_url)
            return fresh.direct_url if fresh else None

        async def persist() -> None:
            save_manifest(manifest_file, source_url, title, photos)

        successful, failed, skipped = await downloader.download_all(
            photos, refresh=refresh, save=persist
        )
        print(f"Download summary: {successful} completed ({skipped} skipped), {failed} failed")
        return successful > 0 and failed == 0
