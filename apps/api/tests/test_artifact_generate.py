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
from app.artifacts.generate import _build_messages, derive_title, generate_artifact
from app.brain.retrieve import Hit
from app.db import Base, SessionLocal, engine
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
    assert user == "an E minor pentatonic lick"
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
