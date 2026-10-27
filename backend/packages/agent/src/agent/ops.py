"""Apply edit ops to a plan. The LLM returns ops; this code changes the EDL (rules.md hard rule 3).

Each op either applies or is skipped with a plain reason, so the reply can say what changed and
what could not be done. New times are snapped to word boundaries; ranges that do not snap are skipped.
"""

from dataclasses import dataclass, field

from agent.snapping import SNAP_WITHIN_S, snap_range
from core.aspect import MAX_SIDE, MIN_SIDE, output_size, parse_aspect
from core.edl import EDL, Segment, Transition, TransitionOverride
from core.ops import AddOp, EditOp, RemoveOp, ReorderOp, SetAspectOp, SetTargetOp, SetTransitionOp, TrimOp
from core.transcript import snap
from core.transitions import TransitionType


@dataclass
class ApplyResult:
    edl: EDL
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rerun_selection: bool = False  # a set_target op asks select_and_order to run again


def _next_id(segments: list[Segment]) -> str:
    nums = [int(s.id[1:]) for s in segments if s.id[1:].isdigit()]
    return f"s{max(nums, default=0) + 1}"


def _fmt(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


def apply_ops(edl: EDL, ops: list[EditOp], boundaries: list[float]) -> ApplyResult:
    result = ApplyResult(edl=edl.model_copy(deep=True))
    for op in ops:
        reason = _apply(result, op, boundaries)
        if reason:
            result.skipped.append(reason)
    return result


def _apply(r: ApplyResult, op: EditOp, boundaries: list[float]) -> str | None:  # noqa: C901
    e = r.edl
    segs = list(e.segments)
    ids = [s.id for s in segs]

    if isinstance(op, RemoveOp):
        if op.id not in ids:
            return f"There is no clip {op.id} to remove."
        if len(segs) == 1:
            return "I kept the last clip: a short needs at least one."
        r.edl = e.model_copy(
            update={
                "segments": [s for s in segs if s.id != op.id],
                "transitions": e.transitions.model_copy(
                    update={"overrides": [o for o in e.transitions.overrides if op.id not in (o.from_, o.to)]}
                ),
            }
        )
        r.applied.append(f"Removed clip {op.id}.")
        return None

    if isinstance(op, TrimOp):
        if op.id not in ids:
            return f"There is no clip {op.id} to trim."
        seg = segs[ids.index(op.id)]
        start, end = seg.src_start, seg.src_end
        if op.src_start is not None:
            s = snap(op.src_start, boundaries, SNAP_WITHIN_S)
            if s is None:
                return f"I could not find a word boundary near {_fmt(op.src_start)} for {op.id}."
            start = s
        if op.src_end is not None:
            t = snap(op.src_end, boundaries, SNAP_WITHIN_S)
            if t is None:
                return f"I could not find a word boundary near {_fmt(op.src_end)} for {op.id}."
            end = t
        if end <= start:
            return f"That trim would leave nothing of {op.id}."
        segs[ids.index(op.id)] = seg.model_copy(update={"src_start": start, "src_end": end, "crop": []})
        r.edl = e.model_copy(update={"segments": segs})
        r.applied.append(f"Trimmed {op.id} to {_fmt(start)}-{_fmt(end)}.")
        return None

    if isinstance(op, AddOp):
        snapped = snap_range(op.src_start, op.src_end, boundaries)
        if snapped is None:
            return f"I could not line up {_fmt(op.src_start)}-{_fmt(op.src_end)} with the words, so I did not add it."
        if op.after_id is not None and op.after_id not in ids:
            return f"There is no clip {op.after_id} to add after."
        new = Segment(id=_next_id(segs), src_start=snapped.start, src_end=snapped.end, reason="Added on request")
        at = 0 if op.after_id is None else ids.index(op.after_id) + 1
        segs.insert(at, new)
        r.edl = e.model_copy(update={"segments": segs})
        r.applied.append(f"Added {_fmt(snapped.start)}-{_fmt(snapped.end)} as {new.id}.")
        return None

    if isinstance(op, ReorderOp):
        if sorted(op.ids) != sorted(ids):
            return "The new order must list every clip exactly once."
        by_id = {s.id: s for s in segs}
        adjacent = {(op.ids[i], op.ids[i + 1]) for i in range(len(op.ids) - 1)}
        r.edl = e.model_copy(
            update={
                "segments": [by_id[i] for i in op.ids],
                "transitions": e.transitions.model_copy(
                    update={"overrides": [o for o in e.transitions.overrides if (o.from_, o.to) in adjacent]}
                ),
            }
        )
        r.applied.append("Changed the clip order to " + ", ".join(op.ids) + ".")
        return None

    if isinstance(op, SetTargetOp):
        if not 3 <= op.seconds <= 180:
            return "A short must be 3 to 180 seconds long."
        r.edl = e.model_copy(update={"output": e.output.model_copy(update={"target_duration_s": op.seconds})})
        r.rerun_selection = True
        r.applied.append(f"Set the target length to {op.seconds:g} seconds.")
        return None

    if isinstance(op, SetTransitionOp):
        if op.type != TransitionType.cut and not 0.1 <= op.duration_s <= 2.0:
            return "Transitions must be 0.1 to 2 seconds."
        if op.all:
            r.edl = e.model_copy(
                update={
                    "transitions": e.transitions.model_copy(
                        update={"default": Transition(type=op.type, duration_s=op.duration_s), "overrides": []}
                    )
                }
            )
            r.applied.append(f"Used {op.type.value.replace('_', ' ')} between every clip.")
            return None
        pairs = {(ids[i], ids[i + 1]) for i in range(len(ids) - 1)}
        if (op.from_, op.to) not in pairs:
            return f"{op.from_} and {op.to} are not next to each other."
        assert op.from_ is not None and op.to is not None
        overrides = [o for o in e.transitions.overrides if (o.from_, o.to) != (op.from_, op.to)]
        overrides.append(
            TransitionOverride.model_validate(
                {"from": op.from_, "to": op.to, "type": op.type, "duration_s": op.duration_s}
            )
        )
        r.edl = e.model_copy(update={"transitions": e.transitions.model_copy(update={"overrides": overrides})})
        r.applied.append(f"Used {op.type.value.replace('_', ' ')} between {op.from_} and {op.to}.")
        return None

    if isinstance(op, SetAspectOp):
        out = e.output
        if op.width is not None or op.height is not None:
            if op.width is None or op.height is None:
                return "A custom size needs both width and height."
            if op.width % 2 or op.height % 2 or not all(MIN_SIDE <= v <= MAX_SIDE for v in (op.width, op.height)):
                return f"Width and height must be even and {MIN_SIDE} to {MAX_SIDE}."
            out = out.model_copy(update={"width": op.width, "height": op.height, "aspect": f"{op.width}:{op.height}"})
        elif op.aspect is not None:
            try:
                parse_aspect(op.aspect)
            except ValueError:
                return f"{op.aspect} is not an aspect ratio like 9:16."
            w, h = output_size(op.aspect)
            out = out.model_copy(update={"aspect": op.aspect, "width": w, "height": h})
        if op.framing is not None:
            out = out.model_copy(update={"framing": op.framing})
        r.edl = e.model_copy(update={"output": out})
        r.applied.append(f"Changed the output to {out.aspect} ({out.framing.value}).")
        return None

    return "I did not understand one of the changes."
