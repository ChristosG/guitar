"""`brain/crawl.py` — the scoped same-site crawler behind multi-page URL
sources (2026-07-21). No network: `safe_fetch_html` is replaced with a fake
site, so these tests pin the SCOPING rules — same host only, BFS order,
fragment-dedup, junk-path skipping, the page budget — not trafilatura.
"""
import pytest

import app.brain.crawl as crawl_mod
from app.brain.crawl import crawl_site


SITE = {
    "https://guides.example.com/course": (
        "<title>The Course</title>"
        '<a href="/course/lesson-1">L1</a>'
        '<a href="/course/lesson-2#top">L2</a>'
        '<a href="/login">login</a>'
        '<a href="/style.css">css</a>'
        '<a href="https://other-site.com/steal">other</a>'
        "<p>Welcome to the course index.</p>"
    ),
    "https://guides.example.com/course/lesson-1": (
        "<title>Lesson 1</title><p>Tune the guitar before anything else.</p>"
        '<a href="/course/lesson-2">next</a>'
    ),
    "https://guides.example.com/course/lesson-2": (
        "<title>Lesson 2</title><p>Now learn the E minor chord properly.</p>"
    ),
}


@pytest.fixture(autouse=True)
def _fake_site(monkeypatch):
    def fake_fetch(url, **kwargs):
        if url in SITE:
            return SITE[url]
        raise ValueError(f"404 {url}")

    monkeypatch.setattr(crawl_mod, "safe_fetch_html", fake_fetch)
    # The extractor is trafilatura's business; here a trivial stand-in that
    # returns the body text keeps the test about CRAWLING.
    import app.brain.extract as extract_mod

    monkeypatch.setattr(extract_mod, "_trafilatura_extract", lambda html, url: None)


def test_bfs_same_host_dedup_and_junk_filtering():
    pages = crawl_site("https://guides.example.com/course", max_pages=10)

    urls = [u for u, _, _ in pages]
    # BFS: the index first, then its lessons in link order; lesson-2 reached
    # once despite the #fragment variant and the second link from lesson-1.
    assert urls == [
        "https://guides.example.com/course",
        "https://guides.example.com/course/lesson-1",
        "https://guides.example.com/course/lesson-2",
    ]
    # login/css/other-host were never fetched (fake_fetch would have 404'd
    # loudly into a skip, but more to the point they are not in the output).
    titles = [t for _, t, _ in pages]
    assert titles == ["The Course", "Lesson 1", "Lesson 2"]
    assert "E minor" in pages[2][2]


def test_the_page_budget_caps_fetches():
    pages = crawl_site("https://guides.example.com/course", max_pages=2)
    assert [u for u, _, _ in pages] == [
        "https://guides.example.com/course",
        "https://guides.example.com/course/lesson-1",
    ]


def test_an_unfetchable_seed_returns_empty_not_raises():
    assert crawl_site("https://guides.example.com/missing", max_pages=5) == []
