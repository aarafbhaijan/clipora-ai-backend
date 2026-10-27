import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused@localhost/unused")

from api.schemas import SpeakerOut, SpeakerTurnOut  # noqa: E402
from chat.turn import asked_time, speaker_reply  # noqa: E402


def _spk(**kw: object) -> SpeakerOut:
    base: dict[str, object] = {"available": True, "main_track_id": 1, "coverage": 0.8, "people": 2, "analysed": True}
    return SpeakerOut.model_validate(base | kw)


TURNS = [
    SpeakerTurnOut(start=0, end=30, track_id=1, label="Person 1", confidence=0.8),
    SpeakerTurnOut(start=31, end=95, track_id=2, label="Person 2", confidence=0.7),
]


def test_asked_time() -> None:
    assert asked_time("who is speaking at 1:20?") == 80
    assert asked_time("who talks around 45 sec") == 45
    assert asked_time("who is speaking") is None


def test_reply_names_the_talker_at_a_moment() -> None:
    text = speaker_reply(_spk(turns=TURNS), "who is speaking at 1:20", True)
    assert "**Person 2** is talking" in text and "0:31–1:35" in text
    assert "can't see anyone" in speaker_reply(_spk(turns=TURNS), "who is speaking at 2:00", True)


def test_reply_lists_turns_or_falls_back_to_the_main_person() -> None:
    assert "| 0:00–0:30 | Person 1 |" in speaker_reply(_spk(turns=TURNS), "who is speaking", True)
    fallback = speaker_reply(_spk(), "who is speaking", True)
    assert "couldn't match anyone's lips" in fallback and "green box" in fallback
    assert "couldn't match" not in speaker_reply(_spk(analysed=False), "who is speaking", True)


def _transcript() -> Any:
    from core.transcript import Cue, Transcript

    return Transcript(
        cues=[
            Cue(id="c1", start=1, end=4, text="Welcome to the show."),
            Cue(id="c2", start=5, end=9, text="Great to have you."),
            Cue(id="c3", start=35, end=40, text="Thanks, happy to be here."),
            Cue(id="c4", start=100, end=104, text="And that's the news."),
        ]
    )


def test_transcript_reply_groups_lines_by_speaker() -> None:
    from chat.turn import transcript_reply

    text = transcript_reply(_transcript(), _spk(turns=TURNS), True)
    assert "**Person 1** · 0:01–0:09\nWelcome to the show. Great to have you." in text
    assert "**Person 2** · 0:35–0:40\nThanks, happy to be here." in text
    assert "**Not clear** · 1:40–1:44" in text  # after the last turn: nobody on screen said it
    assert "Transcript** tab" in text


def test_transcript_reply_without_speakers_uses_time_paragraphs() -> None:
    from chat.turn import transcript_reply

    text = transcript_reply(_transcript(), _spk(analysed=False), True)
    assert "can't tell the speakers apart" in text
    assert "**0:01–0:09**\nWelcome to the show. Great to have you." in text
    assert "**0:35–0:40**" in text
    assert transcript_reply(_transcript(), None, False).startswith("**Transcript**")


def test_request_limit_is_read_from_a_413() -> None:
    from chat.turn import request_limit

    class TooLarge(Exception):
        status_code = 413

    class Busy(Exception):
        status_code = 429

    msg = "Error code: 413 - tokens per minute (TPM): Limit 8000, Requested 10349"
    assert request_limit(TooLarge(msg)) == 8000
    assert request_limit(Busy(msg)) is None
    assert request_limit(ValueError(msg)) is None
