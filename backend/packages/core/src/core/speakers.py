"""Who is talking when: each tracked person's mouth, matched against the sound (architecture.md section 12.1).

Ingest samples the video 12 times a second (`LipFrame`): how open each visible person's mouth is (MediaPipe's
jawOpen, 0..1) and how loud the audio is at that moment. A talking mouth opens and closes with the syllables,
so while there is speech (transcript words), the person whose mouth follows the loudness is the speaker; a
listener's mouth doesn't. Pure functions on plain data, so the API and the agent can use the turns without
importing vision. Thresholds are starting points: tune them on real videos.
"""

from bisect import bisect_right
from math import sqrt
from statistics import mean

from pydantic import BaseModel

from core.transcript import Word

WINDOW_S = 3.0  # speech is judged in windows of about three seconds
MIN_SAMPLES = 12  # mouth samples a person needs inside a window to be judged (a third of it)
SYNC_MIN = 0.3  # mouth-to-loudness correlation that counts as talking (a listener is about 0)
MARGIN = 0.1  # how much better the speaker must follow the sound than the next person
WORD_GAP_S = 0.35  # words closer than this are one stretch of speech
JOIN_GAP_S = 1.5  # turns of the same person closer than this become one (a pause, a breath)


class LipFrame(BaseModel):
    """One sample: audio loudness (RMS, 0..1) and, per visible track id, how open the mouth is (0..1)."""

    t: float
    loud: float
    mouth: dict[int, float]


class SpeakerTurn(BaseModel):
    """A stretch of source time in which one tracked person is talking."""

    start: float
    end: float
    track_id: int
    confidence: float  # 0..1, how clearly this person's lips moved more than anyone else's


def speech_runs(words: list[Word], gap: float = WORD_GAP_S) -> list[tuple[float, float]]:
    """Word timings merged into stretches of continuous speech."""
    runs: list[tuple[float, float]] = []
    for w in sorted(words, key=lambda w: w.start):
        if runs and w.start - runs[-1][1] <= gap:
            runs[-1] = (runs[-1][0], max(runs[-1][1], w.end))
        else:
            runs.append((w.start, w.end))
    return runs


def _windows(runs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in runs:
        n = max(1, round((end - start) / WINDOW_S))
        step = (end - start) / n
        out.extend((start + i * step, start + (i + 1) * step) for i in range(n))
    return out


def _correlation(xs: list[float], ys: list[float]) -> float:
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sx = sqrt(sum((x - mx) ** 2 for x in xs))
    sy = sqrt(sum((y - my) ** 2 for y in ys))
    return sxy / (sx * sy) if sx > 1e-9 and sy > 1e-9 else 0.0


def _judge(
    frames: list[LipFrame], times: list[float], start: float, end: float, sizes: dict[int, float]
) -> tuple[int, float] | None:
    """(track, confidence) of who talks in [start, end], or None when no mouth clearly follows the sound.

    A listener's mouth almost never follows the voice, so when several do, they are the same person shown twice
    (a monitor, a mirror, a picture-in-picture): the biggest on screen is the real one."""
    lo, hi = bisect_right(times, start - 1e-6), bisect_right(times, end)
    series: dict[int, tuple[list[float], list[float]]] = {}
    for f in frames[lo:hi]:
        for track, m in f.mouth.items():
            mouth, loud = series.setdefault(track, ([], []))
            mouth.append(m)
            loud.append(f.loud)
    scores = sorted(
        ((_correlation(m, s), track) for track, (m, s) in series.items() if len(m) >= MIN_SAMPLES),
        reverse=True,
    )
    if not scores or scores[0][0] < SYNC_MIN:
        return None
    talking = [(score, track) for score, track in scores if score >= SYNC_MIN]
    if len(talking) > 1:
        score, track = max(talking, key=lambda st: sizes.get(st[1], 0.0))
        return track, min(1.0, score / 0.5) * 0.5  # sure someone talks, less sure it's this copy
    best, track = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    if len(scores) > 1 and best - second < MARGIN:
        return None
    return track, min(1.0, (best - max(0.0, second)) / 0.5)


def speaker_turns(
    frames: list[LipFrame], words: list[Word], sizes: dict[int, float] | None = None
) -> list[SpeakerTurn]:
    """Who talks when, in source time, ordered. Speech nobody on screen is clearly saying (a voice-over, a
    person off camera, or faces too small to read) has no turn. `sizes`: each track's usual box area, to choose
    between copies of the same person."""
    runs = speech_runs(words)
    if not frames or not runs:
        return []
    frames = sorted(frames, key=lambda f: f.t)
    times = [f.t for f in frames]
    windows = _windows(runs)
    labels = [_judge(frames, times, s, e, sizes or {}) for s, e in windows]

    # One unclear window between two of the same person (a look away, a hand over the mouth) is still them.
    for i in range(1, len(labels) - 1):
        prev, nxt = labels[i - 1], labels[i + 1]
        if prev and nxt and prev[0] == nxt[0] and labels[i] is None:
            labels[i] = (prev[0], min(prev[1], nxt[1]))

    turns: list[SpeakerTurn] = []
    scores: list[list[float]] = []
    for (start, end), label in zip(windows, labels, strict=True):
        if label is None:
            continue
        track, conf = label
        if turns and turns[-1].track_id == track and start - turns[-1].end <= JOIN_GAP_S:
            turns[-1].end = end
            scores[-1].append(conf)
        else:
            turns.append(SpeakerTurn(start=start, end=end, track_id=track, confidence=0))
            scores.append([conf])
    for turn, s in zip(turns, scores, strict=True):
        turn.start, turn.end, turn.confidence = round(turn.start, 3), round(turn.end, 3), round(mean(s), 2)
    return turns


def speaker_at(turns: list[SpeakerTurn], t: float) -> SpeakerTurn | None:
    """The turn covering source time t, if any. `turns` are ordered and don't overlap."""
    i = bisect_right([x.start for x in turns], t) - 1
    return turns[i] if i >= 0 and t <= turns[i].end else None
