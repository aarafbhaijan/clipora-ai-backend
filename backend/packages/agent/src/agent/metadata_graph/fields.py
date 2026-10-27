"""Field ops for metadata revisions. The LLM returns ops; code applies them (rules.md hard rule 3)."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from agent.metadata_graph.platforms import PLATFORMS
from core.metadata import MetadataFields, PlatformPick

FieldName = Literal["title", "thumbnail_title", "description", "hashtags", "domain", "subtopic"]
PLATFORM_BOUND: tuple[FieldName, ...] = ("title", "description", "hashtags")


class FieldOp(BaseModel):
    op: Literal["set_field", "regenerate_field", "set_platform", "set_tone", "set_language"]
    field: FieldName | None = None
    value: str | None = Field(default=None, description="New text for set_field; hashtags space-separated")
    instruction: str | None = Field(default=None, max_length=300)
    platform: str | None = None
    tone: Literal["neutral", "casual", "punchy", "professional"] | None = None
    language: str | None = None


class FieldOpsOut(BaseModel):
    ops: list[FieldOp] = Field(max_length=10)


@dataclass
class FieldResult:
    fields: MetadataFields
    platform: str
    tone: str | None
    language: str | None
    regenerate: dict[str, str] = field(default_factory=dict)  # field -> instruction
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _hashtags(value: str) -> list[str]:
    tags = [t if t.startswith("#") else f"#{t}" for t in value.replace(",", " ").split() if t.strip("#")]
    return list(dict.fromkeys(tags))


def apply_field_ops(
    fields: MetadataFields, platform: str, tone: str | None, language: str | None, ops: list[FieldOp]
) -> FieldResult:
    r = FieldResult(fields=fields.model_copy(deep=True), platform=platform, tone=tone, language=language)
    for op in ops:
        if op.op == "set_field":
            if op.field is None or op.value is None:
                r.skipped.append("A field change was missing its text.")
                continue
            value: object = _hashtags(op.value) if op.field == "hashtags" else op.value.strip()
            r.fields = r.fields.model_copy(update={op.field: value})
            r.applied.append(f"Changed the {op.field.replace('_', ' ')}.")
        elif op.op == "regenerate_field":
            if op.field is None:
                r.skipped.append("I did not know which field to rewrite.")
                continue
            r.regenerate[op.field] = op.instruction or ""
        elif op.op == "set_platform":
            if op.platform not in PLATFORMS:
                r.skipped.append(f"I do not know the platform {op.platform}.")
                continue
            r.platform = op.platform
            r.fields = r.fields.model_copy(
                update={"platform": PlatformPick(platform=op.platform, reason="Your choice")}
            )
            for f in PLATFORM_BOUND:  # changing the platform rewrites the platform-bound fields (FR-25)
                r.regenerate.setdefault(f, "")
            r.applied.append(f"Switched to {PLATFORMS[op.platform].label}.")
        elif op.op == "set_tone" and op.tone:
            r.tone = op.tone
            for f in ("title", "description"):
                r.regenerate.setdefault(f, "")
            r.applied.append(f"Set the tone to {op.tone}.")
        elif op.op == "set_language" and op.language:
            r.language = op.language
            for f in ("title", "thumbnail_title", "description", "hashtags"):
                r.regenerate.setdefault(f, "")
            r.applied.append(f"Set the language to {op.language}.")
        else:
            r.skipped.append("I did not understand one of the changes.")
    return r
