"""Mouth movement per tracked person (ingest stage "speakers", architecture.md section 12.1).

Frames come from ffmpeg at 12 fps. For each YOLO person box (the nearest detection frame, so faces keep the
person's track id) we cut out the head, enlarge it, and run MediaPipe's face landmarker; its jawOpen score says
how open the mouth is. Each sample also carries the audio loudness at that moment. Turning these into
"who talks when" is the pure function `core.speakers.speaker_turns`.
"""

import hashlib
import os
import subprocess
import urllib.request
from bisect import bisect_left
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from core.crop import Box, Detections
from core.speakers import LipFrame
from media.commands import detect_frames
from media.ff import MediaError

LIP_FPS = 12  # syllables come 4-6 a second; 12 samples a second see the mouth open and close
LIP_SHORT_SIDE = 720  # decode larger than YOLO's 640 frames: lips need detail
HEAD_SIDE = 256  # head crops are enlarged (or shrunk) to this before the landmarker
MIN_HEAD_PX = 40  # heads smaller than this in the decoded frame are too small to read (their scores are noise)
MAX_BOX_GAP_S = 0.5  # YOLO runs at 3 fps; farther than this from a detection frame, nobody is measured
AUDIO_RATE = 16000
SOUND_LAG_S = 0.08  # the mouth opens about a frame before the sound is heard; measured on real talks

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"


def model_path() -> str:
    """Landmarker weights: FACE_MODEL if set (the worker image bakes them in), else downloaded once to the cache."""
    configured = os.environ.get("FACE_MODEL")
    if configured:
        return configured
    path = Path.home() / ".cache" / "clipora" / "face_landmarker.task"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        data = urllib.request.urlopen(MODEL_URL, timeout=60).read()  # noqa: S310 (fixed https URL)
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            raise MediaError("face model download failed", "sha256 mismatch")
        path.write_bytes(data)
    return str(path)


@lru_cache(maxsize=1)
def _landmarker() -> Any:
    from mediapipe.tasks.python import BaseOptions, vision  # heavy import, only in worker-ingest

    options = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path()),
        running_mode=vision.RunningMode.IMAGE,
        num_faces=1,
        output_face_blendshapes=True,
        min_face_detection_confidence=0.5,
    )
    return vision.FaceLandmarker.create_from_options(options)


def head_region(box: Box, w: int, h: int) -> tuple[int, int, int, int] | None:
    """Pixel square around the head of a person box (its top part), or None when it is too small."""
    bw, bh = (box.x2 - box.x1) * w, (box.y2 - box.y1) * h
    side = min(bw, bh * 0.55, max(bw * 0.8, bh * 0.3))
    if side < MIN_HEAD_PX:
        return None
    cx = (box.x1 + box.x2) / 2 * w
    top = box.y1 * h - side * 0.1
    x1, y1 = max(0, round(cx - side * 0.6)), max(0, round(top))
    x2, y2 = min(w, round(cx + side * 0.6)), min(h, round(top + side * 1.2))
    return (x1, y1, x2, y2) if x2 - x1 >= MIN_HEAD_PX and y2 - y1 >= MIN_HEAD_PX else None


def mouth_open(rgb: Any, region: tuple[int, int, int, int]) -> float | None:
    """MediaPipe jawOpen (0 closed .. 1 wide open) of the face in the region, or None if no face is found."""
    import cv2
    import mediapipe as mp

    x1, y1, x2, y2 = region
    crop = rgb[y1:y2, x1:x2]
    scale = HEAD_SIDE / max(crop.shape[0], crop.shape[1])
    crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    result = _landmarker().detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(crop)))
    if not result.face_blendshapes:
        return None
    return next((c.score for c in result.face_blendshapes[0] if c.category_name == "jawOpen"), None)


def loudness(pcm: Any, t: float) -> float:
    """RMS of the 16 kHz mono samples around t + SOUND_LAG_S (one lip sample either side)."""
    centre = t + SOUND_LAG_S
    half = 1.0 / LIP_FPS
    chunk = pcm[max(0, int((centre - half) * AUDIO_RATE)) : int((centre + half) * AUDIO_RATE)]
    return round(float(np.sqrt(np.mean(np.square(chunk)))), 5) if len(chunk) else 0.0


def measure_lips(
    src: str,
    width: int,
    height: int,
    duration_s: float,
    detections: Detections,
    pcm: Any,
    *,
    on_progress: Callable[[float], None] | None = None,
    check: Callable[[], None] | None = None,
    register: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> list[LipFrame]:
    """Mouth openness of every readable person, 12 times a second, with the audio loudness (pcm: 16 kHz mono)."""
    args, w, h = detect_frames(src, width, height, LIP_FPS, min(LIP_SHORT_SIDE, width, height))
    frame_bytes = w * h * 3
    det_times = [f.t for f in detections.frames]
    out: list[LipFrame] = []
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if register is not None:
        register(proc)
    assert proc.stdout is not None
    n = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            if check is not None:
                check()
            t = round(n / LIP_FPS, 3)
            n += 1
            if on_progress is not None and duration_s > 0 and n % LIP_FPS == 0:
                on_progress(min(1.0, t / duration_s))
            i = bisect_left(det_times, t)
            near = [j for j in (i - 1, i) if 0 <= j < len(det_times)]
            if not near:
                continue
            j = min(near, key=lambda k: abs(det_times[k] - t))
            if abs(det_times[j] - t) > MAX_BOX_GAP_S or not detections.frames[j].boxes:
                continue
            rgb = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 3))[:, :, ::-1]  # ffmpeg gives BGR
            mouth: dict[int, float] = {}
            for box in detections.frames[j].boxes:
                region = head_region(box, w, h)
                value = mouth_open(rgb, region) if region else None
                if value is not None:
                    mouth[box.track_id] = round(float(value), 4)
            if mouth:
                out.append(LipFrame(t=t, loud=loudness(pcm, t), mouth=mouth))
    finally:
        _, err = proc.communicate()
    if proc.returncode not in (0, None) and n == 0:
        raise MediaError("frame decode for lip reading failed", err.decode(errors="replace")[-2000:])
    return out
