import math
from collections.abc import Callable

from core.crop import Box, DetectionFrame, Detections, crop_path, focus_spans
from core.edl import Segment
from core.speakers import LipFrame, SpeakerTurn, speaker_at, speaker_turns, speech_runs
from core.transcript import Word

FPS = 12


def _loud(t: float) -> float:
    return 0.5 + 0.5 * math.sin(2 * math.pi * 4.5 * t)  # syllables, about 4.5 a second


def _two_people(until: float, talker: Callable[[float], int]) -> list[LipFrame]:
    """Track 1 and 2 on screen; the talker's mouth follows the sound, the other's moves on its own rhythm."""
    frames = []
    for i in range(int(until * FPS)):
        t = i / FPS
        who = talker(t)
        follow = 0.1 + 0.4 * _loud(t)
        idle = 0.1 + 0.1 * (0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * t + 1.3))
        frames.append(
            LipFrame(t=t, loud=_loud(t), mouth={1: follow if who == 1 else idle, 2: follow if who == 2 else idle})
        )
    return frames


def _speech(until: float) -> list[Word]:
    return [Word(text="w", start=t / 2, end=t / 2 + 0.4) for t in range(int(until * 2))]


def test_speech_runs_merge_close_words() -> None:
    words = [Word(text="a", start=0, end=0.5), Word(text="b", start=0.7, end=1.0), Word(text="c", start=3, end=3.5)]
    assert speech_runs(words) == [(0, 1.0), (3, 3.5)]


def test_turns_follow_whose_mouth_moves_with_the_sound() -> None:
    frames = _two_people(24, lambda t: 1 if t < 12 else 2)
    turns = speaker_turns(frames, _speech(24))
    assert [t.track_id for t in turns] == [1, 2]
    assert turns[0].start < 1 and 10 <= turns[0].end <= 14 and turns[1].end > 22
    assert speaker_at(turns, 5).track_id == 1 and speaker_at(turns, 20).track_id == 2


def test_no_turns_without_speech_or_when_no_mouth_follows() -> None:
    frames = _two_people(12, lambda t: 1)
    assert speaker_turns(frames, []) == []
    silent = [f.model_copy(update={"loud": 0.2}) for f in frames]  # flat sound: nothing to follow
    assert speaker_turns(silent, _speech(12)) == []


def test_crop_cuts_to_the_talker() -> None:
    left = Box(track_id=1, x1=0.05, y1=0.1, x2=0.3, y2=0.9, conf=0.9)
    right = Box(track_id=2, x1=0.7, y1=0.1, x2=0.95, y2=0.9, conf=0.9)
    det = Detections(
        fps=3,
        frames=[DetectionFrame(t=i / 3, boxes=[left, right]) for i in range(90)],
        turns=[
            SpeakerTurn(start=0, end=12, track_id=1, confidence=0.9),
            SpeakerTurn(start=12.5, end=13, track_id=2, confidence=0.9),  # too short to cut to
            SpeakerTurn(start=15, end=30, track_id=2, confidence=0.9),
        ],
    )
    seg = Segment(id="s1", src_start=5, src_end=25)
    assert focus_spans(det, seg, 1) == [(5, 15, 1), (15, 25, 2)]
    kf = crop_path(det, [seg], 1920, 1080, 1080, 1920)[0].crop
    before = [k for k in kf if k.t < 15]
    after = [k for k in kf if k.t >= 15]
    assert all(k.cx < 0.4 for k in before) and all(k.cx > 0.6 for k in after)
    assert kf[0].t == 5 and kf[-1].t == 25


def test_crop_without_turns_is_unchanged() -> None:
    seg = Segment(id="s1", src_start=0, src_end=10)
    assert focus_spans(Detections(fps=3, frames=[]), seg, 4) == [(0, 10, 4)]
    assert focus_spans(None, seg, None) == [(0, 10, None)]
