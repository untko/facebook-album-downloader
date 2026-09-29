import json
from pathlib import Path

from facebook_album_downloader.manifest import (
    detect_downloaded,
    extract_set_id,
    load_manifest,
    merge_photos,
    normalize_photo_id,
    save_manifest,
)
from facebook_album_downloader.models import Photo


def test_normalize_photo_ids():
    assert normalize_photo_id("https://www.facebook.com/photo/?fbid=123&set=pcb.999") == "fbid_123"
    assert normalize_photo_id("https://www.facebook.com/user/photos/a.11/456/") == "photo_456"
    assert extract_set_id("https://www.facebook.com/photo/?fbid=123&set=pcb.999") == "pcb.999"


def test_manifest_roundtrip_and_legacy_mapping(tmp_path: Path):
    path = tmp_path / "album_urls.json"
    photos = [Photo(1, "https://www.facebook.com/photo/?fbid=1", "https://scontent/x.jpg", "1.jpg", True, "fbid_1")]
    save_manifest(path, "https://facebook.com/post", "Test", photos)
    loaded = load_manifest(path)
    assert loaded and loaded["album_title"] == "Test"
    assert loaded["photos"][0].media_id == "fbid_1"

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"https://facebook.com/photo/?fbid=2": "https://scontent/y.jpg"}))
    loaded_legacy = load_manifest(legacy)
    assert loaded_legacy and loaded_legacy["photos"][0].media_id == "fbid_2"


def test_merge_preserves_download_state_and_prefers_fresh_direct_url():
    saved = [Photo(1, "https://facebook.com/photo/?fbid=1", "https://old", "1.jpg", True, "fbid_1")]
    discovered = [Photo(1, "https://facebook.com/photo/?fbid=1", "https://fresh", "", False, "fbid_1")]
    merged = merge_photos(discovered, saved)
    assert merged[0].direct_url == "https://fresh"
    assert merged[0].downloaded is True
    assert merged[0].filename == "1.jpg"


def test_detect_downloaded_rejects_zero_byte_files(tmp_path: Path):
    (tmp_path / "1.jpg").write_bytes(b"ok")
    (tmp_path / "2.jpg").write_bytes(b"")
    photos = [Photo(1, filename="1.jpg"), Photo(2, filename="2.jpg")]
    found, pending = detect_downloaded(photos, tmp_path)
    assert (found, pending) == (1, 1)
    assert photos[0].downloaded is True
    assert photos[1].downloaded is False
