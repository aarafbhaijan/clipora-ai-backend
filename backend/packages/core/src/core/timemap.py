"""Source time and output time. Convert only through `src_to_out` and `out_to_src`.

The frontend has a TypeScript port of these functions; both run the vectors in `core/vectors/timemap.json`.
"""

from dataclasses import dataclass

from core.edl import EDL


@dataclass(frozen=True)
class SegmentTiming:
    index: int
    id: str
    t_out_start: float
    t_out_end: float
    src_start: float
    src_end: float


def boundary_durations(edl: EDL) -> list[float]:
    return [t.effective_duration for t in edl.boundaries()]


def segment_timings(edl: EDL) -> list[SegmentTiming]:
    """Output range of each segment. Segment k starts at sum(len_0..len_k-1) - sum(d_0..d_k-1)."""
    durations = boundary_durations(edl)
    timings: list[SegmentTiming] = []
    t = 0.0
    for i, seg in enumerate(edl.segments):
        timings.append(SegmentTiming(i, seg.id, t, t + seg.length, seg.src_start, seg.src_end))
        if i < len(durations):
            t += seg.length - durations[i]
    return timings


def output_duration(edl: EDL) -> float:
    return sum(s.length for s in edl.segments) - sum(boundary_durations(edl))


def xfade_offsets(edl: EDL) -> list[float]:
    """FFmpeg xfade offset for each boundary: offset_k = (len_1 + ... + len_k) - (d_1 + ... + d_k)."""
    timings = segment_timings(edl)
    return [timings[i + 1].t_out_start for i in range(len(timings) - 1)]


def src_to_out(edl: EDL, src_time: float) -> float | None:
    """Output time of a source time, from the first segment that contains it. None if it is not in the edit."""
    for st in segment_timings(edl):
        if st.src_start <= src_time < st.src_end:
            return st.t_out_start + (src_time - st.src_start)
    return None


def out_to_src(edl: EDL, t_out: float) -> list[tuple[int, float]]:
    """(segment index, source time) for every segment playing at t_out: two inside a transition overlap."""
    hits = [
        (st.index, st.src_start + (t_out - st.t_out_start))
        for st in segment_timings(edl)
        if st.t_out_start <= t_out < st.t_out_end
    ]
    return hits
