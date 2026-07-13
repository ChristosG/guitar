"""Post-turn guard (Plan 11 Task 2, C3): detect a hand-typed ASCII tab in
assistant prose so `app/agent/loop.py` can intercept it before it ever
reaches the tutor as if it were a real answer.

THE BUG THIS EXISTS FOR: Chris asked for a G major scale tab. The model
typed six lines of ASCII into a code fence — every string reading the
identical `0-2-4-5-7-8-10` fret sequence (not actually a G major scale) —
instead of calling `generate_artifact`, the schema-validated, AlphaTab-
rendered, ACTUALLY PLAYABLE artifact Plan 4 already built and shipped. A
hand-typed tab is unverifiable prose bluffing as a structured answer. This
module exists to CATCH that, never to correct it — silently "fixing" a wrong
tab by eye would just be a second, quieter bluff; the only acceptable fix is
"call the real tool", enforced by `loop.py`'s post-turn guard (see that
module for the suspend/re-prompt/fallback recovery this feeds into) and by
`SYSTEM_PROMPT`'s explicit ban (`app/agent/prompts.py`).

TWO independent shapes are detected by `looks_like_tablature`:

  1. THE STRING-LINE SHAPE — a line that is (ignoring leading/trailing
     whitespace) a single standard-tuning string letter — `e` (lowercase,
     the high string) or `E`/`A`/`D`/`G`/`B` (the other five) — immediately
     followed by `|` and then a run of tab-notation characters (digits,
     dashes, and the handful of technique symbols real ASCII tabs use:
     `~`/`h`/`p`/`b`/`/`/`\\` for slides/hammer-ons/pull-offs/bends).  This
     is Chris's EXACT bluff shape (`e|-----0-2-4-5-7-8-10-`), checked
     ANYWHERE in the text (not just inside a fence — a model could just as
     easily type it as bare prose), and requires >=2 matching lines (one
     alone could be coincidence; every real bluff, including Chris's, has
     several string lines).
  2. THE FENCED DASH/DIGIT BLOCK — >=2 lines *inside a ``` fence* that are
     ENTIRELY tab-notation characters with no letters/words/punctuation
     outside that set (and at least one digit). Catches a tab typed without
     the leading string-letter (e.g. mid-tab, or the letter dropped) while
     staying narrow: an ordinary code fence always has letters, `=`, `[`,
     `]`, or spaces this row shape rejects.

FALSE-POSITIVE DEFENCE (the brief's own explicit warning: "a FALSE POSITIVE
that suppresses a legitimate answer is its own bug") — see
`tests/test_agent_guards.py` for the pinned cases:
  - Shape 1 requires the letter IMMEDIATELY followed by `|`, nothing before
    it on that side. A markdown table row (`| E | A | D |`) always has the
    `|` FIRST (or a space before the letter), never `letter|` glued
    together, so it can never match. A table's separator row (`|---|---|`)
    starts with `|`, not a letter, for the same reason.
  - Shape 2 only ever looks INSIDE a ``` fence, and only counts a row that
    is PURELY tab-notation characters — one stray letter, a bracket, or a
    comma-and-space (`0, 2, 4`) disqualifies that row. `x = [0, 2, 4]` fails
    on every count (`x`, `=`, `[`, `]`, and the spaces around them).
  - Both shapes require at least one DIGIT in the tab-notation content — a
    line of bare dashes alone (e.g. a markdown `---` horizontal rule) is not
    a tab and does not count toward either shape's >=2 threshold.
  - Ordinary prose (no `|`, no fence) never reaches either regex at all.
"""
import re

# Shape 1: the string-line shape (Chris's exact bluff). Standard-tuning
# string letters ONLY (e/E/A/D/G/B) — not every letter of the alphabet —
# immediately followed by `|`, so a markdown table cell (always separated by
# whitespace/another `|`, never glued to a letter) can never match.
_STRING_LINE_RE = re.compile(
    r"(?m)^[ \t]*[eEADGB]\|[0-9\-~hpb/\\]*[0-9][0-9\-~hpb/\\]*[ \t]*$"
)

# Shape 2: rows inside a ``` fence. A row must be PURELY tab-notation
# characters (digits/dashes/technique symbols) with at least one digit —
# a stray letter, bracket, comma, or space disqualifies it.
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_DASH_DIGIT_ROW_RE = re.compile(r"^[0-9\-~hpb/\\]*[0-9][0-9\-~hpb/\\]*$")

_MIN_MATCHING_LINES = 2


def looks_like_tablature(text: str) -> bool:
    """True iff `text` contains a hand-typed ASCII guitar tab — either shape
    described in this module's docstring. Used by `app/agent/loop.py` as a
    post-turn guard: a positive here means the content must NOT be shown to
    the tutor as a final answer (see that module for the recovery).
    """
    if not text:
        return False

    if len(_STRING_LINE_RE.findall(text)) >= _MIN_MATCHING_LINES:
        return True

    for fence_body in _FENCE_RE.findall(text):
        rows = [ln.strip() for ln in fence_body.splitlines() if ln.strip()]
        matching = [ln for ln in rows if _DASH_DIGIT_ROW_RE.match(ln)]
        if len(matching) >= _MIN_MATCHING_LINES:
            return True

    return False
