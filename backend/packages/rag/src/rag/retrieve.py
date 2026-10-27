"""Hybrid retrieval over one project's transcript, and memory matching (architecture.md 11.4).

Per query: top 20 by vector similarity (exact scan, cosine) plus top 20 by full text, merged with
reciprocal rank fusion (k = 60). The top 8 come back with cue ids and times.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from db.ids import uuid7
from db.models import AgentMemory, TranscriptChunk
from rag.chunks import Chunk
from rag.embed import embed_queries, embed_query

PER_LIST = 20
RRF_K = 60
_SIMPLE = text("'simple'::regconfig")


@dataclass(frozen=True)
class Hit:
    cue_from: str
    cue_to: str
    start_s: float
    end_s: float
    text: str
    score: float


@dataclass(frozen=True)
class MemoryHit:
    id: uuid.UUID
    kind: str
    text: str
    score: float


def rrf(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: score = sum of 1 / (k + rank), rank starting at 1. Best first."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def write_chunks(tx: Session, project_id: uuid.UUID, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
    """Replace the project's chunks. Safe to run twice."""
    if len(chunks) != len(embeddings):
        raise ValueError("one embedding per chunk")
    tx.execute(delete(TranscriptChunk).where(TranscriptChunk.project_id == project_id))
    if not chunks:
        return
    tx.execute(
        insert(TranscriptChunk).values(
            [
                {
                    "id": uuid7(),
                    "project_id": project_id,
                    "cue_from": c.cue_from,
                    "cue_to": c.cue_to,
                    "start_s": c.start_s,
                    "end_s": c.end_s,
                    "text": c.text,
                    "embedding": e,
                    "tsv": func.to_tsvector(_SIMPLE, c.text),
                }
                for c, e in zip(chunks, embeddings, strict=True)
            ]
        )
    )


def _vector_ids(tx: Session, project_id: uuid.UUID, vector: list[float]) -> list[str]:
    rows = tx.execute(
        select(TranscriptChunk.id)
        .where(TranscriptChunk.project_id == project_id)
        .order_by(TranscriptChunk.embedding.cosine_distance(vector))
        .limit(PER_LIST)
    ).scalars()
    return [str(r) for r in rows]


def _text_ids(tx: Session, project_id: uuid.UUID, query: str) -> list[str]:
    tsquery = func.websearch_to_tsquery(_SIMPLE, query)
    rows = tx.execute(
        select(TranscriptChunk.id)
        .where(TranscriptChunk.project_id == project_id, TranscriptChunk.tsv.op("@@")(tsquery))
        .order_by(func.ts_rank(TranscriptChunk.tsv, tsquery).desc())
        .limit(PER_LIST)
    ).scalars()
    return [str(r) for r in rows]


def retrieve(tx: Session, project_id: uuid.UUID, queries: list[str], top_k: int = 8) -> list[Hit]:
    queries = [q.strip() for q in queries if q.strip()]
    if not queries:
        return []
    rankings: list[list[str]] = []
    for query, vector in zip(queries, embed_queries(queries), strict=True):
        rankings.append(_vector_ids(tx, project_id, vector))
        rankings.append(_text_ids(tx, project_id, query))
    fused = rrf(rankings)[:top_k]
    if not fused:
        return []
    ids = [uuid.UUID(k) for k, _ in fused]
    rows = {
        str(r.id): r
        for r in tx.execute(
            select(TranscriptChunk).where(TranscriptChunk.project_id == project_id, TranscriptChunk.id.in_(ids))
        ).scalars()
    }
    return [
        Hit(cue_from=r.cue_from, cue_to=r.cue_to, start_s=r.start_s, end_s=r.end_s, text=r.text, score=score)
        for key, score in fused
        if (r := rows.get(key)) is not None
    ]


def match_memories(tx: Session, user_id: uuid.UUID, text: str, top_k: int = 5) -> list[MemoryHit]:
    """Active memories closest to `text`. Memories without an embedding (new or edited) are embedded now."""
    missing = list(
        tx.execute(
            select(AgentMemory).where(
                AgentMemory.user_id == user_id, AgentMemory.status == "active", AgentMemory.embedding.is_(None)
            )
        ).scalars()
    )
    for m in missing:
        m.embedding = embed_query(m.text)
    if missing:
        tx.flush()
    vector = embed_query(text)
    distance = AgentMemory.embedding.cosine_distance(vector)
    rows = tx.execute(
        select(AgentMemory, distance)
        .where(AgentMemory.user_id == user_id, AgentMemory.status == "active")
        .order_by(distance)
        .limit(top_k)
    ).all()
    return [MemoryHit(id=m.id, kind=m.kind, text=m.text, score=1.0 - float(d)) for m, d in rows]
