from facebook_album_downloader.post_collector import MediaCollector
from facebook_album_downloader.models import Photo


def test_scope_links_to_dominant_post_set():
    post = [
        f"https://www.facebook.com/photo/?fbid={i}&set=pcb.999"
        for i in (101, 102, 103, 104)
    ]
    unrelated = [
        "https://www.facebook.com/photo/?fbid=9001",
        "https://www.facebook.com/photo/?fbid=9002&set=a.123",
    ]
    assert MediaCollector._scope_links_to_post_set(unrelated + post) == post


def test_dedupe_uses_rendered_image_identity_not_stale_fbid():
    photos = [
        Photo(
            1,
            "https://www.facebook.com/photo/?fbid=101&set=pcb.999",
            "https://scontent.example/v/img-a.jpg?token=one",
            media_id="fbid_101",
        ),
        Photo(
            2,
            "https://www.facebook.com/photo/?fbid=102&set=pcb.999",
            "https://scontent.example/v/img-a.jpg?token=two",
            media_id="fbid_102",
        ),
        Photo(
            3,
            "https://www.facebook.com/photo/?fbid=102&set=pcb.999",
            "https://scontent.example/v/img-b.jpg?token=three",
            media_id="fbid_102",
        ),
    ]

    result = MediaCollector._dedupe_by_image(photos)
    assert len(result) == 2
    assert result[0].direct_url.endswith("token=one")
    assert result[1].direct_url.endswith("token=three")


def test_merge_unique_images_respects_expected_count():
    base = [
        Photo(1, direct_url="https://scontent.example/1.jpg"),
        Photo(2, direct_url="https://scontent.example/2.jpg"),
    ]
    extras = [
        Photo(3, direct_url="https://scontent.example/2.jpg?fresh=1"),
        Photo(4, direct_url="https://scontent.example/3.jpg"),
        Photo(5, direct_url="https://scontent.example/4.jpg"),
    ]
    result = MediaCollector._merge_unique_images(base, extras, limit=3)
    assert len(result) == 3
    assert result[-1].direct_url.endswith("3.jpg")
