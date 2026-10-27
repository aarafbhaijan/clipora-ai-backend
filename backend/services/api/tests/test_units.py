"""API logic that runs without a database. Endpoint tests against Postgres live in test_endpoints.py."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from api.views import history_state, split_jobs


def job(kind: str, status: str, created: int = 0) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), kind=kind, status=status, created_at=datetime.fromtimestamp(created, UTC))


def project(status: str = "ready", version: int = 0) -> Any:
    return SimpleNamespace(status=status, current_version=version)


def test_history_states() -> None:
    assert history_state(project("uploading"), None, None, False, None) == "uploading"
    assert history_state(project("ingesting"), job("ingest", "queued"), None, False, None) == "waiting"
    assert history_state(project("ingesting"), job("ingest", "running"), None, False, None) == "processing"
    assert history_state(project(), job("suggest", "running"), None, False, None) == "finding_ideas"
    assert history_state(project(), None, None, True, None) == "pick_idea"
    assert history_state(project(), job("agent", "waiting_input"), None, True, None) == "needs_answer"
    assert history_state(project(version=2), job("export", "running"), None, True, None) == "exporting"
    done = SimpleNamespace(status="done")
    assert history_state(project(version=2), None, None, True, done) == "exported"
    assert history_state(project("failed"), None, job("ingest", "failed"), False, None) == "failed"


def test_split_jobs_prefers_export_over_metadata() -> None:
    active, failed = split_jobs([job("metadata", "running"), job("export", "queued"), job("agent", "failed")])
    assert active is not None and active.kind == "export"
    assert failed is not None and failed.kind == "agent"
