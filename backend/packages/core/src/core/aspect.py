"""Output size from an aspect label, and the crop window size (architecture.md section 7)."""

import re

PRESETS = ("9:16", "1:1", "4:5", "16:9", "21:9")
SHORT_SIDE = 1080
MIN_SIDE = 240
MAX_SIDE = 4096

_ASPECT = re.compile(r"^(\d+):(\d+)$")


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def parse_aspect(aspect: str) -> tuple[int, int]:
    m = _ASPECT.match(aspect)
    if not m or int(m.group(1)) == 0 or int(m.group(2)) == 0:
        raise ValueError(f"aspect must look like W:H, got {aspect!r}")
    return int(m.group(1)), int(m.group(2))


def output_size(aspect: str, short_side: int = SHORT_SIDE) -> tuple[int, int]:
    """Default size for an aspect: short side 1080, both sides even, long side capped at 4096."""
    w, h = parse_aspect(aspect)
    if w >= h:
        width, height = _even(short_side * w / h), short_side
    else:
        width, height = short_side, _even(short_side * h / w)
    if max(width, height) > MAX_SIDE:
        scale = MAX_SIDE / max(width, height)
        width, height = _even(width * scale), _even(height * scale)
    return width, height


def crop_window(src_w: int, src_h: int, out_w: int, out_h: int, zoom: float = 1.0) -> tuple[float, float]:
    """Crop window size as fractions of the source frame: w0 = min(1, T/S), h0 = min(1, S/T), divided by zoom."""
    s = src_w / src_h
    t = out_w / out_h
    return min(1.0, t / s) / zoom, min(1.0, s / t) / zoom
