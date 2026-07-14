"""Tests for `app.artifacts.generate`: the guided-JSON artifact-spec
generation pipeline (Plan 4 Task 4).

`test_build_messages_...`/`test_derive_title_...` are fast, pure-function
unit tests with no DB and no model — mirrors `test_retrieve.py`'s own
`test_build_grounded_messages_...` convention ("a fast, pure-function unit
test with no DB and no model"). `test_generate_artifact_...` needs a real DB
(it persists via `db.commit()`) but stubs `get_provider`/`search` so it never
touches the live LLM/embed stack — not marked `@pytest.mark.integration` for
that reason, mirroring `test_curriculum_generate_errors.py`'s own rationale
for its monkeypatched tests (only its DB dependency differs: those tests
never even construct a Session, these do).
"""
import pytest
from pydantic import ValidationError
from sqlalchemy import text

import app.artifacts.generate as artifact_generate
from app.artifacts.generate import TITLE_MAX_LEN, _build_messages, derive_title, generate_artifact
from app.brain.retrieve import Hit
from app.db import Base, SessionLocal, engine
from app.i18n import DEFAULT_LOCALE, answer_in
from app.llm.errors import GuidedJSONError
from app.models.artifact import Artifact

# Skip cleanly (not error) when no DB is reachable — mirrors test_retrieve.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


# ---------------------------------------------------------------------------
# _build_messages (pure)
# ---------------------------------------------------------------------------

def test_build_messages_system_prompt_names_the_kind_and_forbids_prose():
    messages = _build_messages(kind="chord_diagram", prompt="G major open chord", hits=[])
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert "chord_diagram" in system
    assert "no prose" in system.lower()


def test_build_messages_without_hits_uses_bare_prompt():
    messages = _build_messages(kind="tab", prompt="an E minor pentatonic lick", hits=[])
    user = messages[1]["content"]
    # The prompt, plus the tail language reminder every builder now appends
    # (Plan 13, Stage 5.3) — and nothing else: no CONTEXT block when there are
    # no hits.
    assert user == f"an E minor pentatonic lick\n\n{answer_in(DEFAULT_LOCALE)}"
    assert "CONTEXT" not in user


def test_build_messages_with_hits_folds_in_numbered_context():
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Tone", text="a tube amp breaks up warmly",
            section_path=None, page=None, score=0.9),
        Hit(chunk_id="c2", source_id="s1", source_title="Tone", text="single coils sound bright",
            section_path=None, page=None, score=0.7),
    ]
    messages = _build_messages(kind="tone_recipe", prompt="SRV Texas Flood", hits=hits)
    user = messages[1]["content"]
    assert "SRV Texas Flood" in user
    assert "CONTEXT" in user
    assert "[1] a tube amp breaks up warmly" in user
    assert "[2] single coils sound bright" in user


def test_build_messages_tab_includes_a_real_worked_alphatex_example():
    """Plan 11 Task 5's prompt half of the fix: without a concrete example of
    real alphaTex syntax, the model has no way to know `alphaTex` wants
    `fret.string.duration` note tokens rather than a plain-English label
    (the exact bug — see `test_artifact_specs.
    test_validate_tab_plain_english_label_raises`). This pins that the
    worked example (verified to actually render — see this task's report)
    reaches the system prompt for kind="tab" specifically.
    """
    messages = _build_messages(kind="tab", prompt="G major scale", hits=[])
    system = messages[0]["content"]
    assert "fret.string.duration" in system
    assert "3.6.4" in system  # the concrete worked example, not just prose


def test_build_messages_other_kinds_do_not_get_the_tab_example():
    messages = _build_messages(kind="chord_diagram", prompt="G major open chord", hits=[])
    system = messages[0]["content"]
    assert "fret.string.duration" not in system


def test_build_messages_repair_error_appends_previous_failure():
    messages = _build_messages(
        kind="chord_diagram", prompt="G major open chord", hits=[],
        repair_error="frets: List should have exactly 6 items",
    )
    user = messages[1]["content"]
    assert "G major open chord" in user
    assert "invalid" in user.lower()
    assert "frets: List should have exactly 6 items" in user


# ---------------------------------------------------------------------------
# derive_title (pure)
# ---------------------------------------------------------------------------

def test_derive_title_prefers_spec_name_field():
    assert derive_title("chord_diagram", {"name": "G"}, "a prompt") == "G"


def test_derive_title_uses_title_field_when_no_name():
    assert derive_title("tab", {"title": "Warm-up lick"}, "a prompt") == "Warm-up lick"


def test_derive_title_tone_recipe_combines_artist_and_song():
    spec = {"artist": "Stevie Ray Vaughan", "song": "Texas Flood"}
    assert derive_title("tone_recipe", spec, None) == "Stevie Ray Vaughan — Texas Flood"


def test_derive_title_tone_recipe_falls_back_to_artist_only():
    assert derive_title("tone_recipe", {"artist": "SRV", "song": None}, None) == "SRV"


def test_derive_title_falls_back_to_prompt_when_no_name_like_field():
    spec = {"nodes": [{"label": "guitar"}, {"label": "amp"}]}  # signal_chain: no name field
    assert derive_title("signal_chain", spec, "Blues clean chain") == "Blues clean chain"


def test_derive_title_falls_back_to_generic_label_when_nothing_available():
    assert derive_title("signal_chain", {"nodes": []}, None) == "signal_chain artifact"


def test_derive_title_truncates_overlong_spec_name_field():
    """Fix 1 (Plan 4 final review): an LLM-emitted spec `name`/`title` can
    exceed `Artifact.title`'s column cap just as easily as a long prompt
    can — previously only the prompt fallback below was truncated, so a
    long `name` reached Postgres uncaught (StringDataRightTruncation, not a
    ValueError, so callers' `except ValueError` never sees it).
    """
    long_name = "G major " * 60  # 480 chars, well past TITLE_MAX_LEN
    assert len(long_name) > TITLE_MAX_LEN
    title = derive_title("chord_diagram", {"name": long_name}, None)
    assert title == long_name[:TITLE_MAX_LEN]
    assert len(title) == TITLE_MAX_LEN


def test_derive_title_truncates_overlong_tone_recipe_artist_song_join():
    spec = {"artist": "Stevie Ray Vaughan " * 10, "song": "Texas Flood " * 10}
    title = derive_title("tone_recipe", spec, None)
    assert len(title) == TITLE_MAX_LEN


# ---------------------------------------------------------------------------
# generate_artifact: repair-retry ("the critical design point") + grounding,
# stubbed provider/search — no live LLM/embed call.
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Stand-in for `LLMProvider`: returns each of `responses` in turn from
    `guided_json`, recording every call's `messages` so a test can assert on
    the retry's repair-prompt content.
    """
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls: list[list[dict]] = []

    def guided_json(self, messages, schema, *, temperature=0.2):
        self.calls.append(messages)
        return next(self._responses)


_VALID_G = {"name": "G", "frets": [3, 2, 0, 0, 0, 3], "fingers": [3, 2, 0, 0, 0, 4]}
_INVALID_G_SHORT_FRETS = {"name": "G", "frets": [3, 2, 0, 0, 0], "fingers": [3, 2, 0, 0, 0, 4]}


def test_generate_artifact_repairs_an_invalid_first_attempt(monkeypatch):
    """guided_json's decoding constrains *shape* but the brief's own
    critical-design-point warns array length constraints (`minItems`) aren't
    reliably enforced — this pins the recovery path: a first response that
    fails `validate_spec` triggers exactly one repair retry (re-prompted
    with the validation error), and the second (valid) response is what
    actually gets persisted.
    """
    fake = _FakeProvider([_INVALID_G_SHORT_FRETS, _VALID_G])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        artifact = generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
        assert artifact.spec["frets"] == [3, 2, 0, 0, 0, 3]
        assert artifact.source == "ai"
    finally:
        db.close()

    assert len(fake.calls) == 2  # exactly one repair retry, not an open-ended loop
    repair_user_message = fake.calls[1][1]["content"]
    assert "invalid" in repair_user_message.lower()


def test_generate_artifact_raises_after_repair_also_fails(monkeypatch):
    """A second consecutive failure is NOT retried again — it propagates
    (the router maps this to a 422) rather than looping further.
    """
    fake = _FakeProvider([_INVALID_G_SHORT_FRETS, _INVALID_G_SHORT_FRETS])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        with pytest.raises(ValidationError):
            generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
    finally:
        db.close()

    assert len(fake.calls) == 2  # one initial attempt + one repair, no more


_TAB_PLAIN_ENGLISH_LABEL = {"alphaTex": "G Major Scale Tab"}  # the exact Plan 11 bug
_TAB_REAL_ALPHATEX = {
    "alphaTex": "3.6.4 5.6.4 2.5.4 3.5.4 | 5.5.4 2.4.4 4.4.4 5.4.4",
    "title": "G major scale",
}


def test_generate_artifact_tab_persists_a_real_alphatex_spec(monkeypatch):
    fake = _FakeProvider([_TAB_REAL_ALPHATEX])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        artifact = generate_artifact(db, kind="tab", prompt="G major scale")
        assert artifact.spec["alphaTex"] == _TAB_REAL_ALPHATEX["alphaTex"]
    finally:
        db.close()


def test_generate_artifact_tab_repairs_a_plain_english_label_into_real_alphatex(monkeypatch):
    """The repair-retry path (already exercised generically by
    `test_generate_artifact_repairs_an_invalid_first_attempt` above) applies
    just as well to a tab whose first attempt is a plain-English label
    instead of notation: `validate_spec` rejects it, one repair retry is
    made with the validation error folded back in, and a valid second
    attempt is what actually gets persisted.
    """
    fake = _FakeProvider([_TAB_PLAIN_ENGLISH_LABEL, _TAB_REAL_ALPHATEX])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        artifact = generate_artifact(db, kind="tab", prompt="G major scale")
        assert artifact.spec["alphaTex"] == _TAB_REAL_ALPHATEX["alphaTex"]
    finally:
        db.close()

    assert len(fake.calls) == 2
    repair_user_message = fake.calls[1][1]["content"]
    assert "invalid" in repair_user_message.lower()


def test_generate_artifact_tab_never_persists_a_broken_artifact_when_both_attempts_fail(monkeypatch):
    """THE bug this task fixes: a `generate_artifact(kind="tab")` call whose
    model output never becomes real notation (both the first attempt AND the
    repair retry are plain-English labels) must FAIL LOUDLY — raise, and
    persist NOTHING — rather than silently commit a schema-valid but
    musically-empty Artifact the tutor would click into and see AlphaTab's
    "No alphaTex data found" error. Worse than the old free-typed-ASCII bug
    in exactly the way the task brief describes: it would otherwise look
    like it worked.
    """
    fake = _FakeProvider([_TAB_PLAIN_ENGLISH_LABEL, _TAB_PLAIN_ENGLISH_LABEL])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        count_before = db.query(Artifact).count()
        with pytest.raises(ValidationError):
            generate_artifact(db, kind="tab", prompt="G major scale")
        count_after = db.query(Artifact).count()
    finally:
        db.close()

    assert count_after == count_before  # nothing persisted
    assert len(fake.calls) == 2  # one initial attempt + one repair, no more


# ---------------------------------------------------------------------------
# Plan 12 Task 5 (G6): "substantively empty" specs for OTHER kinds fail
# loudly through the SAME repair-retry / raise-uncaught path the tab tests
# above already pin — no new error-handling machinery, only the tighter
# `app.artifacts.specs` validators. One kind exercised end-to-end here
# (chord_diagram); `test_artifact_specs.py` covers every guarded kind's
# validator directly.
# ---------------------------------------------------------------------------

_CHORD_ALL_MUTED = {"name": "G", "frets": [-1, -1, -1, -1, -1, -1], "fingers": [0, 0, 0, 0, 0, 0]}


def test_generate_artifact_repairs_an_all_muted_chord_into_a_real_one(monkeypatch):
    fake = _FakeProvider([_CHORD_ALL_MUTED, _VALID_G])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        artifact = generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
        assert artifact.spec["frets"] == [3, 2, 0, 0, 0, 3]
    finally:
        db.close()

    assert len(fake.calls) == 2


def test_generate_artifact_never_persists_an_all_muted_chord_when_both_attempts_fail(monkeypatch):
    """Same invariant as the tab test above, for a different kind: a
    schema-valid-but-empty chord (every string muted) must fail loudly and
    persist nothing if the repair retry doesn't fix it either.
    """
    fake = _FakeProvider([_CHORD_ALL_MUTED, _CHORD_ALL_MUTED])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        count_before = db.query(Artifact).count()
        with pytest.raises(ValidationError):
            generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
        count_after = db.query(Artifact).count()
    finally:
        db.close()

    assert count_after == count_before
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Plan 12 Task 5 (G6): truncation/timeout must not be swallowed either
# ---------------------------------------------------------------------------
#
# Chris: the FIRST attempt "took too much time then generated nothing" — a
# plausible root cause is a `finish_reason == "length"` truncation getting
# silently swallowed and persisted as an empty artifact, which would be a
# WORSE bug than the validator gap above (no spec to even validate). Pins
# that `generate_artifact` does NOT catch/swallow `GuidedJSONError` (the
# typed error `LLMProvider.guided_json` already raises for exactly this —
# see `app.llm.qwen.QwenVLLM.guided_json`'s own docstring) — it must
# propagate uncaught, with NOTHING persisted, same as every other raised
# failure in this module (the router already maps this to a 502 "retry").

class _ProviderThatTruncates:
    """Mirrors what a REAL truncated `finish_reason == "length"` response
    looks like from the caller's side: `guided_json` itself detects this and
    raises `GuidedJSONError` (see `qwen.py`) — this fake reproduces that
    contract directly rather than re-implementing OpenAI response parsing.
    """
    def __init__(self):
        self.calls = 0

    def guided_json(self, messages, schema, *, temperature=0.2):
        self.calls += 1
        raise GuidedJSONError("guided_json: unusable response (finish_reason='length')")


def test_generate_artifact_propagates_truncation_without_persisting_or_repairing(monkeypatch):
    fake = _ProviderThatTruncates()
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        count_before = db.query(Artifact).count()
        with pytest.raises(GuidedJSONError):
            generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
        count_after = db.query(Artifact).count()
    finally:
        db.close()

    assert count_after == count_before  # nothing persisted
    # No repair retry either: a truncated response isn't a validation
    # failure `generate_artifact`'s repair path is built to fix (there's no
    # spec to repair), so it must not attempt a second call.
    assert fake.calls == 1


class _ProviderThatMustNotBeCalled:
    def guided_json(self, messages, schema, *, temperature=0.2):
        raise AssertionError("guided_json must not be called for an unknown kind")


def test_generate_artifact_unknown_kind_raises_without_calling_provider(monkeypatch):
    monkeypatch.setattr(artifact_generate, "get_provider", _ProviderThatMustNotBeCalled)

    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="unknown artifact kind"):
            generate_artifact(db, kind="banjo_diagram", prompt="anything")
    finally:
        db.close()


_VALID_TONE_RECIPE = {
    "guitar": "Fender Stratocaster", "amp": "Fender Twin Reverb", "chain": "guitar -> amp",
}


def test_generate_artifact_grounds_when_requested(monkeypatch):
    hits = [
        Hit(chunk_id="c1", source_id="s1", source_title="Tone", text="tube amps break up warmly",
            section_path=None, page=None, score=0.9),
    ]
    search_calls = []

    def _fake_search(db, query, *, k=8, **kwargs):
        search_calls.append((query, k))
        return hits

    fake = _FakeProvider([_VALID_TONE_RECIPE])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)
    monkeypatch.setattr(artifact_generate, "search", _fake_search)

    db = SessionLocal()
    try:
        generate_artifact(db, kind="tone_recipe", prompt="SRV Texas Flood", ground=True)
    finally:
        db.close()

    assert search_calls == [("SRV Texas Flood", 6)]
    user_message = fake.calls[0][1]["content"]
    assert "tube amps break up warmly" in user_message  # grounded context reached the prompt


def test_generate_artifact_skips_grounding_when_not_requested(monkeypatch):
    def _fail_if_called(*a, **k):
        raise AssertionError("search must not be called when ground=False")

    fake = _FakeProvider([_VALID_G])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)
    monkeypatch.setattr(artifact_generate, "search", _fail_if_called)

    db = SessionLocal()
    try:
        generate_artifact(db, kind="chord_diagram", prompt="G major open chord", ground=False)
    finally:
        db.close()


def test_generate_artifact_persists_block_id_and_derived_title(monkeypatch):
    fake = _FakeProvider([_VALID_G])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        artifact = generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
        assert artifact.title == "G"          # derived from spec["name"]
        assert artifact.block_id is None
        assert artifact.kind == "chord_diagram"
        artifact_id = artifact.id
    finally:
        db.close()

    # Fresh session — a genuine DB round-trip, not the identity-map object
    # reused under expire_on_commit=False (mirrors test_curriculum_generate.py).
    db2 = SessionLocal()
    try:
        got = db2.get(Artifact, artifact_id)
        assert got is not None
        assert got.spec["name"] == "G"
        assert got.source == "ai"
    finally:
        db2.close()


def test_generate_artifact_truncates_overlong_spec_name_title(monkeypatch):
    """Fix 1 (Plan 4 final review): `guided_json` only constrains output
    *shape*, not length (same "critical design point" as the repair-retry
    tests above) — a real chord `name` longer than `Artifact.title`'s
    column cap (String(300)) must be truncated before `db.commit()`, or the
    commit itself raises StringDataRightTruncation/DataError uncaught (not
    a ValueError, so this never reached the repair-retry or router error
    handling at all).
    """
    long_name = "G major " * 60  # 480 chars, well past TITLE_MAX_LEN
    overlong_spec = {**_VALID_G, "name": long_name}
    fake = _FakeProvider([overlong_spec])
    monkeypatch.setattr(artifact_generate, "get_provider", lambda: fake)

    db = SessionLocal()
    try:
        artifact = generate_artifact(db, kind="chord_diagram", prompt="G major open chord")
        assert artifact.title == long_name[:TITLE_MAX_LEN]
        assert len(artifact.title) == TITLE_MAX_LEN
        artifact_id = artifact.id
    finally:
        db.close()

    # Fresh session — confirms it's genuinely persisted at the clamped
    # length, not just an in-memory attribute the commit never actually wrote.
    db2 = SessionLocal()
    try:
        got = db2.get(Artifact, artifact_id)
        assert got is not None
        assert len(got.title) == TITLE_MAX_LEN
    finally:
        db2.close()
