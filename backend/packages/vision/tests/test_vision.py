import os
import subprocess
from types import SimpleNamespace

import pytest

from vision import boxes_from_result


class T(list):  # stands in for a torch tensor: only .tolist() is used
    def tolist(self) -> list:  # type: ignore[type-arg]
        return list(self)


def test_boxes_from_result_keeps_tracked_people() -> None:
    boxes = SimpleNamespace(
        id=T([3, 4]),
        xyxyn=T([[0.1, 0.2, 0.3, 0.9], [0.5, 0.5, 0.6, 0.6]]),
        conf=T([0.9, 0.5]),
        cls=T([0, 2]),
    )
    out = boxes_from_result(SimpleNamespace(boxes=boxes))
    assert [(b.track_id, b.x1, b.y2, b.conf) for b in out] == [(3, 0.1, 0.9, 0.9)]
    assert boxes_from_result(SimpleNamespace(boxes=SimpleNamespace(id=None))) == []


@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="downloads yolov8n.pt; set RUN_SLOW=1")
def test_detect_runs_on_synthetic_clip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from vision import detect_people

    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-c:v", "libx264", str(src)],
        check=True,
    )  # fmt: skip
    det = detect_people(str(src), 640, 360, 2.0)
    assert det.fps == 3 and len(det.frames) == 6
