"""Posting metadata fields (architecture.md 11.3). Platform limits live in `core.platforms`."""

from pydantic import BaseModel, Field

THUMBNAIL_TITLE_MAX_CHARS = 28
THUMBNAIL_TITLE_WORDS = (2, 5)
HASHTAGS = (3, 8)


class PlatformPick(BaseModel):
    platform: str
    reason: str


class MetadataFields(BaseModel):
    title: str
    title_alternates: list[str] = Field(default_factory=list, max_length=2)
    thumbnail_title: str
    thumbnail_alternates: list[str] = Field(default_factory=list, max_length=2)
    description: str
    platform: PlatformPick
    platform_alternates: list[PlatformPick] = Field(default_factory=list, max_length=2)
    hashtags: list[str]
    domain: str
    subtopic: str | None = None


def field_issues(f: MetadataFields) -> list[str]:
    """Code checks that run before the LLM critic and on every user edit."""
    issues: list[str] = []
    for label, text in [("thumbnail_title", f.thumbnail_title)] + [
        (f"thumbnail_alternates[{i}]", t) for i, t in enumerate(f.thumbnail_alternates)
    ]:
        words = len(text.split())
        if not THUMBNAIL_TITLE_WORDS[0] <= words <= THUMBNAIL_TITLE_WORDS[1]:
            issues.append(f"{label} must be 2 to 5 words")
        if len(text) > THUMBNAIL_TITLE_MAX_CHARS:
            issues.append(f"{label} must be at most {THUMBNAIL_TITLE_MAX_CHARS} characters")
    if not HASHTAGS[0] <= len(f.hashtags) <= HASHTAGS[1]:
        issues.append("hashtags must be 3 to 8")
    if any(not h.startswith("#") or " " in h for h in f.hashtags):
        issues.append("each hashtag starts with # and has no spaces")
    return issues
