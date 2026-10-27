import gzip
import struct

from core.transcript import Word
from media.cues import group_words, parse_srt, wrap
from media.probe import parse_probe
from media.waveform import BUCKET, buckets


def w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end)


def test_group_words_breaks_on_sentence_pause_and_length() -> None:
    words = [w("Hello", 0, 0.4), w("world.", 0.5, 0.9), w("Next", 1.0, 1.3), w("bit", 1.4, 1.6), w("after", 2.5, 2.8)]
    cues = group_words(words)
    assert [c.text for c in cues] == ["Hello world.", "Next bit", "after"]
    assert [c.id for c in cues] == ["c1", "c2", "c3"]
    long = [w("word", i * 0.3, i * 0.3 + 0.25) for i in range(40)]
    for c in group_words(long):
        assert len(c.text) <= 84 and wrap(c.text)
        assert c.end - c.start <= 7.0


def test_wrap_balances_two_lines() -> None:
    text = "the quick brown fox jumps over the lazy dog and keeps on running far"
    lines = wrap(text)
    assert len(lines) == 2 and all(len(x) <= 42 for x in lines)
    assert wrap("x" * 50) == []


def test_parse_srt() -> None:
    text = "﻿2\n00:00:05,000 --> 00:00:06,500\n<i>Second</i>\n\n1\n00:00:01,000 --> 00:00:02,000\nFirst\nline\n"
    cues = parse_srt(text)
    assert [(c.id, c.start, c.end, c.text) for c in cues] == [
        ("c1", 1.0, 2.0, "First line"),
        ("c2", 5.0, 6.5, "Second"),
    ]


def test_parse_probe_rotation_and_cover() -> None:
    data = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "video", "width": 300, "height": 300, "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
             "side_data_list": [{"rotation": -90}]},
            {"codec_type": "audio"},
        ],
    }  # fmt: skip
    r = parse_probe(data)
    assert (r.width, r.height, r.fps, r.duration_s, r.has_audio) == (1080, 1920, 29.97, 12.5, True)
    assert not parse_probe({"streams": [{"codec_type": "audio"}]}).has_video


def test_waveform_buckets() -> None:
    samples = [0] * BUCKET + [32767, -32768] + [0] * (BUCKET - 2) + [100] * 10
    raw = struct.pack(f"<{len(samples)}h", *samples)
    out = buckets([raw[:777], raw[777:]])
    pairs = list(struct.unpack(f"{len(out)}b", out))
    assert pairs == [0, 0, -128, 127, 0, 0]
    assert gzip.decompress(gzip.compress(out)) == out
