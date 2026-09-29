from facebook_album_downloader.collector import extract_graphql_photo_records


def test_extract_graphql_photo_records_from_attachment_payload():
    payload = {
        "data": {
            "story": {
                "attachments": {
                    "edges": [
                        {
                            "node": {
                                "media": {
                                    "__typename": "Photo",
                                    "id": "111",
                                    "url": "https://www.facebook.com/photo/?fbid=111&set=pcb.999",
                                    "image": {
                                        "width": 2048,
                                        "height": 1536,
                                        "uri": "https://scontent.xx.fbcdn.net/large-111.jpg?token=x",
                                    },
                                }
                            }
                        },
                        {
                            "node": {
                                "media": {
                                    "__typename": "Photo",
                                    "id": "222",
                                    "url": "https://www.facebook.com/photo/?fbid=222&set=pcb.999",
                                    "image": {
                                        "width": 1280,
                                        "height": 720,
                                        "uri": "https://scontent.xx.fbcdn.net/large-222.jpg?token=y",
                                    },
                                }
                            }
                        },
                    ]
                }
            }
        }
    }

    photos = extract_graphql_photo_records(payload)
    assert [p.media_id for p in photos] == ["fbid_111", "fbid_222"]
    assert photos[0].direct_url.startswith("https://scontent.xx.fbcdn.net/large-111.jpg")
    assert photos[1].facebook_url.endswith("fbid=222&set=pcb.999")


def test_graphql_photo_discovery_ignores_non_photo_images():
    payload = {
        "data": {
            "actor": {
                "__typename": "User",
                "profile_picture": {
                    "uri": "https://scontent.xx.fbcdn.net/avatar.jpg"
                },
            }
        }
    }
    assert extract_graphql_photo_records(payload) == []
