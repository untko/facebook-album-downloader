from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .models import Photo

DEFAULT_MANIFEST = "album_urls.json"
ALT_MANIFEST = "photo_urls.json"
VALID_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif", "avif")


def sanitize_filename(name: str) -> str:
    value = re.sub(r"\s+", " ", (name or "").strip())
    value = re.sub(r'[\\/:*?"<>|]+', "-", value).strip(". ")
    value = value[:120].rstrip(". ")
    return value or "Facebook_Album"


def extract_set_id(url: str) -> str:
    if not url:
        return ""
    try:
        return parse_qs(urlparse(url).query).get("set", [""])[0]
    except Exception:
        return ""


def normalize_photo_id(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        fbid = query.get("fbid", [""])[0]
        if fbid and str(fbid).isdigit():
            return f"fbid_{fbid}"
    except Exception:
        parsed = None

    match = re.search(r"/photos/(?:[^/]+/)?(\d+)(?:/|$)", url)
    if match:
        return f"photo_{match.group(1)}"

    match = re.search(r"(?:^|[?&=/])pcb[.=](\d+)", url)
    if match:
        return f"pcb_{match.group(1)}"

    try:
        parsed = parsed or urlparse(url)
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        return clean.lower() if clean else url.strip().lower()
    except Exception:
        return url.strip().lower()


def load_manifest(path: str | os.PathLike[str]) -> dict | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None

    album_url = ""
    album_title = ""
    raw_photos: list = []

    if isinstance(data, dict) and isinstance(data.get("photos"), list):
        album_url = str(data.get("album_url") or "")
        album_title = str(data.get("album_title") or "")
        raw_photos = data["photos"]
    elif isinstance(data, list):
        raw_photos = data
    elif isinstance(data, dict):
        metadata_keys = {
            "album_url", "album_title", "total_photos", "extracted_count",
            "downloaded_count", "updated_at", "source_url",
        }
        for fb_url, direct_url in data.items():
            if fb_url in metadata_keys:
                continue
            raw_photos.append({"facebook_url": fb_url, "direct_url": direct_url})
        album_url = str(data.get("album_url") or "")
        album_title = str(data.get("album_title") or "")
    else:
        return None

    photos: list[Photo] = []
    for i, item in enumerate(raw_photos, start=1):
        if isinstance(item, str):
            photo = Photo(index=i, direct_url=item)
        elif isinstance(item, dict):
            photo = Photo.from_dict(item, i)
        else:
            continue
        if not photo.media_id and photo.facebook_url:
            photo.media_id = normalize_photo_id(photo.facebook_url)
        photos.append(photo)

    return {"album_url": album_url, "album_title": album_title, "photos": photos}


def save_manifest(path: str | os.PathLike[str], album_url: str, album_title: str, photos: list[Photo]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "album_url": album_url or "",
        "album_title": album_title or "",
        "total_photos": len(photos),
        "extracted_count": sum(bool(p.direct_url) for p in photos),
        "downloaded_count": sum(bool(p.downloaded) for p in photos),
        "photos": [p.to_dict() for p in photos],
    }
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def merge_photos(discovered: list[Photo], saved: list[Photo]) -> list[Photo]:
    saved_by_id: dict[str, Photo] = {}
    for item in saved:
        key = item.media_id or normalize_photo_id(item.facebook_url)
        if key:
            saved_by_id[key] = item

    merged: list[Photo] = []
    seen: set[str] = set()

    for item in discovered:
        key = item.media_id or normalize_photo_id(item.facebook_url) or item.direct_url
        prior = saved_by_id.get(key)
        if prior:
            item.filename = prior.filename or item.filename
            item.downloaded = prior.downloaded
            item.direct_url = item.direct_url or prior.direct_url
            item.extracted_at = item.extracted_at or prior.extracted_at
        if key:
            seen.add(key)
        merged.append(item)

    for item in saved:
        key = item.media_id or normalize_photo_id(item.facebook_url) or item.direct_url
        if key and key in seen:
            continue
        merged.append(item)
        if key:
            seen.add(key)

    for i, item in enumerate(merged, start=1):
        item.index = i
        if not item.filename:
            item.filename = f"{i}.jpg"
    return merged


def detect_downloaded(photos: list[Photo], output_dir: str | os.PathLike[str]) -> tuple[int, int]:
    root = Path(output_dir)
    found = 0
    for photo in photos:
        candidates: list[Path] = []
        if photo.filename:
            candidates.append(root / photo.filename)
        candidates.extend(root / f"{photo.index}.{ext}" for ext in VALID_EXTENSIONS)
        existing = next((p for p in candidates if p.exists() and p.stat().st_size > 0), None)
        if existing:
            photo.filename = existing.name
            photo.downloaded = True
            found += 1
        else:
            photo.downloaded = False
    return found, len(photos) - found


def manifest_path(output_dir: str | os.PathLike[str], custom: str | None = None) -> Path:
    if custom:
        return Path(custom)
    root = Path(output_dir)
    primary = root / DEFAULT_MANIFEST
    alternate = root / ALT_MANIFEST
    if primary.exists():
        return primary
    if alternate.exists():
        return alternate
    return primary
