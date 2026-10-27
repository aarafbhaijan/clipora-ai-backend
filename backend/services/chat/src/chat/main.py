"""Chat service: `uvicorn chat.main:app --port 8001`. Routed at /api/chat/* (Next.js rewrite locally,
CloudFront path pattern on AWS). Auth, ownership and readiness are checked before the stream starts, so those
failures are normal HTTP errors; everything after that arrives as SSE events (see chat.turn)."""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.llm import default_client
from api.access import own_project
from api.auth import current_user
from api.services.jobs import active_job
from chat.heartbeat import with_heartbeat
from chat.turn import TurnInput, run_turn
from core.enums import JobKind
from core.errors import AppError, ErrorCode
from db.session import engine, session_scope

log = logging.getLogger("chat")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    engine(pool_size=5)
    default_client()  # fail at startup, not on the first message, if the LLM settings are missing
    yield


app = FastAPI(
    title="Clipora AI chat",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/chat/docs",
    openapi_url="/api/chat/openapi.json",
)


@app.exception_handler(AppError)
def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(jsonable_encoder(exc.envelope()), status_code=exc.http_status)


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


@app.get("/api/chat/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/chat/projects/{project_id}/messages")
def chat(project_id: uuid.UUID, body: ChatIn, request: Request) -> StreamingResponse:
    """Route the message; stream the answer for questions and small talk (text/event-stream)."""
    text = body.content.strip()
    if not text:
        raise AppError(ErrorCode.bad_request, "Type a message.")
    with session_scope() as tx:
        user = current_user(request, tx)
        project = own_project(tx, user, project_id)
        if project.status != "ready":
            raise AppError(ErrorCode.bad_request, "This video is still being prepared.")
        busy = active_job(tx, project.id, JobKind.agent.value)
        waiting = busy is not None and busy.status == "waiting_input"
    turn = TurnInput(user=user, project_id=project_id, text=text, waiting_for_answer=waiting)
    return StreamingResponse(
        with_heartbeat(run_turn(default_client(), turn)),
        media_type="text/event-stream",
        # no-transform and X-Accel-Buffering stop proxies (Next.js, nginx, CloudFront) from buffering the stream
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
