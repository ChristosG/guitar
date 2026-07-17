"""THE GUARD ON THE WHOLE REFACTOR: not one byte of any prompt changed.

Making every prompt editable meant lifting ~30 f-string templates out of their
builders into module-level constants that `resolve()` can override. Every one of
those lifts is the exact refactor the spec singles out as the dangerous one:

    "Every slice extraction ships with a test asserting the rendered prompt is
     byte-identical to the pre-refactor string. Extraction without that test is
     prompt editing disguised as refactoring, which is the one thing this spec
     exists to prevent."

Thirty hand-written equality tests would be thirty chances to assert the wrong
thing — and the mistake this catches is not a dropped sentence (someone would
notice) but a dropped `\\n`, a lost `f` prefix turning `{x}` into a literal, or a
`.format()` eating a brace. Those are invisible in review and they change what the
model reads.

So the baseline was captured ONCE from the pre-refactor tree (`30e98d5`, before the
first lift) with `python -m tests.prompt_baseline`, and every render is compared
against it byte for byte, in both locales.

IF THIS TEST GOES RED AND THE CHANGE WAS DELIBERATE, the fix is NOT to regenerate the
baseline until it passes. It is to read the diff this test prints, confirm the change
is the one intended, and regenerate on that commit alone. A baseline refreshed
reflexively is a baseline that proves nothing — it is the "bump the number without
thought" failure P1 already called out about line-numbered assertions.
"""
from __future__ import annotations

import pytest

from app.prompts import registry
from tests.prompt_baseline import LOCALES, load_baseline, render_all

BASELINE = load_baseline()


def test_the_baseline_covers_every_registered_prompt():
    """A prompt added without a baseline entry would otherwise be silently exempt
    from the only test standing between this refactor and a changed prompt."""
    assert set(BASELINE) == set(registry.REGISTRY)


@pytest.mark.parametrize("prompt_id", sorted(BASELINE))
@pytest.mark.parametrize("locale", LOCALES)
def test_the_lift_changed_not_one_byte_of_any_prompt(prompt_id, locale):
    rendered = render_all()[prompt_id][locale]
    expected = BASELINE[prompt_id][locale]
    assert rendered == expected, (
        f"{prompt_id} @ {locale} no longer renders what it rendered before the "
        f"templates were lifted. This is a CHANGED PROMPT, not a failing test."
    )
