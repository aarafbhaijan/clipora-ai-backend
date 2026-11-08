"""Plans: save the user's plan with a version check, read the version history and one version."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header

from api.access import own_project
from api.auth import Me
from api.deps import Tx
from api.schemas import EdlOut, EdlVersionOut, ReframeIn, SpeakerOut
from api.services.plans import reframe_plan, save_user_plan, version_history, version_row
from api.services.speaker import speaker_for
from core.edl import EDL
from core.enums import Framing
from core.errors import AppError, ErrorCode

router = APIRouter(tags=["editing"])


@router.patch(
    "/projects/{project_id}/edl",
    response_model=EdlOut,
    responses={409: {"description": "version_conflict, details.latest holds the current plan"}},
)
def save_edl(
    project_id: uuid.UUID,
    edl: EDL,
    user: Me,
    tx: Tx,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> EdlOut:
    project = own_project(tx, user, project_id, lock=True)
    version, saved = save_user_plan(tx, user, project, edl, if_match)
    return EdlOut(version=version, edl=saved)


@router.post("/projects/{project_id}/reframe", response_model=EdlOut, status_code=201)
def reframe(project_id: uuid.UUID, body: ReframeIn, user: Me, tx: Tx) -> EdlOut:
    """The whole video in another shape (e.g. portrait), following the speaker: a new edited video, ready to export."""
    project = own_project(tx, user, project_id, lock=True)
    version, saved = reframe_plan(tx, user, project, body.aspect, Framing(body.framing))
    return EdlOut(version=version, edl=saved)


@router.get("/projects/{project_id}/edl/versions", response_model=list[EdlVersionOut])
def edl_versions(project_id: uuid.UUID, user: Me, tx: Tx) -> list[EdlVersionOut]:
    """Newest first. Each version names its generated video, so the editor can group them."""
    return version_history(tx, own_project(tx, user, project_id))


@router.get("/projects/{project_id}/edl/versions/{version}", response_model=EdlOut)
def edl_version(project_id: uuid.UUID, version: int, user: Me, tx: Tx) -> EdlOut:
    project = own_project(tx, user, project_id)
    row = version_row(tx, project.id, version)
    if row is None:
        raise AppError(ErrorCode.not_found, "That version does not exist.")
    return EdlOut(version=row.version, edl=EDL.model_validate(row.edl))


@router.get("/projects/{project_id}/speaker", response_model=SpeakerOut)
def speaker(project_id: uuid.UUID, user: Me, tx: Tx) -> SpeakerOut:
    """YOLO person boxes and the main speaker the crop follows (for the editor overlay)."""
    return speaker_for(tx, own_project(tx, user, project_id))
