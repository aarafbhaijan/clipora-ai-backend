"""Export plan: the ffmpeg commands that render an EDL (architecture.md section 13). Pure, so it is golden-tested.

Pass 1 (prepare) is the handler's job. Pass 2 renders each segment to the same size, frame rate and
timebase. Pass 3 joins them (concat demuxer when all boundaries are cuts, else xfade/acrossfade chains
joined by the concat filter), burns captions, normalises loudness and encodes the final MP4.
"""

from dataclasses import dataclass

from core.aspect import crop_window
from core.edl import EDL, CropKeyframe, Segment
from core.enums import Framing
from core.timemap import output_duration, segment_timings
from core.transitions import CATALOGUE, TransitionType
from media.ff import ffmpeg

OUT_FPS = 30
TIMESCALE = 15360
AUDIO_RATE = 48000
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


@dataclass(frozen=True)
class SegmentStep:
    index: int
    length: float
    out_name: str
    cmd_name: str | None
    cmd_text: str | None
    args: list[str]


@dataclass(frozen=True)
class ExportPlan:
    segments: list[SegmentStep]
    final_args: list[str]
    concat_list: str | None  # contents of segments.txt when all boundaries are cuts
    output_duration: float


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def _interp(crop: list[CropKeyframe], t: float) -> tuple[float, float]:
    if t <= crop[0].t:
        return crop[0].cx, crop[0].cy
    for a, b in zip(crop, crop[1:], strict=False):
        if a.t <= t <= b.t:
            f = 0.0 if b.t == a.t else (t - a.t) / (b.t - a.t)
            return a.cx + (b.cx - a.cx) * f, a.cy + (b.cy - a.cy) * f
    return crop[-1].cx, crop[-1].cy


def crop_size(edl: EDL) -> tuple[int, int]:
    """Crop window in source pixels, even, inside the frame."""
    w0, h0 = crop_window(edl.source.width, edl.source.height, edl.output.width, edl.output.height)
    cw = min(_even(w0 * edl.source.width), edl.source.width - edl.source.width % 2)
    ch = min(_even(h0 * edl.source.height), edl.source.height - edl.source.height % 2)
    return cw, ch


def crop_positions(edl: EDL, seg: Segment) -> list[tuple[float, int, int]]:
    """(time from segment start, x, y) for every output frame, from the source-time keyframes."""
    cw, ch = crop_size(edl)
    sw, sh = edl.source.width, edl.source.height
    frames = max(1, round(seg.length * OUT_FPS))
    out = []
    for n in range(frames):
        t = n / OUT_FPS
        cx, cy = _interp(seg.crop, seg.src_start + t) if seg.crop else (0.5, 0.5)
        x = min(max(0, round(cx * sw - cw / 2)), sw - cw)
        y = min(max(0, round(cy * sh - ch / 2)), sh - ch)
        out.append((t, x, y))
    return out


def sendcmd_text(positions: list[tuple[float, int, int]]) -> str:
    """One command line per frame where the window moves."""
    lines = []
    last: tuple[int, int] | None = None
    for t, x, y in positions:
        if (x, y) != last:
            lines.append(f"{t:.4f} crop x {x}, crop y {y};")
            last = (x, y)
    return "\n".join(lines) + "\n"


def _video_filter(edl: EDL, cmd_name: str | None, x0: int, y0: int) -> str:
    ow, oh = edl.output.width, edl.output.height
    tail = f"setsar=1,fps={OUT_FPS},format=yuv420p"
    if edl.output.framing == Framing.fit:
        return (
            f"[0:v]split[bgsrc][fgsrc];"
            f"[bgsrc]scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},boxblur=20:5[bg];"
            f"[fgsrc]scale={ow}:{oh}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}[v]"
        )
    cw, ch = crop_size(edl)
    return f"[0:v]sendcmd=f={cmd_name},crop=w={cw}:h={ch}:x={x0}:y={y0},scale={ow}:{oh}:flags=lanczos,{tail}[v]"


def _audio_filter(length: float, fade_in: bool, fade_out: bool, fade_s: float) -> str:
    parts = [f"aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if fade_in:
        parts.append(f"afade=t=in:st=0:d={fade_s:g}")
    if fade_out:
        parts.append(f"afade=t=out:st={max(0.0, length - fade_s):.4f}:d={fade_s:g}")
    return "[0:a]" + ",".join(parts) + "[a]"


def plan(edl: EDL, source: str, output: str, burn_captions: bool) -> ExportPlan:
    """Commands to run inside one work directory. `source` and `output` are paths in it."""
    boundaries = edl.boundaries()
    is_cut = [b.type == TransitionType.cut for b in boundaries]
    fade_s = edl.audio_fade_ms / 1000
    steps: list[SegmentStep] = []
    for i, seg in enumerate(edl.segments):
        cmd_name = cmd_text = None
        x0 = y0 = 0
        if edl.output.framing == Framing.crop:
            positions = crop_positions(edl, seg)
            _, x0, y0 = positions[0]
            cmd_name, cmd_text = f"crop_{i}.cmd", sendcmd_text(positions)
        # 80 ms audio fades only at cut boundaries (and the two ends of the short).
        fade_in = i == 0 or is_cut[i - 1]
        fade_out = i == len(edl.segments) - 1 or is_cut[i]
        out_name = f"seg_{i}.mp4"
        args = ffmpeg(
            "-ss", f"{seg.src_start:.4f}", "-i", source, "-t", f"{seg.length:.4f}",
            "-filter_complex", _video_filter(edl, cmd_name, x0, y0) + ";"
            + _audio_filter(seg.length, fade_in, fade_out, fade_s),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            "-r", str(OUT_FPS), "-video_track_timescale", str(TIMESCALE),
            "-c:a", "aac", "-b:a", "256k", "-ar", str(AUDIO_RATE), "-ac", "2",
            out_name,
        )  # fmt: skip
        steps.append(SegmentStep(i, seg.length, out_name, cmd_name, cmd_text, args))

    # Segments are already constant 30 fps: pass timestamps through (re-timing added a frame at joins),
    # and cap video by frame count and audio by time at the planned output duration.
    total = output_duration(edl)
    frames = ["-frames:v", str(round(total * OUT_FPS)), "-fps_mode", "passthrough"]
    encode = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    audio = ["-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_RATE), "-ac", "2", "-movflags", "+faststart", output]
    finish = [*frames, *encode, *audio]
    video_tail = "subtitles=filename=captions.ass" if burn_captions else "null"
    audio_tail = f"atrim=end={total:.4f},asetpts=PTS-STARTPTS,{LOUDNORM},aresample={AUDIO_RATE}"

    if all(is_cut):
        concat_list = "".join(f"file '{s.out_name}'\n" for s in steps)
        concat = ["-f", "concat", "-safe", "0", "-i", "segments.txt"]
        if burn_captions:
            final = ffmpeg(
                *concat, "-filter_complex", f"[0:v]{video_tail}[v];[0:a]{audio_tail}[a]",
                "-map", "[v]", "-map", "[a]", *finish,
            )  # fmt: skip
        else:
            # Nothing to draw on the picture: the segments are joined as they are, so the video is encoded once.
            final = ffmpeg(
                *concat, "-filter_complex", f"[0:a]{audio_tail}[a]",
                "-map", "0:v", "-map", "[a]", *frames, "-c:v", "copy", *audio,
            )  # fmt: skip
        return ExportPlan(steps, final, concat_list, total)

    timings = segment_timings(edl)
    graph: list[str] = [f"[{i}:v]settb=AVTB[v{i}]" for i in range(len(steps))]
    chains: list[tuple[str, str]] = []
    cv, ca, chain_start = "v0", "0:a", 0
    for b, transition in enumerate(boundaries):
        nxt = b + 1
        if is_cut[b]:
            chains.append((cv, ca))
            cv, ca, chain_start = f"v{nxt}", f"{nxt}:a", nxt
            continue
        name = CATALOGUE[transition.type].xfade
        offset = timings[nxt].t_out_start - timings[chain_start].t_out_start
        d = transition.effective_duration
        graph.append(f"[{cv}][v{nxt}]xfade=transition={name}:duration={d:g}:offset={offset:.4f}[x{b}]")
        graph.append(f"[{ca}][{nxt}:a]acrossfade=d={d:g}[y{b}]")
        cv, ca = f"x{b}", f"y{b}"
    chains.append((cv, ca))
    if len(chains) > 1:
        joined = "".join(f"[{v}][{a}]" for v, a in chains)
        graph.append(f"{joined}concat=n={len(chains)}:v=1:a=1[jv][ja]")
        jv, ja = "jv", "ja"
    else:
        jv, ja = chains[0]
    graph.append(f"[{jv}]{video_tail}[v]")
    graph.append(f"[{ja}]{audio_tail}[a]")
    inputs = [arg for s in steps for arg in ("-i", s.out_name)]
    final = ffmpeg(*inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", *finish)
    return ExportPlan(steps, final, None, total)
