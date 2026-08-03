"""The byte-identity baseline for every registered prompt, and the one helper that
makes it reproducible.

WHY THIS EXISTS. Making 29 prompts editable meant lifting ~30 f-string templates out
of their builders into module-level constants that `resolve()` can override. Every
one of those lifts is the refactor the spec singles out as the dangerous one:

    "Every slice extraction ships with a test asserting the rendered prompt is
     byte-identical to the pre-refactor string. Extraction without that test is
     prompt editing disguised as refactoring, which is the one thing this spec
     exists to prevent."

Thirty of those tests, hand-written, would be thirty chances to assert the wrong
thing. So the baseline is captured ONCE from the pre-refactor tree (`--write`, run at
`30e98d5`) and `test_prompts_byte_identity.py` asserts every render still matches it
byte for byte. One mechanism, no judgement calls, and it fails loudly on a lift that
moved a newline.

WHY THE DATE IS FROZEN. `_SAMPLE_STUDENT` has a `birthdate` and `students/context._age`
computes against `date.today()`, so `shared.student_brief` — and the four prompts that
interpolate it — contain a number that changes on the sample student's birthday. A
fixture holding "18 years old" would go red one morning for a reason that has nothing
to do with prompts, and the fix would be to bump the number without reading why. The
frozen date makes the fixture permanent; the REAL `_age` still runs, so it is still
the live function under test.
"""
from __future__ import annotations

import datetime
import json
from contextlib import contextmanager
from pathlib import Path

BASELINE_PATH = Path(__file__).parent / "fixtures" / "prompt_renders_baseline.json"

# The day the baseline was captured. Arbitrary, and that is the point: any fixed day
# makes `_age` a constant. It must never change, or every student-bearing prompt in
# the fixture shifts by a year at once.
FROZEN_TODAY = datetime.date(2026, 7, 17)

LOCALES = ("el", "en")

# REGENERATED ONCE, DELIBERATELY, after the language fix — and this is the only entry
# in this file's history that is not "nothing changed".
#
# The lift itself changed zero bytes across all 31 prompts × both locales; that was
# verified before this. What moved afterwards was five renders — `curriculum.outline`,
# `curriculum.extend`, `curriculum.refine`, `lesson.draft`, `lesson.deepen` — and only
# at `en`, because those five now render at the SAMPLE COURSE's language instead of the
# cockpit's. That is Chris's bug being fixed, not a prompt being edited: the text the
# model receives is untouched, and the diff was confirmed to be exactly those five ×
# `en` and nothing else before regenerating. See `registry._SAMPLE_COURSE_LANGUAGE`.
#
# 2026-08-03, two SCOPED recaptures (single entries, diff verified first, everything
# else byte-identical): `tools.descriptions` — the catalogue stopped advertising a
# nonexistent artifact kind ('scale'→'scale_diagram'), the unrenderable 'gear_card',
# and the model-facing `student_id` args; and `curriculum.revise` — the revise
# planner now carries `{curriculum_style}` like every other flow that writes course
# material (its titles/objectives land verbatim on the board — the register rule
# in `app.i18n.curriculum_style` applied, not new prose invented).


class _FrozenDate(datetime.date):
    @classmethod
    def today(cls):
        return FROZEN_TODAY


@contextmanager
def frozen_today():
    """Pin `students/context.date.today()`. Patches the NAME the module bound at
    import (`from datetime import date`), not `datetime.date` globally — the latter
    would reach into SQLAlchemy's own type handling for every other test in the run.
    """
    from app.students import context

    original = context.date
    context.date = _FrozenDate
    try:
        yield
    finally:
        context.date = original


def render_all() -> dict[str, dict[str, str]]:
    """`{prompt_id: {locale: rendered_text}}` for every entry, code defaults only.

    `db=None` is deliberate: the baseline is what an UN-EDITED install sends, which is
    exactly the text `resolve()` must keep returning when nothing is stored.
    """
    from app.prompts import registry

    with frozen_today():
        return {
            prompt_id: {loc: registry.render(prompt_id, loc).text for loc in LOCALES}
            for prompt_id in registry.REGISTRY
        }


def load_baseline() -> dict[str, dict[str, str]]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = render_all()
    BASELINE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = sum(len(t) for v in data.values() for t in v.values())
    print(f"wrote {len(data)} prompts x {len(LOCALES)} locales, {total:,} chars")
