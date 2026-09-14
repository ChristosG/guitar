"""The inline-mark grammar, pinned by the SHARED case table.

Every case here is also run, from the very same bytes, by
`apps/web/tests/inline-marks.spec.ts` against `src/lib/inline-marks.ts`. The two
parsers exist because the tutor's lesson text is plain strings with three inline
markers (`**έντονο**`, `*πλάγιο*`, `<u>υπογράμμιση</u>`) and both halves of the
app have to read them: the board and the editor in TypeScript, the Word export
and the word counter in Python. A rule implemented in only one of them is a
lesson that looks one way on screen and another way in the .docx — which is the
kind of bug the tutor discovers in front of a student.

So the ground truth is the JSON, not this file, and
`test_the_web_copy_of_the_case_table_is_identical` is what stops the two copies
from drifting.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.curriculum.inline_marks import parse_inline_marks, strip_inline_marks

_HERE = Path(__file__).resolve().parent
_API_FIXTURE = _HERE / "fixtures" / "inline_marks_cases.json"
_WEB_FIXTURE = _HERE.parents[1] / "web" / "tests" / "fixtures" / "inline_marks_cases.json"

_CASES = json.loads(_API_FIXTURE.read_text(encoding="utf-8"))["cases"]
_IDS = [c["name"] for c in _CASES]


def test_the_web_copy_of_the_case_table_is_identical():
    assert _WEB_FIXTURE.exists(), f"the web copy is missing: {_WEB_FIXTURE}"
    assert _API_FIXTURE.read_bytes() == _WEB_FIXTURE.read_bytes(), (
        "the two copies of inline_marks_cases.json have drifted — copy one over "
        "the other and run BOTH suites"
    )


# Named, not counted: a `len(...) >= 12` guard is satisfied by twelve cases about
# nothing. These are the rules the grammar exists for, and deleting one of them
# has to fail here rather than quietly narrow the contract.
_REQUIRED = {
    "plain text has no marks",
    "strong",
    "em",
    "underline",
    "em nested inside strong",
    "strong nested inside em",
    "nesting works both ways round",
    "strong nested inside underline",
    "triple markers are strong around em",
    "an opener with no closer is literal",
    "an unmatched closer is literal",
    "the empty strong pair the toolbar inserts",
    "the empty underline pair the toolbar inserts",
    "a bare pair of asterisks has no closer, so it is text",
    "a lone asterisk is literal",
    "arithmetic is not italics",
    "whitespace just inside the marker keeps it literal",
    "a span may cross a newline",
    "the empty string",
}


def test_the_case_table_still_covers_what_the_grammar_was_written_for():
    missing = _REQUIRED - set(_IDS)
    assert not missing, f"the case table lost: {sorted(missing)}"
    assert len(_IDS) == len(set(_IDS)), "two cases share a name"


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_parse_inline_marks(case):
    assert parse_inline_marks(case["in"]) == case["tree"]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_strip_inline_marks(case):
    assert strip_inline_marks(case["in"]) == case["stripped"]


def _render(nodes: list[dict]) -> str:
    out = []
    for node in nodes:
        if node["type"] == "text":
            out.append(node["value"])
        elif node["type"] == "u":
            out.append("<u>" + _render(node["children"]) + "</u>")
        elif node["type"] == "strong":
            out.append("**" + _render(node["children"]) + "**")
        else:
            out.append("*" + _render(node["children"]) + "*")
    return "".join(out)


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_no_character_is_ever_lost(case):
    """Re-rendering the tree gives the input back, whatever the tolerance rules
    decided — a parser that silently eats the tutor's typing is worse than one
    that refuses to italicise."""
    assert _render(parse_inline_marks(case["in"])) == case["in"]


def test_text_nodes_are_merged_not_split_per_character():
    # A tree with one text node per character would still pass `stripped`, and
    # would make the DOCX export write one run per letter.
    tree = parse_inline_marks("2 * 3 * 4")
    assert tree == [{"type": "text", "value": "2 * 3 * 4"}]


def _depth(nodes: list[dict], at: int = 0) -> int:
    return max(
        [_depth(n["children"], at + 1) for n in nodes if n["type"] != "text"] + [at]
    )


def test_a_long_lesson_full_of_lone_asterisks_parses_in_linear_time():
    # 15k characters, 5000 asterisks, not one of them a mark. The naive scanner
    # re-searched the whole tail for every one of them and took 5.6 SECONDS —
    # on a word count that runs on every keystroke-ish save.
    text = "*a " * 5000
    started = time.monotonic()
    tree = parse_inline_marks(text)
    assert time.monotonic() - started < 1.0
    assert tree == [{"type": "text", "value": text}]


def test_twenty_thousand_asterisks_parse_without_blowing_the_stack():
    # This one used to raise RecursionError at ~4k while the TypeScript twin
    # parsed on — the worst kind of divergence, because the board would render
    # what the Word export had just crashed on. The same assertions run in
    # `apps/web/tests/inline-marks.spec.ts`.
    text = "*" * 20000
    started = time.monotonic()
    tree = parse_inline_marks(text)
    assert time.monotonic() - started < 1.0
    assert _depth(tree) == 32  # MAX_DEPTH — past it every marker is literal
    assert _render(tree) == text
    assert len(strip_inline_marks(text)) == 20000 - 32 * 4


def test_an_unclosed_marker_never_swallows_the_rest_of_the_lesson():
    long_tail = "**ξεκινά έντονα\n" + "κανονικό κείμενο\n" * 20
    assert strip_inline_marks(long_tail) == long_tail
