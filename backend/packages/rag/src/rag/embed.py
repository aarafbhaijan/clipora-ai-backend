"""Embeddings with an open multilingual model, 384-d, normalised (architecture.md section 10).

multilingual-e5-small expects "passage: " on indexed text and "query: " on searches.
Loaded lazily and cached per process; only workers import this.
"""

import os
from functools import lru_cache
from typing import Any

MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
DIM = 384
BATCH = 32


@lru_cache(maxsize=1)
def _model() -> Any:
    from sentence_transformers import SentenceTransformer  # heavy import

    return SentenceTransformer(MODEL, device="cpu")


def _encode(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = _model().encode(texts, batch_size=BATCH, normalize_embeddings=True, show_progress_bar=False)
    out = [[float(x) for x in v] for v in vectors]
    if out and len(out[0]) != DIM:
        raise RuntimeError(f"embedding model returned {len(out[0])} dimensions, expected {DIM}")
    return out


def embed_passages(texts: list[str]) -> list[list[float]]:
    return _encode([f"passage: {t}" for t in texts])


def embed_query(text: str) -> list[float]:
    return _encode([f"query: {text}"])[0]


def embed_queries(texts: list[str]) -> list[list[float]]:
    return _encode([f"query: {t}" for t in texts])
