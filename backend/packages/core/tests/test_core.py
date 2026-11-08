import json
from pathlib import Path

import pytest

from core.aspect import crop_window, output_size
from core.crop import Box, DetectionFrame, Detections, crop_path
from core.edl import EDL
from core.enums import Author
from core.errors import AppError
from core.settings import LimitKey, check_value, effective_limits
from core.timemap import out_to_src, output_duration, segment_timings, src_to_out, xfade_offsets
from core.transcript import Cue, Transcript, Word, snap, to_srt
from core.validation import find_issues, validate

VECTORS = Path(__file__).parents[1] / "src" / "core" / "vectors" / "timemap.json"


def make_edl(**over: object) -> EDL:
    data: dict[str, object] = {
        "version": 1,
        "source": {"key": "k", "duration_s": 600.0, "width": 1920, "height": 1080, "fps": 30},
        "output": {"aspect": "9:16", "width": 1080, "height": 1920, "target_duration_s": 20},
        "segments": [
            {"id": "s1", "src_start": 10.0, "src_end": 20.0},
            {"id": "s2", "src_start": 30.0, "src_end": 40.0},
            {"id": "s3", "src_start": 50.0, "src_end": 51.5},
        ],
        "transitions": {
            "default": {"type": "fade", "duration_s": 0.5},
            "overrides": [{"from": "s2", "to": "s3", "type": "cut", "duration_s": 0.4}],
        },
    }
    data.update(over)
    edl = EDL.model_validate(data)
    return edl.model_copy(update={"segments": crop_path(None, edl.segments, 1920, 1080, 1080, 1920)})


def test_timings_and_offsets() -> None:
    edl = make_edl()
    t = segment_timings(edl)
    assert [round(x.t_out_start, 3) for x in t] == [0.0, 9.5, 19.5]
    assert xfade_offsets(edl) == pytest.approx([9.5, 19.5])
    assert output_duration(edl) == pytest.approx(21.0)


def test_time_mapping_overlap() -> None:
    edl = make_edl()
    assert src_to_out(edl, 15.0) == pytest.approx(5.0)
    assert src_to_out(edl, 100.0) is None
    both = out_to_src(edl, 9.7)
    assert [i for i, _ in both] == [0, 1]
    assert both[0][1] == pytest.approx(19.7)
    assert both[1][1] == pytest.approx(30.2)


def test_shared_vectors() -> None:
    for case in json.loads(VECTORS.read_text()):
        edl = EDL.model_validate(case["edl"])
        assert output_duration(edl) == pytest.approx(case["output_duration"])
        assert [s.t_out_start for s in segment_timings(edl)] == pytest.approx(case["t_out_starts"])
        for q in case["src_to_out"]:
            got = src_to_out(edl, q["src"])
            assert (got is None and q["t_out"] is None) or got == pytest.approx(q["t_out"])
        for q in case["out_to_src"]:
            assert [[i, round(s, 6)] for i, s in out_to_src(edl, q["t_out"])] == q["hits"]


def test_crop_window() -> None:
    w, h = crop_window(1920, 1080, 1080, 1920)
    assert round(w, 4) == 0.3164 and h == 1.0
    assert crop_window(1080, 1920, 1920, 1080)[1] == pytest.approx(0.3164, abs=1e-4)
    assert output_size("9:16") == (1080, 1920)
    assert output_size("21:9") == (2520, 1080)


def test_validation_accepts_user_plan() -> None:
    assert find_issues(make_edl(), Author.user) == []


def test_validation_agent_target_and_errors() -> None:
    edl = make_edl()
    assert find_issues(edl, Author.agent) == []
    far = edl.model_copy(update={"output": edl.output.model_copy(update={"target_duration_s": 60})})
    assert any("10%" in i.message for i in find_issues(far, Author.agent))
    with pytest.raises(AppError) as e:
        validate(make_edl(segments=[{"id": "s1", "src_start": 5, "src_end": 5.5}]), Author.user)
    assert e.value.code == "edl_invalid"


def test_validation_duplicate_and_transition_margin() -> None:
    edl = make_edl(
        segments=[
            {"id": "s1", "src_start": 10, "src_end": 20},
            {"id": "s2", "src_start": 19, "src_end": 25},
        ]
    )
    assert any("repeats" in i.message for i in find_issues(edl, Author.user))


def test_crop_path_follows_speaker() -> None:
    frames = [
        DetectionFrame(t=t / 3, boxes=[Box(track_id=1, x1=0.6, y1=0.2, x2=0.8, y2=0.9, conf=0.9)]) for t in range(0, 90)
    ]
    edl = make_edl()
    segs = crop_path(Detections(fps=3, frames=frames), edl.segments[:1], 1920, 1080, 1080, 1920)
    kf = segs[0].crop
    assert kf[0].t == 10.0 and kf[-1].t == 20.0
    assert all(0.158 <= k.cx <= 0.842 for k in kf)
    assert kf[-1].cx > 0.6


def test_square_crop_at_the_frame_edge_stays_inside() -> None:
    # 1:1 from 16:9: the window is 0.5625 wide, so its centre stops at 0.28125 / 0.71875. Plain 4-decimal
    # rounding gave 0.2812, 0.00005 outside the frame, and the plan was rejected.
    for x1, x2 in ((0.0, 0.1), (0.9, 1.0)):
        box = Box(track_id=1, x1=x1, y1=0.1, x2=x2, y2=0.9, conf=0.9)
        frames = [DetectionFrame(t=t / 3, boxes=[box]) for t in range(90)]
        edl = make_edl(output={"aspect": "1:1", "width": 1080, "height": 1080, "framing": "crop"})
        edl = edl.model_copy(update={"source": edl.source.model_copy(update={"width": 1280, "height": 720})})
        segs = crop_path(Detections(fps=3, frames=frames), edl.segments, 1280, 720, 1080, 1080)
        issues = find_issues(edl.model_copy(update={"segments": segs}), Author.agent)
        assert not [i for i in issues if ".crop" in i.path]


def test_settings() -> None:
    eff = effective_limits({"max_active_videos": 8}, {"max_storage_mb": 100})
    assert eff[LimitKey.max_active_videos] == 8
    assert eff[LimitKey.max_storage_mb] == 100
    assert eff[LimitKey.max_file_size_mb] == 1024
    with pytest.raises(AppError):
        check_value("max_active_videos", 0)
    with pytest.raises(AppError):
        check_value("nope", 1)


def test_snap_and_srt() -> None:
    tr = Transcript(
        cues=[
            Cue(
                id="c1",
                start=0.0,
                end=1.2,
                text="Hello there",
                words=[Word(text="Hello", start=0.0, end=0.5)],
            )
        ]
    )
    assert snap(0.4, tr.word_boundaries(), 0.5) == 0.5
    assert snap(3.0, tr.word_boundaries(), 0.5) is None
    assert to_srt(tr.cues, {"c1": "Hi"}) == "1\n00:00:00,000 --> 00:00:01,200\nHi\n"


def test_whole_video_reframe_is_exempt_from_the_user_length_limit() -> None:
    # A 10-minute video reframed as one clip is allowed; a 10-minute cut that skips a bit is still too long.
    cuts = {"default": {"type": "cut", "duration_s": 0.4}, "overrides": []}
    whole = make_edl(segments=[{"id": "s1", "src_start": 0.0, "src_end": 600.0}], transitions=cuts)
    assert find_issues(whole, Author.user) == []
    partial = make_edl(segments=[{"id": "s1", "src_start": 5.0, "src_end": 600.0}], transitions=cuts)
    assert [i.path for i in find_issues(partial, Author.user)] == ["output"]
