"""Learning: turn feedback events into preference statements (architecture.md 11.4)."""

from core.enums import JobKind
from core.jobs import DistillPayload
from db.session import session_scope
from worker.context import JobContext, Outcome
from worker.handlers import register


@register(JobKind.distill)
def handle(ctx: JobContext) -> Outcome:
    from agent.apps import embed_one
    from agent.distill import distill
    from agent.llm import CallContext, default_client

    payload = DistillPayload.model_validate(ctx.payload)
    ctx.progress("learning", 10)
    distill(
        session_scope,
        default_client(),
        CallContext(payload.user_id, None, ctx.job_id, ctx.kind),
        payload.user_id,
        embed_one,
    )
    return Outcome.done
