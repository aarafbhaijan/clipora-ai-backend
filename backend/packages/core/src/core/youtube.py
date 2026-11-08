"""YouTube links: which video a pasted link points at. Pure, so the API can reject a bad link before any work.

Accepted: youtube.com/watch?v=ID (any of www., m., music.), youtu.be/ID, and /shorts/, /live/, /embed/ paths.
Playlist, time and tracking parameters are ignored: one link imports one video.
"""

import re
from urllib.parse import parse_qs, urlparse

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
          "www.youtube-nocookie.com"}  # fmt: skip
_PATHS = ("shorts", "live", "embed", "v")


def youtube_id(url: str) -> str | None:
    """The 11-character video id of a YouTube link, or None when it isn't one."""
    text = url.strip()
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    try:
        u = urlparse(text)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    candidate: str | None = None
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parts[0] if parts else None
    elif host in _HOSTS:
        if parts[:1] == ["watch"]:
            values = parse_qs(u.query).get("v")
            candidate = values[0] if values else None
        elif len(parts) >= 2 and parts[0] in _PATHS:
            candidate = parts[1]
    return candidate if candidate and _ID.match(candidate) else None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
