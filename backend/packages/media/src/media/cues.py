"""Words to subtitle cues, and SRT parsing (architecture.md section 10).

A cue holds at most 2 lines of 42 characters and 7 s. It breaks at sentence ends or pauses over 0.6 s.
Cue text is stored on one line; `wrap` splits it into at most two lines when rendering.
"""

import re

from core.transcript import Cue, Word

MAX_LINE = 42
MAX_LINES = 2
MAX_CUE_S = 7.0
PAUSE_S = 0.6
_SENTENCE_END = re.compile(r"[.!?…。！？]$")


def _join(words: list[Word]) -> str:
    return " ".join(w.text.strip() for w in words if w.text.strip())


def group_words(words: list[Word]) -> list[Cue]:
    cues: list[Cue] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            text = _join(current)
            if text:
                cues.append(
                    Cue(
                        id=f"c{len(cues) + 1}",
                        start=current[0].start,
                        end=current[-1].end,
                        text=text,
                        words=list(current),
                    )
                )
            current.clear()

    for w in words:
        if not w.text.strip():
            continue
        if current:
            candidate = _join([*current, w])
            too_long = len(candidate) > MAX_LINE * MAX_LINES or not wrap(candidate)
            too_slow = w.end - current[0].start > MAX_CUE_S
            pause = w.start - current[-1].end > PAUSE_S
            if too_long or too_slow or pause:
                flush()
        current.append(w)
        if _SENTENCE_END.search(w.text.strip()):
            flush()
    flush()
    return cues


def wrap(text: str, width: int = MAX_LINE) -> list[str]:
    """Split into at most 2 balanced lines of `width` characters. Empty list if it cannot fit."""
    if len(text) <= width:
        return [text]
    words = text.split(" ")
    best: list[str] = []
    best_score = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) <= width and len(b) <= width:
            score = abs(len(a) - len(b))
            if best_score is None or score < best_score:
                best, best_score = [a, b], score
    return best


_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _seconds(m: re.Match[str]) -> float:
    h, mnt, s, ms = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    """Parse a user-supplied SRT. Ids are renumbered c1, c2... in time order. No word timings."""
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("﻿", "").strip())
    raw: list[tuple[float, float, str]] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            times = _TIME.findall(line)
            if "-->" in line and len(times) == 2:
                start, end = (_seconds(m) for m in _TIME.finditer(line))
                body = " ".join(re.sub(r"<[^>]+>", "", ln) for ln in lines[i + 1 :]).strip()
                if body and end > start:
                    raw.append((start, end, body))
                break
    raw.sort(key=lambda r: r[0])
    return [Cue(id=f"c{i}", start=s, end=e, text=t) for i, (s, e, t) in enumerate(raw, start=1)]
