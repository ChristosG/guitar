"""The inline-mark grammar — the Python half.

The tutor's lesson text is stored as PLAIN STRINGS. Bold, italic and underline
live in the string itself as three markers: ``**έντονο**``, ``*πλάγιο*`` and
``<u>υπογράμμιση</u>``. Nothing else is markup: a lesson body is not Markdown
and must never be handed to a Markdown renderer, because the tutor writes ``#``,
``-``, ``1.`` and ``_`` as ordinary characters and expects to read them back.

This module is the ONLY place the grammar is written in Python. Its twin is
``apps/web/src/lib/inline-marks.ts``, and the two are pinned against each other
by ONE case table — ``tests/fixtures/inline_marks_cases.json``, whose bytes
``tests/test_inline_marks.py`` compares with the web copy. What the Word export
writes and what the word counter counts must be exactly what the tutor sees on
his screen; two hand-written parsers drift the moment they are allowed to.

The rules, in the order the scanner applies them:

1. Scan left to right. ``<u>`` is tried first, then ``**``, then ``*``.
2. A marker OPENS a span only if a matching closer exists later (inside the
   current span, when nested). Otherwise it is literal text — an unclosed ``**``
   must not swallow the rest of the lesson.
3. An unmatched closer is literal. ``**…**`` and ``<u>…</u>`` may be EMPTY (the
   toolbar inserts ``****`` and ``<u></u>`` when Ctrl+B is pressed on a blank
   line — those must read as an empty span, not as stray asterisks); a ``*``
   span must have content, which is what keeps ``**x`` literal.
4. For ``*``/``**``: whitespace immediately INSIDE the marker makes it literal.
   That is what keeps «2 * 3 * 4» text instead of italics, and why ``* x*``
   stays as typed.
5. A closer sitting in a run of asterisks is taken at the END of the run, which
   is what turns ``***και τα δύο***`` into strong(em(...)) rather than
   strong("*και τα δύο") plus a stray asterisk — EXCEPT when that run opens a
   ``**`` span of its own, which the em then skips whole, so that
   ``*a **b** c*`` nests the same way ``**a *b* c**`` does.
6. Newlines are ordinary characters: a span may cross one.
7. Nesting stops at ``MAX_DEPTH``. Past it every marker is literal text, so a
   pathological string of asterisks can neither blow this module's recursion
   limit (it did, at ~4k asterisks, while the TypeScript twin parsed on — a real
   divergence) nor build a tree nobody can render.

Deliberately dependency-free (no ``app.db``, no ``app.models``): it is imported
by the DOCX export, by the word counter, and by tests that must not need a
database.
"""

from __future__ import annotations

# (mark, open, close) — order matters: ``**`` MUST be tried before ``*``.
MARKERS: tuple[tuple[str, str, str], ...] = (
    ("u", "<u>", "</u>"),
    ("strong", "**", "**"),
    ("em", "*", "*"),
)

#: The same cap as the TypeScript twin — see rule 7.
MAX_DEPTH = 32

# Spelled out rather than using ``str.isspace()``, because the TypeScript twin
# must agree character for character.
SPACE = " \t\n\r\f\v\u00a0"


def _is_space(ch: str) -> bool:
    return ch != "" and ch in SPACE


def _char(text: str, i: int) -> str:
    """``text[i]`` with out-of-range reading as the empty string."""
    return text[i] if 0 <= i < len(text) else ""


def _remember_absent(absent: dict[str, int], close: str, start: int) -> None:
    known = absent.get(close)
    if known is None or start < known:
        absent[close] = start


def _strong_span_closer(
    text: str, run_start: int, end: int, absent: dict[str, int]
) -> int | None:
    """The closer of the ``**`` span opening at ``run_start``, or None."""
    content_start = run_start + 2
    if content_start >= end or _is_space(_char(text, content_start)):
        return None
    return _find_closer(text, content_start, end, "**", absent)


def _find_closer(
    text: str, start: int, end: int, close: str, absent: dict[str, int]
) -> int | None:
    """Position of ``close`` inside ``[start, end)``, or ``None``.

    ``absent`` is the per-range memo that keeps the scan linear: once a closer
    has been proven missing from some offset, every later opener in the same
    range gets the answer for free (the search only gets stricter as ``start``
    grows). Without it 20k characters of ``*a `` took 5.6 seconds.
    """
    proven_absent_from = absent.get(close)
    if proven_absent_from is not None and start >= proven_absent_from:
        return None

    j = start
    while j + len(close) <= end:
        if not text.startswith(close, j):
            j += 1
            continue
        if close == "*":
            run_end = j
            while run_end < end and text[run_end] == "*":
                run_end += 1
            if run_end - j >= 2:
                # A ``**`` span INSIDE this em — ``*a **b** c*``. Step over it
                # whole; right-aligning into its opener would end the em
                # mid-marker.
                inner = _strong_span_closer(text, j, end, absent)
                if inner is not None:
                    j = inner + 2
                    continue
            at = run_end - 1  # rule 5
            if at > start and not _is_space(_char(text, at - 1)):
                return at  # an em is never empty
            j = run_end
            continue
        at = j
        if close == "**":
            run_end = j
            while run_end < end and text[run_end] == "*":
                run_end += 1
            at = run_end - 2  # rule 5
            if _is_space(_char(text, at - 1)):
                j = run_end
                continue
        return at  # ``**…**`` and ``<u>…</u>`` may be empty — rule 3
    _remember_absent(absent, close, start)
    return None


def _parse_range(text: str, start: int, end: int, depth: int) -> list[dict]:
    nodes: list[dict] = []
    absent: dict[str, int] = {}
    buf: list[str] = []

    def flush() -> None:
        if buf:
            nodes.append({"type": "text", "value": "".join(buf)})
            buf.clear()

    i = start
    while i < end:
        opened = False
        if depth < MAX_DEPTH:
            for mark, open_, close in MARKERS:
                if i + len(open_) > end or not text.startswith(open_, i):
                    continue
                content_start = i + len(open_)
                if mark != "u" and (
                    content_start >= end or _is_space(_char(text, content_start))
                ):
                    # Rule 4. The marker is literal, and the shorter marker
                    # inside it does NOT get a second chance — else «** x**»
                    # opens an em.
                    buf.append(open_)
                    i = content_start
                    opened = True
                    break
                closer_at = _find_closer(text, content_start, end, close, absent)
                if closer_at is None:
                    continue  # rule 2 — try the next, shorter marker
                flush()
                nodes.append(
                    {
                        "type": mark,
                        "children": _parse_range(text, content_start, closer_at, depth + 1),
                    }
                )
                i = closer_at + len(close)
                opened = True
                break
        if not opened:
            buf.append(text[i])
            i += 1
    flush()
    return nodes


def parse_inline_marks(text: str) -> list[dict]:
    """Parse lesson text into inline nodes.

    A node is either ``{"type": "text", "value": str}`` or
    ``{"type": "strong"|"em"|"u", "children": [...]}``. Never raises, never
    loses a character: anything that is not a well-formed span is text.
    """
    return _parse_range(text, 0, len(text), 0)


def strip_inline_marks(text: str) -> str:
    """The text a reader sees — every marker that actually opened a span gone."""
    out: list[str] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            if node["type"] == "text":
                out.append(node["value"])
            else:
                walk(node["children"])

    walk(parse_inline_marks(text))
    return "".join(out)
