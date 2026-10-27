"""Plans (EDLs): saving a user's version with the version check, crop recompute, and the version history
grouped into generated videos (architecture.md section 7)."""

import json
import uuid
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

import storage
from api.assets import detections_key, ingest_markers
from api.auth import CurrentUser
from api.schemas import EdlVersionOut, SegmentBrief
from api.services.learning import record_plan_edit
from core.crop import Detections, crop_path
from core.edl import EDL
from core.enums import Author, MessageRole, Thread
from core.errors import AppError, ErrorCode
from core.timemap import output_duration
from core.validation import validate
from db.ids import uuid7
from db.models import EdlVersion, Message, Project

MAX_VERSIONS = 500


def current_version_row(tx: Session, project: Project) -> EdlVersion | None:
    if not project.current_version:
        return None
    return tx.execute(
        select(EdlVersion).where(EdlVersion.project_id == project.id, EdlVersion.version == project.current_version)
    ).scalar_one()


def version_row(tx: Session, project_id: uuid.UUID, version: int) -> EdlVersion | None:
    return tx.execute(
        select(EdlVersion).where(EdlVersion.project_id == project_id, EdlVersion.version == version)
    ).scalar_one_or_none()


# ---- Crops ------------------------------------------------------------------------------------------------


@lru_cache(maxsize=32)
def load_detections(key: str) -> Detections:
    """YOLO person boxes written by ingest stage 6 (cached: the file never changes)."""
    return Detections.model_validate(json.loads(storage.get_bytes(key)))


def recompute_crops(tx: Session, project: Project, new: EDL, old: EDL | None) -> EDL:
    """Recompute keyframes only for segments whose range changed, or all when the output size changed."""
    key = detections_key(ingest_markers(tx, project.id))
    detections = load_detections(key) if key else None
    size_changed = old is None or (old.output.width, old.output.height) != (new.output.width, new.output.height)
    old_ranges = {s.id: (s.src_start, s.src_end) for s in old.segments} if old else {}
    changed = [
        s for s in new.segments if size_changed or not s.crop or old_ranges.get(s.id) != (s.src_start, s.src_end)
    ]
    if not changed:
        return new
    src, out = new.source, new.output
    fresh = {s.id: s for s in crop_path(detections, changed, src.width, src.height, out.width, out.height)}
    return new.model_copy(update={"segments": [fresh.get(s.id, s) for s in new.segments]})


# ---- Saving -----------------------------------------------------------------------------------------------


def save_user_plan(tx: Session, user: CurrentUser, project: Project, edl: EDL, if_match: int) -> tuple[int, EDL]:
    """Append the user's plan as the next version. 409 version_conflict (with the latest plan) if it moved."""
    current = current_version_row(tx, project)
    if if_match != project.current_version:
        latest = EDL.model_validate(current.edl).to_json() if current else None
        raise AppError(
            ErrorCode.version_conflict,
            "The plan changed while you were editing.",
            {"version": project.current_version, "latest": latest},
        )
    if project.width is None or project.height is None or project.duration_s is None:
        raise AppError(ErrorCode.bad_request, "This video is still being prepared.")
    old = EDL.model_validate(current.edl) if current else None
    # The source is always the project's own video, whatever the client sent.
    source = edl.source.model_copy(
        update={
            "duration_s": project.duration_s,
            "width": project.width,
            "height": project.height,
            "fps": project.fps or 30,
        }
    )
    version = project.current_version + 1
    new = recompute_crops(tx, project, edl.model_copy(update={"source": source, "version": version}), old)
    validate(new, Author.user)
    tx.add(
        EdlVersion(
            id=uuid7(),
            project_id=project.id,
            version=version,
            edl=new.to_json(),
            author=Author.user.value,
            parent_version=project.current_version or None,
        )
    )
    project.current_version = version
    if current is not None:
        record_plan_edit(tx, user, project, current, new)
    return version, new


# ---- History, grouped into generated videos ---------------------------------------------------------------


def _request_titles(tx: Session, project_id: uuid.UUID) -> dict[int, str]:
    """For plans made before shorts were tagged: the idea title or request that produced each version."""
    titles: dict[int, str] = {}
    last_request = ""
    for m in tx.execute(
        select(Message)
        .where(Message.project_id == project_id, Message.thread == Thread.edit.value)
        .order_by(Message.created_at)
    ).scalars():
        if m.role == MessageRole.user.value:
            idea = (m.payload or {}).get("suggestion") or {}
            last_request = str(idea.get("title") or m.content or "").removeprefix("Make this: ")[:58]
        elif m.edl_version and m.edl_version not in titles and last_request:
            titles[m.edl_version] = last_request
    return titles


def version_history(tx: Session, project: Project) -> list[EdlVersionOut]:
    """Newest first. Each version names its generated video (edl.meta.short); untagged user versions inherit
    their parent's, untagged agent versions are titled from the request that made them."""
    rows = tx.execute(
        select(EdlVersion).where(EdlVersion.project_id == project.id).order_by(EdlVersion.version).limit(MAX_VERSIONS)
    ).scalars()
    shorts: dict[int, tuple[str, str]] = {}
    requested = _request_titles(tx, project.id)
    out = []
    for r in rows:
        e = EDL.model_validate(r.edl)
        tag = (e.meta.model_extra or {}).get("short")
        if isinstance(tag, dict) and tag.get("id"):
            short = (str(tag["id"]), str(tag.get("title") or "Short video"))
        elif r.parent_version in shorts and r.author == "user":
            short = shorts[r.parent_version]
        else:
            short = (f"v{r.version}", requested.get(r.version) or f"Plan {r.version}")
        shorts[r.version] = short
        out.append(
            EdlVersionOut(
                version=r.version,
                author=r.author,
                parent_version=r.parent_version,
                created_at=r.created_at,
                segment_count=len(e.segments),
                output_duration_s=round(output_duration(e), 2),
                short_id=short[0],
                short_title=short[1],
                aspect=e.output.aspect,
                segments=[SegmentBrief(id=s.id, src_start=s.src_start, src_end=s.src_end) for s in e.segments],
            )
        )
    return out[::-1]
