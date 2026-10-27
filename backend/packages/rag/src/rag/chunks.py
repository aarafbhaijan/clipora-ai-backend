"""Transcript chunks for retrieval: 3 cues per chunk with 1 cue of overlap (ingest stage 7)."""

from dataclasses import dataclass

from core.transcript import Cue


@dataclass(frozen=True)
class Chunk:
    cue_from: str
    cue_to: str
    start_s: float
    end_s: float
    text: str


def chunk_cues(cues: list[Cue], size: int = 3, overlap: int = 1) -> list[Chunk]:
    if size <= overlap:
        raise ValueError("size must be larger than overlap")
    step = size - overlap
    chunks: list[Chunk] = []
    for start in range(0, len(cues), step):
        group = cues[start : start + size]
        if not group:
            break
        chunks.append(
            Chunk(
                cue_from=group[0].id,
                cue_to=group[-1].id,
                start_s=group[0].start,
                end_s=group[-1].end,
                text=" ".join(c.text for c in group),
            )
        )
        if start + size >= len(cues):
            break
    return chunks
