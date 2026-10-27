"""Metadata turn: run the metadata graph for one message (architecture.md 11.3)."""

from core.enums import JobKind, Thread
from core.jobs import MetadataPayload
from db.models import Project
from db.session import session_scope
from worker.context import JobContext, Outcome
from worker.handlers import register


@register(JobKind.metadata)
def handle(ctx: JobContext) -> Outcome:
    from agent import store
    from agent.apps import metadata_app
    from agent.llm import CallContext, LLMOutputError, default_client
    from agent.metadata_graph.graph import MetaDeps
    from agent.runner import run_turn

    payload = MetadataPayload.model_validate(ctx.payload)
    if payload.message_id is None:
        return Outcome.done
    with session_scope() as tx:
        p = tx.get(Project, payload.project_id)
        if p is None or p.deleted_at is not None or p.user_id != ctx.user_id:
            return Outcome.done
    deps = MetaDeps(
        llm=default_client(),
        call=CallContext(ctx.user_id, payload.project_id, ctx.job_id, ctx.kind),
        session=session_scope,
        progress=ctx.progress,
        check=ctx.check,
    )
    fresh = {
        "project_id": str(payload.project_id),
        "user_id": str(ctx.user_id),
        "message_id": str(payload.message_id),
        "edl_version": payload.edl_version,
        "options": payload.options,
    }
    try:
        end = run_turn(metadata_app(), f"{payload.project_id}:metadata", str(payload.message_id), deps, fresh)
    except LLMOutputError:
        with session_scope() as tx:
            if store.existing_reply(tx, payload.project_id, payload.message_id, Thread.metadata) is None:
                store.post_message(
                    tx,
                    payload.project_id,
                    "Sorry, I could not write that. Please try again.",
                    thread=Thread.metadata,
                    payload={"reply_to": str(payload.message_id), "error": True},
                )
        return Outcome.done
    return Outcome.waiting_input if end == "waiting_input" else Outcome.done
