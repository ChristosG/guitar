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


def test_the_case_table_still_covers_what_the_grammar_was_written_for():
    inputs = [c["in"] for c in _CASES]
    assert len(_CASES) >= 12
    assert "***και τα δύο***" in inputs  # strong around em
    assert "2 * 3 * 4" in inputs  # arithmetic is not italics
    assert "" in inputs


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


def test_an_unclosed_marker_never_swallows_the_rest_of_the_lesson():
    long_tail = "**ξεκινά έντονα\n" + "κανονικό κείμενο\n" * 20
    assert strip_inline_marks(long_tail) == long_tail
