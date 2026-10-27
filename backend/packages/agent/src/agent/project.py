"""Read what the agent needs about one project: the row, transcript, detections and current plan.

Keys come from the latest ingest job's stage markers (contract in backend/services/api/src/api/assets.py).
Transcript and detections are cached per key, so a graph run does not re-download them per node.
"""

import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import storage
from core.crop import Detections
from core.edl import EDL
from core.timemap import output_duration
from core.transcript import Cue, Transcript
from db.models import EdlVersion, Job, Project


@dataclass(frozen=True)
class ProjectInfo:
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    duration_s: float
    width: int
    height: int
    fps: float
    source_key: str
    current_version: int
    transcript_key: str | None
    detections_key: str | None


def ingest_markers(tx: Session, project_id: uuid.UUID) -> dict[str, Any]:
    job = tx.execute(
        select(Job).where(Job.project_id == project_id, Job.kind == "ingest").order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    return dict(job.stage_markers) if job else {}


def load_project(tx: Session, project_id: uuid.UUID, user_id: uuid.UUID) -> ProjectInfo:
    p = tx.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id, Project.deleted_at.is_(None))
    ).scalar_one()
    markers = ingest_markers(tx, p.id)
    tr = markers.get("transcribe")
    # The speakers stage's copy (detections plus who talks when) when it ran, else the plain YOLO detections.
    det = next(
        (markers[k] for k in ("speakers", "detect") if isinstance(markers.get(k), dict) and markers[k].get("key")), None
    )
    return ProjectInfo(
        id=p.id,
        user_id=p.user_id,
        title=p.title,
        duration_s=float(p.duration_s or 0),
        width=int(p.width or 1920),
        height=int(p.height or 1080),
        fps=float(p.fps or 30),
        source_key=p.source_key,
        current_version=p.current_version,
        transcript_key=str(tr["transcript_key"]) if isinstance(tr, dict) and tr.get("transcript_key") else None,
        detections_key=str(det["key"]) if isinstance(det, dict) and det.get("key") else None,
    )


@lru_cache(maxsize=16)
def transcript(key: str) -> Transcript:
    return Transcript.model_validate(json.loads(storage.get_bytes(key)))


@lru_cache(maxsize=16)
def detections(key: str) -> Detections:
    return Detections.model_validate(json.loads(storage.get_bytes(key)))


def load_transcript(info: ProjectInfo) -> Transcript:
    return transcript(info.transcript_key) if info.transcript_key else Transcript(cues=[])


def load_detections(info: ProjectInfo) -> Detections | None:
    return detections(info.detections_key) if info.detections_key else None


def current_edl(tx: Session, project_id: uuid.UUID, version: int) -> EDL | None:
    if not version:
        return None
    row = tx.execute(
        select(EdlVersion).where(EdlVersion.project_id == project_id, EdlVersion.version == version)
    ).scalar_one_or_none()
    return EDL.model_validate(row.edl) if row else None


def fmt_time(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


def cue_lines(cues: list[Cue]) -> str:
    """One line per cue: `[c12 01:02.40-01:05.10] text`. The ids are what the model cites."""
    return "\n".join(f"[{c.id} {fmt_time(c.start)}-{fmt_time(c.end)}] {c.text}" for c in cues)


def plan_summary(edl: EDL) -> str:
    lines = [
        f"output {edl.output.aspect} {edl.output.width}x{edl.output.height} framing={edl.output.framing.value} "
        f"target={edl.output.target_duration_s} length={output_duration(edl):.1f}s",
        f"default transition {edl.transitions.default.type.value} {edl.transitions.default.duration_s}s",
    ]
    lines += [
        f"{s.id}: {fmt_time(s.src_start)}-{fmt_time(s.src_end)} ({s.length:.1f}s) {s.reason or ''}".rstrip()
        for s in edl.segments
    ]
    return "\n".join(lines)


def cues_in(transcript: Transcript, start: float, end: float) -> list[Cue]:
    return [c for c in transcript.cues if c.end > start and c.start < end]
