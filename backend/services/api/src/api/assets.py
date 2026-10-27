"""Asset URLs for the editor, read from the ingest job's stage markers.

Contract with worker-ingest: each stage marker's data names the keys it wrote.
  probe       {"duration_s", "width", "height", "fps"}
  transcribe  {"transcript_key", "srt_key"}
  proxy       {"key"}
  thumbnails  {"poster_key", "interval_s", "tile_width", "tile_height", "columns", "rows",
               "sheets": [{"key", "start_s", "count"}]}
  waveform    {"key"}
  detect      {"key"}
  speakers    {"key", "turns"}   detections.json plus who talks when (core.speakers); preferred over detect
  index       {"chunks"}
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import storage
from api.schemas import Assets, SpriteSheet, Thumbnails
from db.models import Job


def ingest_markers(tx: Session, project_id: uuid.UUID) -> dict[str, Any]:
    job = tx.execute(
        select(Job).where(Job.project_id == project_id, Job.kind == "ingest").order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    return dict(job.stage_markers) if job else {}


def _url(marker: Any, field: str = "key") -> str | None:
    if isinstance(marker, dict) and marker.get(field):
        return storage.asset_url(str(marker[field]))
    return None


def poster_url(markers: dict[str, Any]) -> str | None:
    return _url(markers.get("thumbnails"), "poster_key")


def assets_from(markers: dict[str, Any], source_key: str | None = None) -> Assets:
    thumbs = markers.get("thumbnails")
    thumbnails = None
    if isinstance(thumbs, dict) and thumbs.get("sheets"):
        thumbnails = Thumbnails(
            interval_s=thumbs["interval_s"],
            tile_width=thumbs["tile_width"],
            tile_height=thumbs["tile_height"],
            columns=thumbs["columns"],
            rows=thumbs["rows"],
            sheets=[
                SpriteSheet(url=storage.asset_url(s["key"]), start_s=s["start_s"], count=s["count"])
                for s in thumbs["sheets"]
            ],
        )
    return Assets(
        proxy_url=_url(markers.get("proxy")),
        source_url=storage.asset_url(source_key) if source_key and markers.get("proxy") else None,
        waveform_url=_url(markers.get("waveform")),
        transcript_url=_url(markers.get("transcribe"), "transcript_key"),
        srt_url=_url(markers.get("transcribe"), "srt_key"),
        thumbnails=thumbnails,
    )


def detections_key(markers: dict[str, Any]) -> str | None:
    """The speakers stage's copy (detections with turns) when it ran, else the plain YOLO detections."""
    for stage in ("speakers", "detect"):
        d = markers.get(stage)
        if isinstance(d, dict) and d.get("key"):
            return str(d["key"])
    return None
