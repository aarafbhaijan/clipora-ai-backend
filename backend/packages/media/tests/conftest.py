import os
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def golden():  # type: ignore[no-untyped-def]
    """Compare text with a golden file. UPDATE_GOLDEN=1 rewrites it."""

    def check(name: str, text: str) -> None:
        path = GOLDEN / name
        if os.environ.get("UPDATE_GOLDEN") == "1" or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            if os.environ.get("UPDATE_GOLDEN") != "1":
                pytest.fail(f"golden file {name} was missing and has been written; review and re-run")
        assert text == path.read_text(), f"{name} changed; run with UPDATE_GOLDEN=1 if intended"

    return check
