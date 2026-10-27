"""Platforms, their text limits and the domain list for posting metadata (architecture.md 11.3).

Every limit is None until it is checked against each platform's current rules (architecture.md
"Not verified": platform limits). A None limit is not enforced. Do not fill these from memory.
The domain list is a draft for Arav to confirm (open question 7 in prd.md).
"""

from dataclasses import dataclass

from core.metadata import MetadataFields


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    title_max_chars: int | None = None
    description_max_chars: int | None = None
    hashtags_max: int | None = None


PLATFORMS: dict[str, Platform] = {
    p.key: p
    for p in (
        Platform("youtube_shorts", "YouTube Shorts"),
        Platform("tiktok", "TikTok"),
        Platform("instagram_reels", "Instagram Reels"),
        Platform("facebook_reels", "Facebook Reels"),
        Platform("linkedin", "LinkedIn"),
        Platform("x", "X"),
        Platform("youtube", "YouTube"),
    )
}

# Draft list, not confirmed.
DOMAINS: tuple[str, ...] = (
    "business",
    "marketing",
    "sales",
    "entrepreneurship",
    "finance",
    "investing",
    "careers",
    "leadership",
    "technology",
    "software",
    "ai",
    "science",
    "health",
    "fitness",
    "nutrition",
    "mental health",
    "education",
    "language learning",
    "parenting",
    "relationships",
    "lifestyle",
    "travel",
    "food",
    "fashion",
    "beauty",
    "gaming",
    "sports",
    "music",
    "film and tv",
    "news and politics",
    "comedy",
    "personal development",
    "religion and spirituality",
    "home and diy",
)


def limit_issues(fields: MetadataFields, platform_key: str) -> list[str]:
    p = PLATFORMS.get(platform_key)
    if p is None:
        return [f"unknown platform {platform_key}"]
    issues: list[str] = []
    if p.title_max_chars is not None and len(fields.title) > p.title_max_chars:
        issues.append(f"title is longer than {p.title_max_chars} characters for {p.label}")
    if p.description_max_chars is not None and len(fields.description) > p.description_max_chars:
        issues.append(f"description is longer than {p.description_max_chars} characters for {p.label}")
    if p.hashtags_max is not None and len(fields.hashtags) > p.hashtags_max:
        issues.append(f"{p.label} allows at most {p.hashtags_max} hashtags")
    return issues
