import os

import pytest

from core.transcript import Cue
from rag import chunk_cues, rrf


def cues(n: int) -> list[Cue]:
    return [Cue(id=f"c{i}", start=float(i), end=i + 0.9, text=f"t{i}") for i in range(1, n + 1)]


def test_chunks_three_with_one_overlap() -> None:
    chunks = chunk_cues(cues(7))
    assert [(c.cue_from, c.cue_to) for c in chunks] == [("c1", "c3"), ("c3", "c5"), ("c5", "c7")]
    assert chunks[0].text == "t1 t2 t3" and chunks[0].start_s == 1.0 and chunks[0].end_s == 3.9
    assert [(c.cue_from, c.cue_to) for c in chunk_cues(cues(4))] == [("c1", "c3"), ("c3", "c4")]
    assert chunk_cues([]) == []
    assert len(chunk_cues(cues(1))) == 1


def test_rrf() -> None:
    fused = rrf([["a", "b", "c"], ["b", "c", "d"]], k=60)
    assert [k for k, _ in fused] == ["b", "c", "a", "d"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="downloads multilingual-e5-small; set RUN_SLOW=1")
def test_embeddings_are_384_and_normalised() -> None:
    from rag import embed_passages, embed_query

    v = embed_query("where do I talk about hiring?")
    p = embed_passages(["We are hiring two engineers next quarter.", "Il pleut à Paris."])
    assert len(v) == 384 and all(len(x) == 384 for x in p)
    assert abs(sum(x * x for x in v) - 1) < 1e-3
    sim = [sum(a * b for a, b in zip(v, x, strict=True)) for x in p]
    assert sim[0] > sim[1]
