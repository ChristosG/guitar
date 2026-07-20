"""Scoped same-site crawling for URL sources — "the guide is not one page".

Chris, on the library's real gap: some sites keep one course across many
linked pages (justinguitar.com's grades, licklibrary's course indexes) — a
single-URL ingest lands on the table of contents and misses the course. This
module turns ONE seed URL into an ordered list of (url, text) pages, which
`paginate_source` then persists as one multi-page source — the same shape a
PDF book already has, so the Reader, the chunker, citations and the canon all
work on a crawled site without a new branch anywhere downstream.

Scope, deliberately narrow:
  * SAME HOST only — a guide never continues on another domain, and every
    fetched URL still passes `assert_public_url` (via `safe_fetch_html`), so
    the SSRF posture of single-URL ingest is unchanged.
  * Breadth-first from the seed, so page order follows the site's own
    navigation ("lesson 1" is discovered before "lesson 12"), capped at
    `max_pages` fetches and `MAX_DEPTH` hops from the seed. BFS + the cap is
    the whole politeness story a ~30-page crawl needs; a per-request delay
    would make a 30-page crawl take a minute for no one's benefit.
  * Fragments dropped, query strings kept (paginated guides use `?page=2`),
    obvious non-content paths (login/cart/search) and non-HTML extensions
    skipped without spending a fetch on them.

Never raises for a page that fails — a broken link inside a guide is a
skipped page, not a failed source. Only an unusable SEED (nothing extracted
at all) yields an empty result, which `ingest_source` already records as
"empty" honestly.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

from app.brain.urlsafe import safe_fetch_html

log = logging.getLogger(__name__)

# Hops from the seed. Guides are shallow by design — index -> section -> page
# is 2; 3 covers a "grades" layer. Deeper than that is the rest of the site.
MAX_DEPTH = 3

# The hard ceiling a caller may ask for — `crawl_pages` clamps to it. Keeps a
# seed pointed at a huge site from turning "add a guide" into a site mirror.
MAX_CRAWL_PAGES = 50

# Path fragments that are never course content. Cheap negative filter — a
# false negative here just costs one wasted fetch, a false positive skips a
# real page, so only the unambiguous ones are listed.
_SKIP_PATH_RE = re.compile(
    r"/(login|logout|signin|signup|register|cart|checkout|account|privacy|"
    r"terms|search|tag|feed|rss|wp-admin|wp-login)([/?#.]|$)",
    re.I,
)
_SKIP_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|css|js|json|xml|pdf|zip|mp[34]|wav|ogg|"
    r"mov|avi|woff2?|ttf)$",
    re.I,
)


class _LinkParser(HTMLParser):
    """Collects href attributes of <a> tags, and the page <title>."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _normalize(base_url: str, href: str) -> str | None:
    """Resolve `href` against `base_url` into a crawlable same-host URL, or
    None when it is out of scope (other host, non-http, junk path, asset)."""
    absolute, _frag = urldefrag(urljoin(base_url, href))
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    base = urlparse(base_url)
    if parsed.netloc != base.netloc:
        return None
    if _SKIP_PATH_RE.search(parsed.path) or _SKIP_EXT_RE.search(parsed.path):
        return None
    return absolute


def crawl_site(seed_url: str, *, max_pages: int) -> list[tuple[str, str, str]]:
    """BFS from `seed_url`, same host, `MAX_DEPTH` hops, at most
    `min(max_pages, MAX_CRAWL_PAGES)` fetched pages.

    Returns ordered `(url, title, text)` triples for every page that yielded
    real extracted text. Pages that fetch but extract to nothing (a nav shell,
    a redirect stub) still contribute their LINKS — a table of contents is
    exactly such a page and is the most important one to walk through — they
    just don't become Page rows.
    """
    # Local import: extract.py imports are heavyweight (trafilatura), and
    # crawl.py must stay importable by the router without dragging them in
    # until a crawl actually runs.
    from app.brain.extract import _strip_html, _trafilatura_extract

    budget = max(1, min(max_pages, MAX_CRAWL_PAGES))
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    queue: deque[tuple[str, int]] = deque()

    seed = _normalize(seed_url, "") or seed_url
    queue.append((seed, 0))
    seen.add(seed)

    fetched = 0
    while queue and fetched < budget:
        url, depth = queue.popleft()
        try:
            html = safe_fetch_html(url)
        except ValueError:
            log.info("crawl_site: skipping unfetchable page %s", url, exc_info=True)
            continue
        fetched += 1

        parser = _LinkParser()
        try:
            parser.feed(html)
        except Exception:
            log.info("crawl_site: unparseable HTML at %s", url, exc_info=True)

        text = (_trafilatura_extract(html, url) or _strip_html(html) or "").strip()
        if text:
            out.append((url, parser.title.strip(), text))

        if depth < MAX_DEPTH:
            for href in parser.hrefs:
                nxt = _normalize(url, href)
                if nxt and nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, depth + 1))

    log.info(
        "crawl_site: seed=%s fetched=%d kept=%d (budget=%d)",
        seed_url, fetched, len(out), budget,
    )
    return out
