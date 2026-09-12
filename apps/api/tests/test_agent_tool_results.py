"""Tool results reach the model as the tutor wrote them, and never unbounded.

2026-09-11: `get_curriculum` on a 25-lesson Greek course returned 488,708 chars
of prose; `json.dumps(..., ensure_ascii=True)` made that 2,310,878 chars of
`\\uXXXX`, and the next model call was "~1,826,053 tokens (limit 1,000,000)".
"""
from app.agent import loop


def test_stringify_keeps_greek_as_greek():
    out = loop._stringify({"title": "Η θεωρία του ήχου"})
    assert "Η θεωρία του ήχου" in out
    assert "\\u0397" not in out


def test_stringify_caps_at_limit_with_an_honest_marker():
    big = {"body": "α" * (loop.TOOL_RESULT_MAX_CHARS + 5_000)}
    out = loop._stringify(big)
    assert len(out) <= loop.TOOL_RESULT_MAX_CHARS + len(loop.TOOL_RESULT_TRUNCATION_MARKER)
    assert out.endswith(loop.TOOL_RESULT_TRUNCATION_MARKER)


def test_stringify_below_limit_is_untouched():
    out = loop._stringify({"x": "μικρό"})
    assert loop.TOOL_RESULT_TRUNCATION_MARKER not in out
