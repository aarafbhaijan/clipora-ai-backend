"""Ideas after ingest (architecture.md 11.2). Discarded if the user already sent a message."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.enums import JobKind, MessageKind, MessageRole, Thread
from core.jobs import SuggestPayload
from db.models import Message
from db.session import session_scope
from worker.context import JobContext, Outcome
from worker.handlers import register


def _user_spoke(tx: Session, project_id: uuid.UUID) -> bool:
    return (
        tx.execute(
            select(Message.id)
            .where(
                Message.project_id == project_id,
                Message.thread == Thread.edit.value,
                Message.role == MessageRole.user.value,
            )
            .limit(1)
        ).first()
        is not None
    )


@register(JobKind.suggest)
def handle(ctx: JobContext) -> Outcome:
    from agent import store
    from agent.llm import CallContext, default_client
    from agent.project import load_project, load_transcript
    from agent.suggest import SuggestOut, resolve_ideas, suggest

    payload = SuggestPayload.model_validate(ctx.payload)
    pid = payload.project_id
    with session_scope() as tx:
        if not payload.more and _user_spoke(tx, pid):
            return Outcome.done
        shown = list(
            tx.execute(
                select(Message).where(Message.project_id == pid, Message.kind == MessageKind.suggestions.value)
            ).scalars()
        )
        if shown and not payload.more:
            return Outcome.done  # already posted by an earlier run
        info = load_project(tx, pid, ctx.user_id)
    titles = [i["title"] for m in shown for i in m.payload.get("ideas", [])]
    transcript = load_transcript(info)
    ctx.progress("finding ideas", 20)
    if transcript.cues:
        out, version = suggest(
            default_client(), CallContext(ctx.user_id, pid, ctx.job_id, ctx.kind), transcript, titles
        )
    else:
        out, version = SuggestOut(summary="", topics=[], ideas=[]), "none"
    ideas = resolve_ideas(out, transcript, set(titles))
    ctx.check()
    with session_scope() as tx:
        if not payload.more and _user_spoke(tx, pid):
            return Outcome.done
        store.post_message(
            tx,
            pid,
            out.summary or "Here are some ideas for this video.",
            kind=MessageKind.suggestions,
            payload={"summary": out.summary, "topics": out.topics, "ideas": ideas, "prompt_version": version},
        )
    return Outcome.done
