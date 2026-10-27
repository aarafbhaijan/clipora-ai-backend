"""Transcript shapes: `transcript.json` written by ingest, read by the API, agent, export and the editor."""

from pydantic import BaseModel, Field


class Word(BaseModel):
    text: str
    start: float
    end: float


class Cue(BaseModel):
    """One subtitle entry. Ids `c1`, `c2`... never change after ingest."""

    id: str = Field(pattern=r"^c[0-9]+$")
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str | None = None
    cues: list[Cue]

    def word_boundaries(self) -> list[float]:
        """Every word start and end, sorted. Used to snap LLM and user timestamps."""
        points = {w.start for c in self.cues for w in c.words} | {w.end for c in self.cues for w in c.words}
        if not points:
            points = {c.start for c in self.cues} | {c.end for c in self.cues}
        return sorted(points)


def snap(t: float, boundaries: list[float], within: float) -> float | None:
    """Nearest boundary within `within` seconds, else None (the caller rejects the range)."""
    best = min(boundaries, key=lambda b: abs(b - t), default=None)
    if best is None or abs(best - t) > within:
        return None
    return best


def format_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(cues: list[Cue], edits: dict[str, str] | None = None) -> str:
    edits = edits or {}
    blocks = [
        f"{i}\n{format_srt_time(c.start)} --> {format_srt_time(c.end)}\n{edits.get(c.id, c.text)}\n"
        for i, c in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)
