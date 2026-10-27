"""Transcript chunking, embeddings, hybrid retrieval and memory helpers (architecture.md 10 and 11.4).

Public interface (implemented in the modules below, used by worker-ingest and the agent):

    chunk_cues(cues) -> list[Chunk]                        3 cues per chunk, 1 overlap
    embed_passages(texts) -> list[list[float]]             384-d, normalised
    embed_query(text) -> list[float]
    write_chunks(tx, project_id, chunks, embeddings)       idempotent: replaces the project's rows
    retrieve(tx, project_id, queries, top_k=8) -> list[Hit] vector top 20 + full text top 20, RRF k=60
    match_memories(tx, user_id, text, top_k=5) -> list[MemoryHit]   active memories only

The API never imports this package (no embeddings in the API image).
"""

from rag.chunks import Chunk, chunk_cues
from rag.embed import embed_passages, embed_query
from rag.retrieve import Hit, MemoryHit, match_memories, retrieve, rrf, write_chunks

__all__ = [
    "Chunk",
    "Hit",
    "MemoryHit",
    "chunk_cues",
    "embed_passages",
    "embed_query",
    "match_memories",
    "retrieve",
    "rrf",
    "write_chunks",
]
