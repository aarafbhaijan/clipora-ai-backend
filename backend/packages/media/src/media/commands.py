"""ffmpeg argument builders for ingest (architecture.md section 10). Pure: they only build lists."""

from media.ff import ffmpeg, ffmpeg_pipe


# Scale so the short side is N px, keep aspect, both sides even.
def short_side(n: int) -> str:
    return f"scale='if(gte(iw,ih),-2,{n})':'if(gte(iw,ih),{n},-2)'"


PROXY_SHORT_SIDE = 720  # the editor zooms into the frame for 9:16 and 4:5; 360 px looked blurry there
PROXY_CRF = 23


def proxy(src: str, out: str, short: int = PROXY_SHORT_SIDE) -> list[str]:
    """Editor copy: short side `short` (never more than the source's), H.264 CRF 23, GOP 48 with no scene-cut
    keyframes, for fast exact seeking."""
    return ffmpeg(
        "-i", src,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"{short_side(short)}:flags=lanczos,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(PROXY_CRF),
        "-g", "48", "-keyint_min", "48", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        out,
    )  # fmt: skip


def speech_wav(src: str, out: str) -> list[str]:
    """16 kHz mono PCM for Whisper."""
    return ffmpeg("-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out)


SPRITE_COLUMNS = 10
SPRITE_ROWS = 10
THUMB_INTERVAL_S = 2.0
THUMB_SHORT_SIDE = 90


def sprites(src: str, out_pattern: str) -> list[str]:
    """One frame every 2 s, short side 90 px, tiled 10x10 per sheet (sprite_000.jpg, ...)."""
    return ffmpeg(
        "-i", src,
        "-map", "0:v:0",
        "-vf", f"fps=1/{THUMB_INTERVAL_S:g},{short_side(THUMB_SHORT_SIDE)},setsar=1,"
               f"tile={SPRITE_COLUMNS}x{SPRITE_ROWS}",
        "-q:v", "5", "-start_number", "0",
        out_pattern,
    )  # fmt: skip


def poster(src: str, out: str, at_s: float) -> list[str]:
    return ffmpeg(
        "-ss", f"{at_s:.3f}", "-i", src, "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"{short_side(360)},setsar=1", "-q:v", "4", out,
    )  # fmt: skip


WAVEFORM_RATE = 8000


def waveform_pcm(src: str) -> list[str]:
    """Mono signed 16-bit PCM at 8 kHz on stdout, for min/max buckets."""
    return ffmpeg_pipe("-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(WAVEFORM_RATE), "-f", "s16le", "-")


DETECT_FPS = 3
DETECT_SHORT_SIDE = 640


def detect_frames(
    src: str, width: int, height: int, fps: float = DETECT_FPS, short: int = DETECT_SHORT_SIDE
) -> tuple[list[str], int, int]:
    """Raw BGR frames (3 fps for YOLO, more for lip reading) on stdout. Returns (args, frame width, frame height)."""
    short = max(2, short // 2 * 2)
    if width >= height:
        h = short
        w = max(2, round(width * h / height / 2) * 2)
    else:
        w = short
        h = max(2, round(height * w / width / 2) * 2)
    args = ffmpeg_pipe(
        "-i", src, "-map", "0:v:0", "-vf", f"fps={fps},scale={w}:{h},setsar=1",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    )  # fmt: skip
    return args, w, h


def tile_size(width: int, height: int, short: int = THUMB_SHORT_SIDE) -> tuple[int, int]:
    """Size of one thumbnail tile, matching `short_side` (even long side)."""
    if width >= height:
        return max(2, round(width * short / height / 2) * 2), short
    return short, max(2, round(height * short / width / 2) * 2)
