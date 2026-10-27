"""The speaker the crop follows, for the editor's speaker overlay (green box) and the chat's "who is speaking".

YOLO and the lip-movement step run once per video at ingest (architecture.md sections 10 and 12.1) and their
results are cached with the detections, so answering this never re-runs a model. Who talks when comes from
`Detections.turns`; between turns the frame stays on the last talker, and before anyone talks it is on the main
person (core.crop.main_track). These are the same rules the crop path and the export use, so the green box is
exactly what a portrait export follows.
"""

from collections import Counter

from sqlalchemy.orm import Session

from api.assets import detections_key, ingest_markers
from api.schemas import SpeakerBox, SpeakerFrame, SpeakerOut, SpeakerTurnOut
from api.services.plans import current_version_row, load_detections
from core.crop import CUT_MIN_S, main_track
from core.edl import EDL, Segment
from db.models import Project


def speaker_for(tx: Session, project: Project) -> SpeakerOut:
    markers = ingest_markers(tx, project.id)
    key = detections_key(markers)
    if not key:
        return SpeakerOut(available=False)
    det = load_detections(key)
    current = current_version_row(tx, project)
    if current is not None:
        segments = EDL.model_validate(current.edl).segments
    else:
        segments = [Segment(id="s1", src_start=0.0, src_end=float(project.duration_s or 0) or 1e9)]
    main = main_track(det, segments)

    def in_clips(t: float) -> bool:
        return any(s.src_start <= t <= s.src_end for s in segments)

    clip_frames = [f for f in det.frames if in_clips(f.t)]
    seen = sum(1 for f in clip_frames if any(b.track_id == main for b in f.boxes))
    per_track = Counter(b.track_id for f in det.frames for b in f.boxes)
    min_frames = max(1, round(det.fps))  # about one second on screen
    labels: dict[int, str] = {}
    for turn in det.turns:
        labels.setdefault(turn.track_id, f"Person {len(labels) + 1}")
    return SpeakerOut(
        available=True,
        analysed="speakers" in markers,
        cut_min_s=CUT_MIN_S,
        turns=[
            SpeakerTurnOut(
                start=t.start, end=t.end, track_id=t.track_id, label=labels[t.track_id], confidence=t.confidence
            )
            for t in det.turns
        ],
        fps=det.fps,
        main_track_id=main,
        coverage=round(seen / len(clip_frames), 3) if clip_frames else 0.0,
        people=sum(1 for n in per_track.values() if n >= min_frames),
        frames=[
            SpeakerFrame(
                t=round(f.t, 3),
                boxes=[
                    SpeakerBox(
                        track_id=b.track_id, x1=round(b.x1, 4), y1=round(b.y1, 4), x2=round(b.x2, 4), y2=round(b.y2, 4)
                    )
                    for b in f.boxes
                ],
            )
            for f in det.frames
        ],
    )
