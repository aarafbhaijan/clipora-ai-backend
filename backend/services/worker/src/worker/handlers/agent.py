"""Agent turn: run the edit graph for one user message (architecture.md 11.1)."""

from core.enums import JobKind
from core.jobs import AgentPayload
from db.models import Project
from db.session import session_scope
from worker.context import JobContext, Outcome
from worker.handlers import register


@register(JobKind.agent)
def handle(ctx: JobContext) -> Outcome:
    import rag
    from agent import store
    from agent.apps import edit_app, embed_one
    from agent.edit_graph import EditDeps
    from agent.llm import CallContext, LLMOutputError, default_client
    from agent.runner import run_turn

    payload = AgentPayload.model_validate(ctx.payload)
    with session_scope() as tx:
        p = tx.get(Project, payload.project_id)
        if p is None or p.deleted_at is not None or p.user_id != ctx.user_id:
            return Outcome.done  # the video was deleted; nothing to answer
    llm = default_client()
    deps = EditDeps(
        llm=llm,
        call=CallContext(ctx.user_id, payload.project_id, ctx.job_id, ctx.kind),
        session=session_scope,
        progress=ctx.progress,
        check=ctx.check,
        retrieve=rag.retrieve,
        match_memories=rag.match_memories,
        embed=embed_one,
        model_id=llm.model_ids["main"],
    )
    fresh = {"project_id": str(payload.project_id), "user_id": str(ctx.user_id), "message_id": str(payload.message_id)}
    try:
        end = run_turn(edit_app(), str(payload.project_id), str(payload.message_id), deps, fresh)
    except LLMOutputError:
        # Invalid output after the retries fails the turn with a message, not the job (architecture.md 15).
        with session_scope() as tx:
            if store.existing_reply(tx, payload.project_id, payload.message_id) is None:
                store.post_message(
                    tx,
                    payload.project_id,
                    "Sorry, I could not work that out. Please try again or rephrase.",
                    payload={"reply_to": str(payload.message_id), "error": True},
                )
        return Outcome.done
    return Outcome.waiting_input if end == "waiting_input" else Outcome.done
