from facebook_album_downloader.collector import direct_media_id, is_photo_url


def test_photo_url_detection():
    assert is_photo_url("https://www.facebook.com/photo/?fbid=123&set=pcb.1")
    assert is_photo_url("https://www.facebook.com/user/photos/a.1/2/")
    assert not is_photo_url("https://www.facebook.com/some-post")


def test_direct_media_id_stable_across_token_changes():
    a = direct_media_id("https://scontent.xx.fbcdn.net/v/image.jpg?token=one")
    b = direct_media_id("https://scontent.xx.fbcdn.net/v/image.jpg?token=two")
    assert a == b
