import json
from typing import Any

import pytest

from core.crop import crop_path
from core.edl import EDL, CropKeyframe
from core.timemap import output_duration
from core.transcript import Cue, Transcript
from media.captions import ass, output_cues, srt
from media.export import crop_size, plan


def make_edl(transitions: dict[str, Any], framing: str = "crop") -> EDL:
    edl = EDL.model_validate(
        {
            "version": 1,
            "source": {"key": "k", "duration_s": 60, "width": 1920, "height": 1080, "fps": 30},
            "output": {"aspect": "9:16", "width": 1080, "height": 1920, "framing": framing},
            "segments": [
                {"id": "s1", "src_start": 2, "src_end": 9},
                {"id": "s2", "src_start": 20, "src_end": 27},
                {"id": "s3", "src_start": 40, "src_end": 47},
            ],
            "transitions": transitions,
            "captions": {"edits": {"c3": "Edited {line}"}},
        }
    )
    segs = crop_path(None, edl.segments, 1920, 1080, 1080, 1920)
    segs[0] = segs[0].model_copy(
        update={"crop": [CropKeyframe(t=2, cx=0.16, cy=0.5), CropKeyframe(t=9, cx=0.84, cy=0.5)]}
    )
    return edl.model_copy(update={"segments": segs})


CASES = {
    "mixed": {
        "default": {"type": "fade", "duration_s": 0.5},
        "overrides": [{"from": "s2", "to": "s3", "type": "slide_left", "duration_s": 0.5}],
    },
    "cutchain": {
        "default": {"type": "fade", "duration_s": 0.5},
        "overrides": [{"from": "s2", "to": "s3", "type": "cut", "duration_s": 0.4}],
    },
    "allcut": {"default": {"type": "cut", "duration_s": 0.4}, "overrides": []},
}


def _dump(edl: EDL) -> str:
    p = plan(edl, "source.mp4", "out.mp4", burn_captions=True)
    data = {
        "segments": [
            {"args": s.args[1:], "cmd_name": s.cmd_name, "cmd_head": (s.cmd_text or "").splitlines()[:5]}
            for s in p.segments
        ],
        "final": p.final_args[1:],
        "concat_list": p.concat_list,
        "output_duration": p.output_duration,
    }
    return json.dumps(data, indent=1) + "\n"


@pytest.mark.parametrize("case", sorted(CASES))
def test_plan_golden(case: str, golden: Any) -> None:
    framing = "fit" if case == "allcut" else "crop"
    golden(f"plan_{case}.json", _dump(make_edl(CASES[case], framing)))


def test_xfade_offsets_and_cut_split() -> None:
    p = plan(make_edl(CASES["cutchain"]), "source.mp4", "out.mp4", True)
    graph = p.final_args[p.final_args.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=0.5:offset=6.5000" in graph
    assert "concat=n=2:v=1:a=1" in graph
    assert p.concat_list is None
    assert p.final_args[p.final_args.index("-frames:v") + 1] == str(round(20.5 * 30))


def test_all_cuts_use_concat_demuxer() -> None:
    p = plan(make_edl(CASES["allcut"], "fit"), "source.mp4", "out.mp4", False)
    assert p.concat_list == "file 'seg_0.mp4'\nfile 'seg_1.mp4'\nfile 'seg_2.mp4'\n"
    assert "segments.txt" in p.final_args
    assert all(s.cmd_name is None for s in p.segments)  # fit ignores crop
    assert "boxblur=20:5" in " ".join(p.segments[0].args)


def test_sendcmd_pans_and_stays_inside() -> None:
    edl = make_edl(CASES["mixed"])
    cw, ch = crop_size(edl)
    assert (cw, ch) == (608, 1080)
    text = plan(edl, "s.mp4", "o.mp4", True).segments[0].cmd_text or ""
    xs = [int(line.split("crop x ")[1].split(",")[0]) for line in text.splitlines()]
    assert xs[0] == 3 and 1290 < xs[-1] <= 1920 - 608
    assert xs == sorted(xs) and min(xs) >= 0 and max(xs) <= 1920 - 608


def test_args_are_lists_without_shell() -> None:
    p = plan(make_edl(CASES["mixed"]), "my file; rm -rf.mp4", "out.mp4", True)
    assert "my file; rm -rf.mp4" in p.segments[0].args  # one argument, never parsed by a shell


def transcript() -> Transcript:
    return Transcript(
        cues=[Cue(id=f"c{i}", start=i * 1.5, end=i * 1.5 + 1.4, text=f"Line number {i}") for i in range(40)]
    )


def test_captions_shift_split_and_edits(golden: Any) -> None:
    edl = make_edl(CASES["mixed"])
    cues = output_cues(edl, transcript())
    assert cues[0].start == 0.0  # c1 (1.5-2.9) clipped at s1 start 2.0
    assert any(c.text == "Edited {line}" for c in cues)
    starts = [c.start for c in cues]
    assert starts == sorted(starts)
    # nothing overlaps: each segment's captions stop where the next starts
    assert all(a.end <= b.start + 1e-6 for a, b in zip(cues, cues[1:], strict=False))
    assert cues[-1].end <= output_duration(edl) + 1e-6
    golden("captions_mixed.ass", ass(cues, 1080, 1920))
    golden("captions_mixed.srt", srt(cues))


def test_all_cuts_without_captions_copy_the_video() -> None:
    """Nothing is drawn on the joined picture, so the segments' video is copied: encoded once, not twice."""
    edl = make_edl(CASES["allcut"])
    copied = plan(edl, "source.mp4", "out.mp4", burn_captions=False).final_args
    assert copied[copied.index("-c:v") + 1] == "copy" and "0:v" in copied
    assert copied[copied.index("-frames:v") + 1] == str(round(output_duration(edl) * 30))
    burned = plan(edl, "source.mp4", "out.mp4", burn_captions=True).final_args
    assert burned[burned.index("-c:v") + 1] == "libx264" and "subtitles=filename=captions.ass" in " ".join(burned)
