"""Overrides: the code default is the truth, an override layers on top, and the
live prompt path actually reads the result.

THE TEST THAT MATTERS IS `test_the_live_lesson_path_actually_sends_the_override`.
Everything else here is plumbing around it. A Settings page that stores an
override the model never receives is not a feature with a bug in it — it is a
screen that tells the tutor he changed how his lessons are written when he did
not. That is the same failure as a fabricated citation: nothing breaks, nothing
goes red, and he trusts it.

THE BRIEF'S DRAFTED TESTS NAMED THREE IDS THAT DO NOT EXIST, and the fix was not
to create them. `student.brief` is `shared.student_brief`; `lesson.draft` has no
slices (the student pitch is registered once, on the prompt that owns it, not
copied onto all four prompts that interpolate it); and `lesson.draft.length` is
the case the spec's own audit rejected by name — *"draft.py:135-139 — 'LENGTH IS
NOT OPTIONAL' wrapped around {ctx.teaching_minutes} and {ctx.position}"* is
listed as an example of pedagogy WELDED to a contract. Manufacturing that slice
to satisfy a test would be prompt editing disguised as refactoring, which is the
one thing the spec exists to prevent. So the placeholder rule is tested against
an injected slice instead — the validator's contract is what must not rot, and
the day a placeholder-bearing slice is earned, this is the test already waiting
for it.
"""
import pytest

from app.models.prompt import SLICE_ID_LEN
from app.models.student import Student
from app.prompts import registry
from app.prompts.registry import Slice
from app.students.context import STUDENT_PITCH, build_student_brief

# The one editable slice this app ships, and the prompt that owns it.
SID = "student.pitch"
OWNER = "shared.student_brief"


# ---------------------------------------------------------------------------
# Resolution: code default, override on top, never the reverse
# ---------------------------------------------------------------------------


def test_default_comes_from_code_when_no_override(client):
    """Defaults live in CODE — diffable, reviewable, and the reset target.
    Overrides layer on top. Never the other way round."""
    r = client.get(f"/prompts/{OWNER}")
    assert r.status_code == 200
    assert r.json()["slices"][0]["effective"] == r.json()["slices"][0]["default"]
    assert r.json()["slices"][0]["default"] == STUDENT_PITCH


def test_saving_an_override_then_resetting_restores_the_code_default(client):
    client.put(f"/prompts/slices/{SID}", json={"text": "Γράψε πιο απλά."})
    assert client.get(f"/prompts/{OWNER}").json()["slices"][0]["effective"] == "Γράψε πιο απλά."
    client.delete(f"/prompts/slices/{SID}")
    body = client.get(f"/prompts/{OWNER}").json()["slices"][0]
    assert body["effective"] == body["default"]
    assert body["has_override"] is False


def test_the_rendered_preview_shows_the_override_not_the_default(client):
    """The viewer must show what the model GETS. If the card's prompt body keeps
    showing the code default while the textarea underneath shows his override,
    the page contradicts itself and one of the two is a lie.
    """
    client.put(f"/prompts/slices/{SID}", json={"text": "Γράψε πιο απλά."})
    body = client.get(f"/prompts/{OWNER}").json()
    assert "Γράψε πιο απλά." in body["text"]
    assert STUDENT_PITCH not in body["text"]


def test_the_pitch_span_follows_the_override(client):
    """`registry._locate` RAISES when a sample is not in the rendered text — by
    design. So an override that the span still describes with the old default is
    not a cosmetic drift, it is a 500 on the Settings page the moment he saves.
    """
    client.put(f"/prompts/slices/{SID}", json={"text": "Γράψε πιο απλά."})
    body = client.get(f"/prompts/{OWNER}").json()
    pitch = [s for s in body["spans"] if s["name"] == "pitch"]
    assert pitch, "the pitch span vanished"
    assert pitch[0]["value"] == "Γράψε πιο απλά."
    assert body["text"][pitch[0]["start"]:pitch[0]["end"]] == "Γράψε πιο απλά."


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS — the live path, not the viewer
# ---------------------------------------------------------------------------


def test_the_live_lesson_path_actually_sends_the_override(client, db):
    """An override he saves must change what the model gets.

    `build_student_brief` is the REAL builder — the one `jobs/curriculum_draft.py:322`,
    `curriculum/generate.py:137` and `curriculum/interview.py:493` call to write his
    lessons. Not a copy of it, not a preview of it. If this passes and the others
    fail, we shipped something. If this fails and the others pass, we shipped a
    viewer bolted onto nothing.
    """
    student = Student(name="Νίκος", level="beginner", preferred_language="el")
    db.add(student)
    db.commit()

    assert STUDENT_PITCH in build_student_brief(db, student.id)

    client.put(f"/prompts/slices/{SID}", json={"text": "Γράψε πιο απλά."})
    db.expire_all()

    brief = build_student_brief(db, student.id)
    assert "Γράψε πιο απλά." in brief
    assert STUDENT_PITCH not in brief


def test_resetting_puts_the_live_path_back_on_the_code_default(client, db):
    student = Student(name="Νίκος", level="beginner", preferred_language="el")
    db.add(student)
    db.commit()

    client.put(f"/prompts/slices/{SID}", json={"text": "Γράψε πιο απλά."})
    client.delete(f"/prompts/slices/{SID}")
    db.expire_all()

    assert STUDENT_PITCH in build_student_brief(db, student.id)


def test_a_student_with_no_override_produces_the_byte_identical_prompt(client, db):
    """`students/context.py`'s docstring promises a brief that is byte-identical
    to the pre-module one when nothing is configured. Adding a database read to
    that function must not have changed a single byte of the default path.
    """
    student = Student(name="Νίκος", level="beginner", preferred_language="el")
    db.add(student)
    db.commit()
    assert build_student_brief(db, student.id).endswith(STUDENT_PITCH)


# ---------------------------------------------------------------------------
# Validation — every failure is a CODE, never a stack trace
# ---------------------------------------------------------------------------


@pytest.fixture
def placeholder_slice(monkeypatch):
    """A slice whose default carries an f-string placeholder.

    No shipped slice has one — that is exactly what makes `student.pitch` safe to
    hand to a textarea (`test_the_only_editable_slice_carries_no_placeholder`).
    The validator still has to exist, because the `Slice` type permits one and a
    dropped `{field}` is a `KeyError` at call time, i.e. a 500 where a lesson
    should be. Injected here rather than invented in the registry: see this
    module's docstring.
    """
    sl = Slice(
        id="test.length",
        label_el="Δοκιμαστικό",
        default="Write about {target_words} words for this {minutes}-minute lesson.",
        kind="replace",
    )
    monkeypatch.setitem(registry.SLICES, sl.id, (registry.REGISTRY[OWNER], sl))
    return sl


def test_an_edit_that_drops_a_required_placeholder_is_rejected_with_a_CODE(
    client, placeholder_slice
):
    """A KeyError at call time is a 500 in the tutor's face. And he never sees a
    stack trace — he gets a machine-readable code the web turns into one Greek
    sentence."""
    r = client.put("/prompts/slices/test.length", json={"text": "Make it long."})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "missing_placeholder"
    assert r.json()["detail"]["missing"] == ["minutes", "target_words"]


def test_an_edit_that_keeps_every_placeholder_is_accepted(client, placeholder_slice):
    """The rule is 'do not DROP one', not 'do not touch the text'. A validator
    that rejects every edit is a locked slice with extra steps."""
    r = client.put(
        "/prompts/slices/test.length",
        json={"text": "About {target_words} words. You have {minutes} minutes."},
    )
    assert r.status_code == 200


def test_an_edit_that_INVENTS_a_placeholder_is_rejected(client, placeholder_slice):
    """THIS is the one that is actually a KeyError at call time: `.format()`
    tolerates an unused kwarg but not an unknown key. Hours into an unattended
    twenty-lesson run, it is a 500 where a lesson should be."""
    r = client.put(
        "/prompts/slices/test.length",
        json={"text": "About {target_words} words for {student_name} in {minutes}."},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_placeholder"
    assert r.json()["detail"]["unknown"] == ["student_name"]


def test_a_stray_brace_is_a_code_not_a_ValueError(client, placeholder_slice):
    """`str.format` raises `ValueError` on this, and so does the validator's own
    parser — which would make the guard against a 500 the thing that caused one.
    """
    r = client.put(
        "/prompts/slices/test.length",
        json={"text": "About {target_words} words in {minutes} { oops"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "malformed_braces"


def test_a_brace_in_a_slice_that_is_never_formatted_is_just_a_character(client):
    """`student.pitch` is appended raw — nothing calls `.format()` on it. So a
    lone brace cannot break anything, and refusing it would be the app inventing
    a rule and enforcing it against a man who typed a bracket."""
    r = client.put(f"/prompts/slices/{SID}", json={"text": "Χρησιμοποίησε το σχήμα { C }"})
    assert r.status_code == 200
    assert r.json()["effective"] == "Χρησιμοποίησε το σχήμα { C }"


def test_an_empty_replace_is_rejected_because_reset_is_the_explicit_act(client):
    r = client.put(f"/prompts/slices/{SID}", json={"text": "   "})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "empty"


def test_an_over_long_edit_is_rejected(client):
    r = client.put(f"/prompts/slices/{SID}", json={"text": "α" * 5000})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "too_long"
    assert r.json()["detail"]["max_chars"] == registry.SLICES[SID][1].max_chars


def test_a_rejected_edit_leaves_the_stored_text_untouched(client):
    client.put(f"/prompts/slices/{SID}", json={"text": "Καλό κείμενο."})
    client.put(f"/prompts/slices/{SID}", json={"text": ""})
    assert client.get(f"/prompts/{OWNER}").json()["slices"][0]["effective"] == "Καλό κείμενο."


def test_an_unknown_slice_is_a_code_not_a_crash(client):
    """The brief drafted this route with `lesson.draft.length`, a slice the spec's
    own audit rejected. The honest answer to an id that does not exist is a code,
    not an invented slice."""
    r = client.put("/prompts/slices/lesson.draft.length", json={"text": "Make it long."})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_slice"


def test_an_unknown_prompt_is_a_code_not_a_crash(client):
    r = client.get("/prompts/chat.nonexistent")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_prompt"


def test_every_failure_carries_a_machine_readable_code(client):
    """The tutor never sees JSON, a stack trace, a status code, or an
    untranslated English string. The `code` is the whole contract with the web.
    """
    failures = [
        client.get("/prompts/nope.nope"),
        client.put("/prompts/slices/nope.nope", json={"text": "x"}),
        client.delete("/prompts/slices/nope.nope"),
        client.get("/prompts/slices/nope.nope/history"),
        client.put(f"/prompts/slices/{SID}", json={"text": ""}),
        client.put(f"/prompts/slices/{SID}", json={"text": "α" * 5000}),
    ]
    for r in failures:
        assert r.status_code in (404, 422), r.status_code
        assert isinstance(r.json()["detail"], dict), r.json()
        assert r.json()["detail"]["code"], r.json()


# ---------------------------------------------------------------------------
# History — the price of allowing edits at all
# ---------------------------------------------------------------------------


def test_history_lets_a_bad_edit_be_recovered(client):
    client.put(f"/prompts/slices/{SID}", json={"text": "first"})
    client.put(f"/prompts/slices/{SID}", json={"text": "second"})
    hist = client.get(f"/prompts/slices/{SID}/history").json()
    assert [h["text"] for h in hist] == ["first"]


def test_history_is_newest_first(client):
    for t in ("first", "second", "third"):
        client.put(f"/prompts/slices/{SID}", json={"text": t})
    hist = client.get(f"/prompts/slices/{SID}/history").json()
    assert [h["text"] for h in hist] == ["second", "first"]


def test_the_first_write_snapshots_nothing(client):
    """There is no previous text to lose: the thing it replaced is the code
    default, which is in git and is what Reset restores."""
    client.put(f"/prompts/slices/{SID}", json={"text": "first"})
    assert client.get(f"/prompts/slices/{SID}/history").json() == []


def test_reset_snapshots_the_text_it_destroys(client):
    """Reset is the ONLY operation that destroys text that exists nowhere else —
    a PUT's previous text is snapshotted, and the default is in git. Without this,
    the one irreversible button on the page is the one labelled 'Επαναφορά'.
    """
    client.put(f"/prompts/slices/{SID}", json={"text": "hours of his thinking"})
    client.delete(f"/prompts/slices/{SID}")
    hist = client.get(f"/prompts/slices/{SID}/history").json()
    assert [h["text"] for h in hist] == ["hours of his thinking"]


def test_resetting_something_that_was_never_overridden_is_not_an_error(client):
    """It is already at the default. Saying so with a 404 would put an error in
    front of a tutor whose wish has been granted."""
    r = client.delete(f"/prompts/slices/{SID}")
    assert r.status_code == 200
    assert r.json()["has_override"] is False
    assert client.get(f"/prompts/slices/{SID}/history").json() == []


# ---------------------------------------------------------------------------
# Reading is the feature
# ---------------------------------------------------------------------------


def test_a_locked_prompt_exposes_no_editable_slice(client):
    """SYSTEM_PROMPT is 1,301 chars of guards, each added because something broke.
    It is readable and not editable."""
    body = client.get("/prompts/chat.system").json()
    assert body["text"], "must be readable"
    assert body["slices"] == [] or all(s["kind"] == "append" for s in body["slices"])


def test_every_registered_prompt_is_listed_and_readable(client):
    listed = client.get("/prompts").json()
    assert {p["id"] for p in listed} == set(registry.REGISTRY)
    for p in listed:
        assert p["title_el"] and p["what_it_does_el"] and p["when_it_runs_el"]


def test_the_list_keeps_registration_order_so_chat_comes_first(client):
    """`by_flow`'s docstring: chat first, because it is the thing he uses every
    day. A dict-ordering accident would reshuffle his Settings page."""
    listed = client.get("/prompts").json()
    assert [p["id"] for p in listed] == list(registry.REGISTRY)
    assert listed[0]["flow"] == "chat"


def test_every_prompt_renders_without_a_student_a_library_or_a_hit(client):
    """The Settings page must not need a curriculum to draw a prompt."""
    for pid in registry.REGISTRY:
        r = client.get(f"/prompts/{pid}")
        assert r.status_code == 200, f"{pid}: {r.status_code}"
        assert r.json()["text"].strip(), f"{pid} rendered empty"


def test_the_list_reports_which_prompts_carry_an_override(client):
    def flag():
        return {p["id"]: p["has_override"] for p in client.get("/prompts").json()}

    assert flag()[OWNER] is False
    client.put(f"/prompts/slices/{SID}", json={"text": "Γράψε πιο απλά."})
    assert flag()[OWNER] is True


def test_the_locale_header_changes_the_rendered_prompt(client):
    """`language_directive` is the only locale-varying string in the app and it is
    injected into 8 prompts. A preview that ignores the header shows him a prompt
    the model never gets."""
    el = client.get("/prompts/chat.system", headers={"X-App-Locale": "el"}).json()["text"]
    en = client.get("/prompts/chat.system", headers={"X-App-Locale": "en"}).json()["text"]
    assert el != en


def test_the_messages_survive_the_flattening(client):
    """`text` is a presentational join; `messages` is what goes on the wire. P3
    needs the roles back to draw the boundaries."""
    body = client.get("/prompts/curriculum.library").json()
    assert body["messages"], "no messages"
    assert all(m["role"] and m["content"] for m in body["messages"])
    assert any(m["cached"] for m in body["messages"]), "the cache breakpoint vanished"


# ---------------------------------------------------------------------------
# Wasted LLM spend is the top severity class
# ---------------------------------------------------------------------------


def test_editing_a_prefix_slice_reports_its_one_off_cache_cost(client):
    """CURRICULUM_SYSTEM is INSIDE the cached prefix. An edit re-mints it once.
    corpus.py's whole premise is that a cache mistake is invisible until the
    invoice arrives a month later — so this is SHOWN, not hidden."""
    body = client.get("/prompts/curriculum.system").json()
    assert body["cache_cost_warning"] is True


def test_a_prompt_outside_the_cached_prefix_does_not_cry_wolf(client):
    """A warning on every card is a warning on no card."""
    body = client.get("/prompts/chat.system").json()
    assert body["cache_cost_warning"] is False


def test_the_student_pitch_is_free_to_edit_because_it_lands_in_the_tail(client):
    """`draft.py:147` appends the student brief inside the block marked "volatile,
    and strictly after the cache breakpoint". Editing it costs nothing, and
    telling him it costs $2 would train him to ignore the warning that does."""
    body = client.get(f"/prompts/{OWNER}").json()
    assert body["cache_cost_warning"] is False
    assert body["slices"][0]["cache_cost_warning"] is False


# ---------------------------------------------------------------------------
# The registry's own contracts, now that P2 depends on them
# ---------------------------------------------------------------------------


def test_resolve_returns_the_code_default_with_no_database_at_all():
    """`db=None` is the honest 'code defaults only' render — the path P1's own
    tests take. It must not reach for a session."""
    assert registry.resolve(None, SID) is STUDENT_PITCH


def test_the_slice_map_and_the_registry_cannot_disagree():
    for sid, (entry, sl) in registry.SLICES.items():
        assert sl.id == sid
        assert sl in registry.REGISTRY[entry.id].slices


def test_every_registered_slice_id_fits_its_column():
    """`varchar(80)`. Slice ids are OURS, not input — they cannot be wrong at
    request time, only at authoring time, so this is the layer to catch it: a
    `DataError` on save would reach the tutor as a 500 for a mistake made months
    earlier in a file he has never seen.
    """
    for sid in registry.SLICES:
        assert len(sid) <= SLICE_ID_LEN, f"{sid!r} is {len(sid)} chars"


def test_an_unknown_slice_id_raises_rather_than_inventing_a_default():
    with pytest.raises(KeyError):
        registry.resolve(None, "nope.nope")
