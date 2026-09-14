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
3. A span is never empty, and an unmatched closer is literal.
4. For ``*``/``**``: whitespace immediately INSIDE the marker makes it literal.
   That is what keeps «2 * 3 * 4» text instead of italics, and why ``* x*``
   stays as typed.
5. A closer sitting in a run of asterisks is taken at the END of the run, which
   is what turns ``***και τα δύο***`` into strong(em(...)) rather than
   strong("*και τα δύο") plus a stray asterisk.
6. Newlines are ordinary characters: a span may cross one.

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

# Spelled out rather than using ``str.isspace()``, because the TypeScript twin
# must agree character for character.
SPACE = " \t\n\r\f\v\u00a0"


def _is_space(ch: str) -> bool:
    return ch != "" and ch in SPACE


def _char(text: str, i: int) -> str:
    """``text[i]`` with out-of-range reading as the empty string."""
    return text[i] if 0 <= i < len(text) else ""


def _find_closer(text: str, start: int, end: int, close: str) -> int | None:
    """Position of ``close`` inside ``[start, end)``, or ``None``."""
    asterisks = close in ("*", "**")
    j = start
    while j + len(close) <= end:
        if text.startswith(close, j):
            at = j
            if asterisks:
                # Right-align inside the run: ``***x***`` closes with the LAST
                # two stars, leaving the first one to close the nested em.
                run_end = j
                while run_end < end and text[run_end] == "*":
                    run_end += 1
                at = run_end - len(close)
                if _is_space(_char(text, at - 1)):
                    j += 1
                    continue
            if at <= start:  # a span is never empty
                j += 1
                continue
            return at
        j += 1
    return None


def _parse_range(text: str, start: int, end: int) -> list[dict]:
    nodes: list[dict] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            nodes.append({"type": "text", "value": "".join(buf)})
            buf.clear()

    i = start
    while i < end:
        opened = False
        for mark, open_, close in MARKERS:
            if i + len(open_) > end or not text.startswith(open_, i):
                continue
            content_start = i + len(open_)
            if mark != "u" and (
                content_start >= end or _is_space(_char(text, content_start))
            ):
                # Rule 4. The marker is literal, and the shorter marker inside
                # it does NOT get a second chance — else «** x**» opens an em.
                buf.append(open_)
                i = content_start
                opened = True
                break
            closer_at = _find_closer(text, content_start, end, close)
            if closer_at is None:
                continue  # rule 2 — try the next, shorter marker
            flush()
            nodes.append(
                {"type": mark, "children": _parse_range(text, content_start, closer_at)}
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
    return _parse_range(text, 0, len(text))


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
