"""The registry must POINT AT the prompts, never copy them.

A viewer that holds its own copy of a prompt is how it silently starts lying: it
shows the copy while the model gets the original, and nothing ever tells you.
That is worse than no viewer, because it is a viewer he will trust — the same
shape as the citation failure this codebase is architected against.
"""
import inspect
import pathlib
import re

import pytest

from app.agent.prompts import SYSTEM_PROMPT
from app.curriculum.corpus import CURRICULUM_SYSTEM
from app.prompts.registry import REGISTRY, render
from app.routers.settings import _PROBE_PROMPT
from app.students.context import STUDENT_PITCH

# Every Greek annotation must actually be Greek. `Ͱ-Ͽ` is Greek and
# Coptic, `ἀ-῿` Greek Extended (the polytonic block — not used here,
# but excluding it would be a trap for a future edit).
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")


def test_the_chat_system_prompt_is_the_SAME_OBJECT_not_a_copy():
    """`is`, not `==`. Equality would pass against a copy that has since drifted."""
    rendered = render("chat.system", locale="el")
    assert SYSTEM_PROMPT in rendered.text
    assert REGISTRY["chat.system"].source_of_truth() is SYSTEM_PROMPT


def test_the_curriculum_system_prompt_is_the_same_object():
    assert REGISTRY["curriculum.system"].source_of_truth() is CURRICULUM_SYSTEM


def test_every_entry_has_greek_annotation():
    """He reads Greek. An entry without it is an English string at the tutor,
    which the settings page's own contract forbids."""
    for pid, entry in REGISTRY.items():
        assert entry.title_el.strip(), f"{pid} has no Greek title"
        assert entry.what_it_does_el.strip(), f"{pid} has no Greek description"
        assert entry.when_it_runs_el.strip(), f"{pid} has no Greek trigger"


def test_the_greek_annotations_are_actually_in_greek():
    """The failure this guards is not an empty string — it is an English one.
    `test_every_entry_has_greek_annotation` passes on `title_el="Chat system
    prompt"`, and that string reaches a tutor who does not read English.
    """
    for pid, entry in REGISTRY.items():
        for field in ("title_el", "what_it_does_el", "when_it_runs_el"):
            value = getattr(entry, field)
            assert _GREEK.search(value), f"{pid}.{field} contains no Greek: {value!r}"


def test_render_marks_interpolated_variables_rather_than_hiding_them():
    """He must see WHERE the student brief goes, not a prompt with a hole in it."""
    rendered = render("lesson.draft", locale="el")
    assert rendered.spans, "no interpolation spans reported"
    assert any(s.name for s in rendered.spans)


def test_every_span_actually_locates_its_value_in_the_rendered_text():
    """A span whose offsets do not hold up is a chip drawn over the wrong words —
    the viewer lying in the one place it claims to be precise.
    """
    for pid in REGISTRY:
        for locale in ("el", "en"):
            rendered = render(pid, locale=locale)
            for span in rendered.spans:
                assert rendered.text[span.start:span.end] == span.value, (
                    f"{pid} ({locale}): span {span.name!r} points at "
                    f"{rendered.text[span.start:span.end]!r}, not {span.value!r}"
                )


def test_locale_changes_the_language_directive():
    """`i18n.language_directive` is the app's only locale-varying directive and it
    is injected into 8 prompts. If the rendered preview ignores locale, he is
    reading a prompt the model never gets."""
    el = render("chat.system", locale="el").text
    en = render("chat.system", locale="en").text
    assert el != en
    assert "Greek" in el or "Ελλην" in el


def test_every_entry_renders_in_both_locales():
    """A registry entry that raises is a Settings page that 500s in his face."""
    for pid in REGISTRY:
        for locale in ("el", "en"):
            rendered = render(pid, locale=locale)
            assert rendered.text.strip(), f"{pid} ({locale}) rendered empty"
            assert rendered.id == pid


def test_render_rejects_an_unknown_prompt_id():
    with pytest.raises(KeyError):
        render("chat.nonexistent", locale="el")


# ---------------------------------------------------------------------------
# Completeness — the test that keeps this honest over time
# ---------------------------------------------------------------------------

_CALL = re.compile(r"\.(chat|guided_json|chat_tools|chat_tools_stream|vision)\(")


def _app_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "app"


def _live_call_sites() -> list[str]:
    """Every `"path/to/file.py:LINE"` outside `app/llm/` that reaches an
    LLMProvider method. `app/llm/` is excluded because it IS the provider — the
    calls in there are the implementation, not a prompt the app sends.
    """
    root = _app_root()
    return [
        f"{p.relative_to(root)}:{i}"
        for p in sorted(root.rglob("*.py"))
        if "llm/" not in str(p.relative_to(root))
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if _CALL.search(line)
    ]


def test_registry_covers_every_provider_call_site():
    """THE test that keeps this honest over time.

    15 call sites reach an LLMProvider method outside app/llm/ (counted
    2026-07-17: artifacts/generate.py x2, lessons/draft.py, brain/ocr.py,
    curriculum/draft.py x3, brain/retrieve.py x2, curriculum/extend.py,
    agent/loop.py x2, curriculum/refine.py, curriculum/outline.py,
    routers/settings.py). When someone adds the 16th, this fails and the viewer
    does not silently go stale.

    MATCHED PER FILE, BY COUNT — not per file, by existence, which is what the
    brief drafted and what would NOT have kept the promise in this docstring.
    `agent/loop.py` already has a registry entry, so an existence check passes
    the moment ANY entry names the file: the 16th call site could be added to
    `loop.py` — the file with the most prompt surface in the app — and this test
    would stay green. Counting is what makes "the 16th fails" true everywhere
    rather than only in the nine files that happen to have exactly one call.

    NOT matched by exact line, either: a docstring edit above a call would fail a
    line-exact assertion, and a test that cries wolf on unrelated edits gets its
    numbers bumped without thought, which is the same staleness by another route.
    `test_registered_call_sites_still_point_at_provider_calls` is what keeps the
    line numbers themselves honest, so the counts here cannot be met with
    fabricated ones.
    """
    sites = _live_call_sites()
    covered = sorted({s for e in REGISTRY.values() for s in e.call_sites})

    live_by_file: dict[str, int] = {}
    for s in sites:
        live_by_file[s.rsplit(":", 1)[0]] = live_by_file.get(s.rsplit(":", 1)[0], 0) + 1
    covered_by_file: dict[str, int] = {}
    for s in covered:
        covered_by_file[s.rsplit(":", 1)[0]] = covered_by_file.get(s.rsplit(":", 1)[0], 0) + 1

    unregistered = {
        f: n for f, n in live_by_file.items()
        if covered_by_file.get(f, 0) != n
    }
    assert not unregistered, (
        "provider call sites with no registry entry — every line that reaches a "
        "model must be reachable from a prompt the tutor can read.\n"
        f"  live (file -> calls):       {live_by_file}\n"
        f"  registered (file -> calls): {covered_by_file}\n"
        f"  mismatched:                 {unregistered}"
    )

    phantom = sorted(set(covered_by_file) - set(live_by_file))
    assert not phantom, (
        f"registry claims call sites in files that no longer call a provider: {phantom}"
    )


def test_registered_call_sites_still_point_at_provider_calls():
    """The counts above are only worth something if the lines are real.

    Without this, `call_sites` could be padded with any line number at all and
    `test_registry_covers_every_provider_call_site` would still pass.
    """
    root = _app_root()
    stale = []
    for entry in REGISTRY.values():
        for site in entry.call_sites:
            path, _, lineno = site.rpartition(":")
            f = root / path
            if not f.exists():
                stale.append(f"{entry.id}: {site} — no such file")
                continue
            lines = f.read_text().splitlines()
            i = int(lineno)
            if not (1 <= i <= len(lines)) or not _CALL.search(lines[i - 1]):
                stale.append(f"{entry.id}: {site} — not a provider call")
    assert not stale, f"registry call_sites have gone stale: {stale}"


def test_source_refs_point_inside_the_real_definition():
    """`source_ref` is SHOWN to the tutor ("app/agent/prompts.py:67 — shown, so
    it's auditable"). A rotted one is an auditable claim that does not audit.

    Function-backed entries are checked against `inspect` — the LIVE line range,
    not a remembered number. WITHIN the range, not equal to its first line: half
    the prompts in this app are one branch of a builder that has three, and
    `retrieve.build_grounded_messages:533` (the zero-hits branch) is a strictly
    more useful thing to show him than `:502` (the `def`). Demanding the `def`
    line would force every branch entry to cite the same spot and quietly make
    `source_ref` useless — precise where it can be, honest either way.

    Constant-backed entries can only be range-checked against the file: a module
    constant has no `inspect` handle to compare against.
    """
    root = _app_root().parent
    bad = []
    for entry in REGISTRY.values():
        path, _, lineno = entry.source_ref.rpartition(":")
        f = root / path
        if not f.exists():
            bad.append(f"{entry.id}: {entry.source_ref} — no such file")
            continue
        i = int(lineno)
        obj = entry.source_of_truth()
        if inspect.isfunction(obj):
            lines, first = inspect.getsourcelines(obj)
            if not (first <= i <= first + len(lines) - 1):
                bad.append(
                    f"{entry.id}: source_ref says {i}, but {obj.__name__} spans "
                    f"{first}-{first + len(lines) - 1}"
                )
        elif not (1 <= i <= len(f.read_text().splitlines())):
            bad.append(f"{entry.id}: {entry.source_ref} — line out of range")
    assert not bad, f"source_ref has rotted: {bad}"


def test_the_registry_holds_no_copy_of_any_prompt_it_points_at():
    """The rule the whole task rests on, checked structurally rather than by
    eye: every entry backed by a string constant renders THAT object, and the
    registry module's own source does not contain the text.
    """
    import app.prompts.registry as registry_module

    source = pathlib.Path(registry_module.__file__).read_text()
    for entry in REGISTRY.values():
        obj = entry.source_of_truth()
        if not isinstance(obj, str):
            continue
        # A 40-char window is long enough that a match is a copy, not a coincidence.
        needle = obj[:40]
        assert needle not in source, (
            f"{entry.id}: registry.py contains a literal copy of the prompt it "
            f"claims to point at ({needle!r})"
        )


def test_every_flow_is_named_and_entries_are_grouped_by_it():
    for pid, entry in REGISTRY.items():
        assert entry.flow, f"{pid} has no flow"
        assert entry.id == pid, f"{pid} keyed under a different id than {entry.id}"


# ---------------------------------------------------------------------------
# The two byte-identical lifts this task performed
# ---------------------------------------------------------------------------


def test_the_lifted_constants_are_byte_identical_to_the_text_they_replaced():
    """Registering these two prompts required lifting them out of the code that
    sent them: `STUDENT_PITCH` out of `build_student_brief`'s tail, and
    `_PROBE_PROMPT` out of the `guided_json` call in `settings.test_settings`. An
    inline literal is the one shape a registry cannot point at — the viewer would
    have to hold a second copy, which is the drift it exists to prevent.

    The spec permits that lift on ONE condition, and this is it: the model
    receives the identical bytes, proven, not asserted in a commit message.
    Without this test the "refactor" is prompt editing wearing a disguise, which
    is the single thing the transparency spec exists to prevent.

    The literals below are deliberately duplicated HERE and nowhere else. A test
    that reads the constant to check the constant checks nothing.
    """
    assert STUDENT_PITCH == (
        "Write for THIS student: pitch the explanations at his level, and where "
        "his notes say he is stuck, address it directly instead of teaching past it."
    )
    assert _PROBE_PROMPT == "Reply with {\"ok\": true} and nothing else."


def test_the_editable_slice_points_at_the_live_constant_too():
    """`Slice.default` is under the same rule as `source_of_truth`: the object,
    not a copy of its text. P2 resolves `override.text if present else default`,
    and a stale default is a Reset button that restores something the code has
    not said for months.
    """
    slices = REGISTRY["shared.student_brief"].slices
    assert len(slices) == 1
    assert slices[0].id == "student.pitch"
    assert slices[0].default is STUDENT_PITCH


def test_the_only_editable_slice_carries_no_placeholder():
    """The property that makes this slice safe to hand to a textarea. A `{field}`
    in here would be an f-string placeholder the surrounding builder requires —
    drop it in an edit and the next lesson draft is a KeyError in his face, i.e.
    a 500 where a lesson should be.
    """
    assert "{" not in STUDENT_PITCH and "}" not in STUDENT_PITCH
