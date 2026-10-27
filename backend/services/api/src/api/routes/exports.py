"""Exports: start rendering a plan version, poll its progress and download links."""

import uuid

from fastapi import APIRouter

from api.access import own_project
from api.auth import Me
from api.deps import Tx
from api.schemas import ExportIn, ExportOut
from api.services.exports import export_out, owned_export
from api.services.exports import start_export as create_export

router = APIRouter(tags=["editing"])


@router.post("/projects/{project_id}/exports", response_model=ExportOut, status_code=201)
def start_export(project_id: uuid.UUID, body: ExportIn, user: Me, tx: Tx) -> ExportOut:
    project = own_project(tx, user, project_id, lock=True)
    return export_out(tx, create_export(tx, user.id, project, body.edl_version))


@router.get("/exports/{export_id}", response_model=ExportOut)
def get_export(export_id: uuid.UUID, user: Me, tx: Tx) -> ExportOut:
    return export_out(tx, owned_export(tx, user.id, export_id))
