"""Captions for an export (architecture.md section 13).

Cues inside each segment, `captions.edits` applied, shifted to output time and split at segment edges.
Each segment's captions stop where the next segment starts, so a transition never shows two captions.
"""

from dataclasses import dataclass

from core.edl import EDL
from core.timemap import segment_timings
from core.transcript import Cue, Transcript, to_srt
from media.cues import wrap

MIN_PIECE_S = 0.1


@dataclass(frozen=True)
class OutCue:
    start: float
    end: float
    text: str


def output_cues(edl: EDL, transcript: Transcript) -> list[OutCue]:
    timings = segment_timings(edl)
    out: list[OutCue] = []
    for i, st in enumerate(timings):
        window_end = timings[i + 1].t_out_start if i + 1 < len(timings) else st.t_out_end
        for c in transcript.cues:
            s, e = max(c.start, st.src_start), min(c.end, st.src_end)
            if e <= s:
                continue
            t0 = st.t_out_start + (s - st.src_start)
            t1 = min(st.t_out_start + (e - st.src_start), window_end)
            if t1 - t0 >= MIN_PIECE_S:
                out.append(OutCue(round(t0, 3), round(t1, 3), edl.captions.edits.get(c.id, c.text)))
    return out


def srt(cues: list[OutCue]) -> str:
    return to_srt([Cue(id=f"c{i}", start=c.start, end=c.end, text=c.text) for i, c in enumerate(cues, start=1)])


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass_text(text: str) -> str:
    lines = wrap(text) or [text]
    escaped = [ln.replace("\\", "\\⁠").replace("{", "\\{").replace("}", "\\}") for ln in lines]
    return "\\N".join(escaped)


# Style "bold-center": bold white text, black outline, bottom centre above platform controls.
FONT = "DejaVu Sans"


def ass(cues: list[OutCue], width: int, height: int) -> str:
    size = min(round(width / 24), round(height * 0.06))
    outline = max(1, round(size * 0.08))
    margin_h = round(width * 0.06)
    margin_v = round(height * 0.18)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,"
        f"{outline},1,2,{margin_h},{margin_h},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = "".join(
        f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Default,,0,0,0,,{_ass_text(c.text)}\n" for c in cues
    )
    return header + events
