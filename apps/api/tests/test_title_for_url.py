"""Unit tests for `app.routers.knowledge._title_for_url` — pure string logic,
no DB, no embed server, deliberately NOT marked integration (unlike the rest
of `test_knowledge_router.py`, which is DB+embed-heavy end to end).

Plan 12 follow-up (cheap, related to the domain-filter bug fix): `POST
/knowledge/sources/bulk`'s auto-derived title used to be the raw
`netloc + path`, e.g. "en.wikipedia.org/wiki/Humbucker" — indistinguishable
from a URL/server-log line, and confusing side by side with the 3 dead
pre-Plan-9 Wikipedia rows in the sources list. `_title_for_url` now derives a
human-legible title from the URL's last path segment (decoded, `-`/`_` ->
space, title-cased only when the raw slug was all-lowercase so it doesn't
mangle a source that already has meaningful casing, e.g. a wiki article's own
proper-noun title) plus the domain for context.

Scope note: this only changes what NEW bulk-ingested sources are titled going
forward — it does not touch/rename any existing row (that's Chris's decision,
per this task's brief).
"""
from app.routers.knowledge import _title_for_url


def test_wiki_style_path_becomes_readable_with_domain_context():
    # The exact case from the bug report: raw title used to be
    # "en.wikipedia.org/wiki/Humbucker" — a URL, not a library entry.
    title = _title_for_url("https://en.wikipedia.org/wiki/Humbucker")
    assert title == "Humbucker — en.wikipedia.org"


def test_wiki_underscored_multiword_title_becomes_spaced_and_keeps_its_casing():
    title = _title_for_url("https://en.wikipedia.org/wiki/Guitar_amplifier")
    # Wikipedia's own casing (only the first word capitalized) is preserved —
    # it is NOT force-title-cased into "Guitar Amplifier" or lower-cased.
    assert title == "Guitar amplifier — en.wikipedia.org"


def test_all_lowercase_hyphenated_slug_is_title_cased_for_readability():
    title = _title_for_url("https://example.com/blog/guitar-amplifier-basics")
    assert title == "Guitar Amplifier Basics — example.com"


def test_percent_encoded_segment_is_decoded():
    title = _title_for_url("https://example.com/wiki/Guitar%20Amplifier")
    assert title == "Guitar Amplifier — example.com"


def test_url_with_no_path_falls_back_to_domain_only():
    assert _title_for_url("https://example.com/") == "example.com"
    assert _title_for_url("https://example.com") == "example.com"


def test_title_is_capped_at_400_chars():
    long_slug = "x" * 500
    title = _title_for_url(f"https://example.com/{long_slug}")
    assert len(title) <= 400
