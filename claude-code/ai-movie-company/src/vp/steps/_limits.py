"""YouTube metadata limits (SPEC S6/S9) — pure functions, shared by S6b and S9.

  title        1..100 characters
  description  ≤ 5000 characters
  tags         ≤ 500 characters in total, counted the way YouTube counts them: tag lengths
               + a comma between tags + 2 quote characters for a tag that contains a space
  forbidden    "<" and ">" anywhere in title, description, tags

Every check returns a list of violation codes (empty = ok), so S9 can put them into review.yaml.
"""
from __future__ import annotations

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500
TAG_MAX = 100
HASHTAGS_MAX = 15
FORBIDDEN = ("<", ">")


def has_forbidden(text: str) -> bool:
    """True if the text contains a character YouTube rejects in metadata (< or >)."""
    return any(ch in (text or "") for ch in FORBIDDEN)


def title_violations(title: object) -> list[str]:
    """Violation codes for a video title."""
    if not isinstance(title, str) or not title.strip():
        return ["title_empty"]
    out = []
    if len(title) > TITLE_MAX:
        out.append("title_too_long")
    if has_forbidden(title):
        out.append("title_forbidden_chars")
    if "\n" in title:
        out.append("title_newline")
    return out


def description_violations(description: object, max_len: int = DESCRIPTION_MAX) -> list[str]:
    """Violation codes for a description (`max_len` lets S6b keep a reserve for the links block)."""
    if not isinstance(description, str):
        return ["description_not_text"]
    out = []
    if len(description) > max_len:
        out.append("description_too_long")
    if has_forbidden(description):
        out.append("description_forbidden_chars")
    return out


def tag_cost(tag: str) -> int:
    """Characters a tag takes from the 500 budget (quoted when it contains a space)."""
    return len(tag) + (2 if " " in tag else 0)


def tags_length(tags: list[str]) -> int:
    """Total length of a tag list as YouTube counts it (tags + separating commas)."""
    if not tags:
        return 0
    return sum(tag_cost(t) for t in tags) + (len(tags) - 1)


def tags_violations(tags: object, max_total: int = TAGS_TOTAL_MAX) -> list[str]:
    """Violation codes for a tag list."""
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return ["tags_not_list"]
    out = []
    if any(not t.strip() for t in tags):
        out.append("tag_empty")
    if any(len(t) > TAG_MAX for t in tags):
        out.append("tag_too_long")
    if any(has_forbidden(t) or "," in t for t in tags):
        out.append("tag_forbidden_chars")
    if tags_length(tags) > max_total:
        out.append("tags_too_long")
    return out


def clean_tags(tags: list[str]) -> list[str]:
    """Strip, drop empties, forbidden characters and case-insensitive duplicates (order kept)."""
    out, seen = [], set()
    for t in tags:
        if not isinstance(t, str):
            continue
        t = " ".join(t.replace(",", " ").split())
        t = t.lstrip("#")
        if not t or has_forbidden(t) or len(t) > TAG_MAX:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def fit_tags(tags: list[str], max_total: int = TAGS_TOTAL_MAX) -> list[str]:
    """Longest prefix of `tags` whose total length fits `max_total` (order = model's priority)."""
    out: list[str] = []
    for t in tags:
        if tags_length(out + [t]) > max_total:
            break
        out.append(t)
    return out


def hashtag_violations(hashtags: object) -> list[str]:
    """Hashtags: list of '#word' (no spaces), at most 15 (YouTube ignores all beyond)."""
    if not isinstance(hashtags, list) or not all(isinstance(h, str) for h in hashtags):
        return ["hashtags_not_list"]
    out = []
    if len(hashtags) > HASHTAGS_MAX:
        out.append("hashtags_too_many")
    for h in hashtags:
        if not h.startswith("#") or len(h) < 2 or any(c.isspace() for c in h) or has_forbidden(h):
            out.append("hashtag_bad_form")
            break
    return out


def normalize_hashtag(h: str) -> str | None:
    """'#AI агенты' / 'ai' -> '#AIагенты' / '#ai'; None if nothing usable remains."""
    if not isinstance(h, str):
        return None
    body = "".join(ch for ch in h.strip().lstrip("#") if ch.isalnum() or ch == "_")
    return f"#{body}" if body else None


def metadata_violations(meta: dict, description_max: int = DESCRIPTION_MAX,
                        tags_max: int = TAGS_TOTAL_MAX) -> list[str]:
    """All limit violations of {title, description, tags[, hashtags]} (S9 formal check)."""
    out = title_violations(meta.get("title"))
    out += description_violations(meta.get("description", ""), description_max)
    out += tags_violations(meta.get("tags", []), tags_max)
    if "hashtags" in meta:
        out += hashtag_violations(meta.get("hashtags"))
    return out
