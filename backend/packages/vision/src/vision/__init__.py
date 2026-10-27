"""YOLO person detection and tracking (ingest stage 6).

Frames come from ffmpeg at 3 fps; YOLOv8n finds people (class 0) at confidence 0.35 and ByteTrack keeps
ids across frames. The result is `core.crop.Detections`, cached as `detections.json`. The crop path
itself is the pure function `core.crop.crop_path`, so the API can recompute it without this package.
"""

import os
import subprocess
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import numpy as np

from core.crop import Box, DetectionFrame, Detections
from media.commands import DETECT_FPS, detect_frames
from media.ff import MediaError

CONFIDENCE = 0.35
PERSON = 0
MODEL = os.environ.get("YOLO_MODEL", "yolov8n.pt")


@lru_cache(maxsize=1)
def _model() -> Any:
    from ultralytics.models import YOLO  # heavy import, only in worker-ingest

    return YOLO(MODEL)


def boxes_from_result(result: Any) -> list[Box]:
    """Tracked person boxes from one ultralytics result, normalised to the frame."""
    boxes = result.boxes
    if boxes is None or boxes.id is None:
        return []
    out = []
    for xyxy, conf, track, cls in zip(
        boxes.xyxyn.tolist(), boxes.conf.tolist(), boxes.id.tolist(), boxes.cls.tolist(), strict=False
    ):
        if int(cls) != PERSON:
            continue
        x1, y1, x2, y2 = (round(float(v), 4) for v in xyxy)
        out.append(Box(track_id=int(track), x1=x1, y1=y1, x2=x2, y2=y2, conf=round(float(conf), 3)))
    return out


def detect_people(
    src: str,
    width: int,
    height: int,
    duration_s: float,
    *,
    on_progress: Callable[[float], None] | None = None,
    check: Callable[[], None] | None = None,
    register: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> Detections:
    args, w, h = detect_frames(src, width, height)
    frame_bytes = w * h * 3
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if register is not None:
        register(proc)
    assert proc.stdout is not None
    model = _model()
    frames: list[DetectionFrame] = []
    n = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            if check is not None:
                check()
            image = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 3))
            result = model.track(
                image, persist=True, tracker="bytetrack.yaml", classes=[PERSON], conf=CONFIDENCE, verbose=False
            )[0]
            t = round(n / DETECT_FPS, 3)
            frames.append(DetectionFrame(t=t, boxes=boxes_from_result(result)))
            n += 1
            if on_progress is not None and duration_s > 0 and n % DETECT_FPS == 0:
                on_progress(min(1.0, t / duration_s))
    finally:
        _, err = proc.communicate()
        _reset_tracker(model)
    if proc.returncode not in (0, None) and not frames:
        raise MediaError("frame decode for detection failed", err.decode(errors="replace")[-2000:])
    return Detections(fps=DETECT_FPS, frames=frames)


def _reset_tracker(model: Any) -> None:
    """The model is cached per process; drop tracker state so the next video starts fresh."""
    predictor = getattr(model, "predictor", None)
    trackers = getattr(predictor, "trackers", None) if predictor is not None else None
    for tracker in trackers or []:
        if hasattr(tracker, "reset"):
            tracker.reset()
