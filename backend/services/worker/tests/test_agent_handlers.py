"""Agent-queue handlers register for their job kinds (imports only; no database or Bedrock)."""

import pytest

pytest.importorskip("worker.handlers")


def test_agent_queue_handlers_register() -> None:
    from core.enums import JobKind, QueueName
    from worker import handlers

    for module in handlers.QUEUE_MODULES[QueueName.agent]:
        __import__(module)
    for kind in (JobKind.agent, JobKind.suggest, JobKind.metadata, JobKind.distill):
        assert handlers.handler_for(kind.value) is not None
