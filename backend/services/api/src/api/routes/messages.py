"""Edit chat: post a message (starts or resumes the agent), ask for more ideas."""

import uuid

from fastapi import APIRouter

from api.access import own_project
from api.auth import Me
from api.deps import Tx
from api.schemas import JobOut, SendMessageIn, SendMessageOut
from api.services.conversation import post_user_message, request_more_ideas
from api.views import job_out, message_out

router = APIRouter(tags=["editing"])


@router.post("/projects/{project_id}/messages", response_model=SendMessageOut, status_code=201)
def send_message(project_id: uuid.UUID, body: SendMessageIn, user: Me, tx: Tx) -> SendMessageOut:
    project = own_project(tx, user, project_id, lock=True)
    message, job = post_user_message(tx, user.id, project, body.content, body.suggestion_id, body.choice)
    out = job_out(job)
    assert out is not None
    return SendMessageOut(message=message_out(message), job=out)


@router.post("/projects/{project_id}/ideas", response_model=JobOut, status_code=202)
def more_ideas(project_id: uuid.UUID, user: Me, tx: Tx) -> JobOut:
    """More ideas: a suggest job that always posts, even after the user has chatted."""
    project = own_project(tx, user, project_id, lock=True)
    out = job_out(request_more_ideas(tx, user.id, project))
    assert out is not None
    return out
