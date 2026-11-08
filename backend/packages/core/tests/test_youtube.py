from core.youtube import watch_url, youtube_id

VID = "dQw4w9WgXcQ"


def test_youtube_links_give_the_video_id() -> None:
    for url in (
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&list=PL123&t=42s",
        f"http://m.youtube.com/watch?feature=share&v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"youtu.be/{VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=shared",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"  https://www.youtube.com/watch?v={VID}  ",
    ):
        assert youtube_id(url) == VID, url
    assert watch_url(VID) == f"https://www.youtube.com/watch?v={VID}"


def test_other_links_are_rejected() -> None:
    for url in (
        "https://vimeo.com/123456",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        "https://evil.example/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "not a url",
        "",
    ):
        assert youtube_id(url) is None, url
