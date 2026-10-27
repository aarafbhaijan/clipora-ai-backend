"""Fakes for agent tests: a scripted chat model (no network, no Bedrock) and a small transcript."""

import json
from collections import defaultdict
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from agent.llm import LLMClient, UsageRecord
from core.transcript import Cue, Transcript, Word


class FakeRunnable:
    def __init__(self, model: "FakeModel", schema: Any) -> None:
        self.model, self.schema = model, schema

    def invoke(self, messages: list[BaseMessage]) -> dict[str, Any]:
        name = self.schema["title"] if isinstance(self.schema, dict) else self.schema.__name__
        self.model.calls.append((name, messages))
        script = self.model.script[name]
        if not script:
            raise AssertionError(f"no scripted response left for {name}")
        item = script.pop(0)
        raw = AIMessage(
            content=json.dumps(item) if not isinstance(item, Exception) else "not json",
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "input_token_details": {"cache_read": 20, "cache_creation": 0},
            },
        )
        if isinstance(item, Exception):
            return {"raw": raw, "parsed": None, "parsing_error": item}
        # A dict schema (what LLMClient sends) comes back as a plain dict, as with the real model.
        parsed = item if isinstance(self.schema, dict) else self.schema.model_validate(item)
        return {"raw": raw, "parsed": parsed, "parsing_error": None}


class FakeModel:
    def __init__(self) -> None:
        self.script: dict[str, list[Any]] = defaultdict(list)
        self.calls: list[tuple[str, list[BaseMessage]]] = []
        self.temperatures: list[float | None] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FakeRunnable:
        assert kwargs.get("method") in ("json_schema", "function_calling") and kwargs.get("include_raw") is True
        return FakeRunnable(self, schema)


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def usage_rows() -> list[UsageRecord]:
    return []


@pytest.fixture
def archived() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def llm(fake_model: FakeModel, usage_rows: list[UsageRecord], archived: list[dict[str, Any]]) -> LLMClient:
    def factory(model_id: str, temperature: float | None) -> FakeModel:
        fake_model.temperatures.append(temperature)
        return fake_model

    return LLMClient(
        model_ids={"main": "test-main", "small": "test-small"},
        factory=factory,  # type: ignore[arg-type]
        write_usage=usage_rows.append,
        archive=lambda ctx, node, seq, body: archived.append({"node": node, "seq": seq, **body}),
    )


def _cue(i: int, start: float, words: list[str]) -> Cue:
    step = 0.5
    ws = [Word(text=w, start=start + k * step, end=start + k * step + 0.4) for k, w in enumerate(words)]
    return Cue(id=f"c{i}", start=ws[0].start, end=ws[-1].end, text=" ".join(words), words=ws)


@pytest.fixture
def transcript() -> Transcript:
    """40 cues, 10 words each, one every 6 s: c1 at 0 s ... c40 at 234 s. Each cue lasts 4.9 s."""
    cues = [_cue(i, (i - 1) * 6.0, [f"w{i}_{k}" for k in range(10)]) for i in range(1, 41)]
    return Transcript(language="en", cues=cues)
