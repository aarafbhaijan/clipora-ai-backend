"""Handler registry. Each queue imports only its own handler modules, so the export worker never loads torch.

A handler is `def handle(ctx: JobContext) -> Outcome`. It must be idempotent: assume it runs twice.
"""

import importlib
from collections.abc import Callable

from core.enums import JobKind, QueueName
from worker.context import JobContext, Outcome

Handler = Callable[[JobContext], Outcome]

_HANDLERS: dict[JobKind, Handler] = {}

QUEUE_MODULES: dict[QueueName, tuple[str, ...]] = {
    QueueName.ingest: ("worker.handlers.ingest",),
    QueueName.agent: (
        "worker.handlers.agent",
        "worker.handlers.suggest",
        "worker.handlers.metadata",
        "worker.handlers.distill",
    ),
    QueueName.export: ("worker.handlers.export",),
}


def register(kind: JobKind) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return deco


def load(queue: QueueName) -> None:
    for module in QUEUE_MODULES[queue]:
        importlib.import_module(module)


def handler_for(kind: str) -> Handler | None:
    return _HANDLERS.get(JobKind(kind))
