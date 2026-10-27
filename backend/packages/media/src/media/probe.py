"""ffprobe a source file (ingest stage 1)."""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from media.ff import FFPROBE, run_json


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    width: int  # as displayed, after rotation
    height: int
    fps: float
    has_video: bool
    has_audio: bool


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(float(side["rotation"])) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag else 0


def _fps(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if value and value != "0/0":
            try:
                f = Fraction(value)
                if f > 0:
                    return round(float(f), 3)
            except (ValueError, ZeroDivisionError):
                continue
    return 30.0


def parse_probe(data: dict[str, Any]) -> ProbeResult:
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not _is_cover(s)), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or (video or {}).get("duration") or 0)
    width = height = 0
    fps = 30.0
    if video is not None:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if _rotation(video) in (90, 270):
            width, height = height, width
        fps = _fps(video)
    return ProbeResult(
        duration_s=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=video is not None,
        has_audio=audio is not None,
    )


def _is_cover(stream: dict[str, Any]) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def probe(path: str | Path) -> ProbeResult:
    data = run_json([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return parse_probe(data)
