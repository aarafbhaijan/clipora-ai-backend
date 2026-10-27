"""Intent routing, transcript fitting and citations for streamed chat."""

from agent.conversation import citations, clean, rule_route, transcript_context
from core.transcript import Cue, Transcript


def intent(text: str) -> str | None:
    r = rule_route(text)
    return r.intent if r else None


def test_edit_requests_route_to_editing() -> None:
    for text in (
        "can you convert this in a short video",
        "make it shorter",
        "Can you make it square?",
        "remove the second clip",
        "make a 30 second vertical short about pricing",
        "use a fade between all clips",
        "please add captions",
    ):
        assert intent(text) == "edit", text


def test_questions_route_to_answers() -> None:
    for text in (
        "Can you tell me what is happening in the video",
        "what is this video about?",
        "Who said that pricing is hard?",
        "summarize the video",
        "What clips did you choose?",
        "where does he talk about failure?",
    ):
        assert intent(text) == "question", text


def test_small_talk_routes_to_chat() -> None:
    for text in ("hi", "Hello!", "thanks", "thank you!", "what can you do?", "ok"):
        assert intent(text) == "chat", text


def test_ambiguous_wording_is_left_to_the_model() -> None:
    assert rule_route("the intro is boring") is None


def transcript(n: int) -> Transcript:
    cues = [Cue(id=f"c{i}", start=i * 5.0, end=i * 5.0 + 4, text=f"line number {i}") for i in range(1, n + 1)]
    return Transcript(cues=cues)


def test_transcript_context_keeps_whole_lines_and_marks_the_cut() -> None:
    full, cut = transcript_context(transcript(5), 10_000)
    assert not cut and full.count("\n") == 4
    short, cut = transcript_context(transcript(200), 2_000)
    assert cut and "[... middle of the transcript omitted ...]" in short and len(short) <= 2_100
    assert short.startswith("[c1 ") and short.rstrip().endswith("line number 200")


def test_citations_keep_known_cues_in_order_and_clean_removes_markers() -> None:
    tr = transcript(20)
    text = "He opens with a story [c2][c3]. Later he talks about failure [c15] and repeats it [c2]. Bad [c99]."
    cites = citations(text, tr)
    assert [c["cue_id"] for c in cites] == ["c2", "c3", "c15"]
    assert cites[0]["start_s"] == 10.0
    assert clean(text) == "He opens with a story. Later he talks about failure and repeats it. Bad."


def test_parenthesised_citations_count_too() -> None:
    tr = transcript(5)
    text = "It is about pricing and hiring.\n\n(c1), (c2), (c3), and (c4)"
    assert [c["cue_id"] for c in citations(text, tr)] == ["c1", "c2", "c3", "c4"]
    assert clean(text) == "It is about pricing and hiring."


def test_clean_keeps_markdown_intact() -> None:
    md = "| Time | What happens |\n| --- | --- |\n| 0:00 | Intro [c1] |\n\n```python\nx = f(a) ;\n```"
    out = clean(md)
    assert "| 0:00 | Intro |" in out
    assert "x = f(a) ;" in out  # code inside a fenced block is untouched


def test_copied_transcript_labels_become_times_and_citations() -> None:
    tr = transcript(5)
    md = "| Time | What happens |\n| --- | --- |\n| [c1 00:00.00-00:05.16] | Intro |\n| [c2 01:05.16-01:10.76] | More |"
    assert [c["cue_id"] for c in citations(md, tr)] == ["c1", "c2"]
    out = clean(md)
    assert "| 0:00–0:05 | Intro |" in out and "| 1:05–1:10 | More |" in out


def test_speaker_requests_route_to_the_speaker_overlay() -> None:
    for text in (
        "identify the speaker",
        "can you identify the person who is speaking?",
        "who is speaking",
        "show me the speaker box",
        "detect the people in this video",
        "who is speaking at 1:20?",
        "who talks when in this video",
    ):
        assert intent(text) == "speaker", text
    assert intent("make it shorter") == "edit"


def test_transcript_requests_are_answered_from_the_transcript() -> None:
    for text in (
        "show the transcription of the video",
        "give me the full transcript",
        "transcript",
        "can you give me the list of speakers along with their spoken speeches",
        "who said what?",
        "show each person and what they said",
    ):
        assert intent(text) == "transcript", text
    # Questions about the content still go to the model.
    assert intent("can you list the important content from the transcription") != "transcript"
    assert intent("have you read the transcription?") == "question"
    assert intent("who is speaking at 1:20?") == "speaker"
