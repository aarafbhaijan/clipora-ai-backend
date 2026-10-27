"""Add "who talks when" to videos ingested before the speakers stage existed.

    python -m worker.backfill_speakers                 # every ready video without it
    python -m worker.backfill_speakers <project_id>... # just these (recomputed even if done)

Runs inline (not through the queue), one video at a time, and records its result as the ingest job's
"speakers" marker, exactly as a fresh ingest would. Plans already saved keep their crop until their clips or the
output size change (the crop is then recomputed and follows the talker).
"""

import argparse
import logging
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

import storage
from core.crop import Detections
from core.enums import ProjectStatus
from core.transcript import Transcript
from db.models import Job, Project
from db.session import session_scope
from worker.speakers import KEY_NAME, with_turns

log = logging.getLogger("backfill")


def _todo(ids: list[str]) -> list[uuid.UUID]:
    with session_scope() as tx:
        q = select(Project.id).where(Project.status == ProjectStatus.ready.value, Project.deleted_at.is_(None))
        if ids:
            return list(tx.execute(q.where(Project.id.in_([uuid.UUID(i) for i in ids]))).scalars())
        out = []
        for pid in tx.execute(q.order_by(Project.created_at)).scalars():
            job = _ingest_job(tx, pid)
            if job is not None and "speakers" not in (job.stage_markers or {}):
                out.append(pid)
        return out


def _ingest_job(tx: Session, project_id: uuid.UUID) -> Job | None:
    return tx.execute(
        select(Job).where(Job.project_id == project_id, Job.kind == "ingest").order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def backfill(project_id: uuid.UUID) -> int:
    with session_scope() as tx:
        p = tx.get(Project, project_id)
        job = _ingest_job(tx, project_id)
        assert p is not None and job is not None
        markers = dict(job.stage_markers or {})
        source_key, user_id = p.source_key, p.user_id
        meta = {"width": int(p.width or 0), "height": int(p.height or 0), "duration_s": float(p.duration_s or 0)}
    detections = Detections.model_validate_json(storage.get_bytes(markers["detect"]["key"]))
    transcript = Transcript.model_validate_json(storage.get_bytes(markers["transcribe"]["transcript_key"]))
    with tempfile.TemporaryDirectory(prefix="speakers-") as work:
        src = Path(work) / ("source" + Path(source_key).suffix)
        storage.download_file(source_key, str(src))
        detections = with_turns(str(src), meta, detections, transcript, Path(work))
    key = storage.asset_key(user_id, project_id, KEY_NAME)
    storage.put_bytes(key, detections.model_dump_json().encode(), "application/json")
    with session_scope() as tx:
        job = _ingest_job(tx, project_id)
        assert job is not None
        job.stage_markers = {**(job.stage_markers or {}), "speakers": {"key": key, "turns": len(detections.turns)}}
    return len(detections.turns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_ids", nargs="*")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    todo = _todo(args.project_ids)
    log.info("%d video(s) to analyse", len(todo))
    for pid in todo:
        try:
            log.info("%s: %d speaker turns", pid, backfill(pid))
        except Exception:
            log.exception("%s: failed", pid)


if __name__ == "__main__":
    main()
