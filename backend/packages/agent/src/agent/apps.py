"""Compiled graphs (one per process) and the adapters that plug rag into them."""

import threading
from typing import Any

from agent.checkpoint import checkpointer

_lock = threading.Lock()
_apps: dict[str, Any] = {}


def edit_app() -> Any:
    with _lock:
        if "edit" not in _apps:
            from agent.edit_graph import build_graph

            _apps["edit"] = build_graph().compile(checkpointer=checkpointer())
        return _apps["edit"]


def metadata_app() -> Any:
    with _lock:
        if "metadata" not in _apps:
            from agent.metadata_graph.graph import build_graph

            _apps["metadata"] = build_graph().compile(checkpointer=checkpointer())
        return _apps["metadata"]


def embed_one(text: str) -> list[float]:
    import rag

    vectors: list[list[float]] = rag.embed_passages([text])
    return vectors[0]
