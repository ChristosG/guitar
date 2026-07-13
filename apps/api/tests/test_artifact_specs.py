"""Tests for `app.artifacts.specs` (per-kind Pydantic spec validators, Plan 4
Task 1) and the `Artifact` model's DB round-trip.

Mirrors `test_students_api.py`'s skip-guard + `setup_module` pattern (not
`test_models_roundtrip.py`'s older standalone one) since that's this
codebase's current convention for DB-touching test modules.
"""
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

# Skip cleanly (not error) when no DB is reachable — mirrors test_students_api.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.artifacts.specs import SPECS, validate_spec
from app.models.artifact import Artifact
from app.models.block import Block


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


# ---------------------------------------------------------------------------
# validate_spec / SPECS
# ---------------------------------------------------------------------------

def test_validate_chord_diagram_returns_normalized_dict():
    spec = {"name": "G", "frets": [3, 2, 0, 0, 0, 3], "fingers": [3, 2, 0, 0, 0, 4]}
    out = validate_spec("chord_diagram", spec)
    assert out["name"] == "G"
    assert out["frets"] == [3, 2, 0, 0, 0, 3]
    assert out["fingers"] == [3, 2, 0, 0, 0, 4]
    assert out["baseFret"] == 1   # default filled in -> genuinely "normalized"
    assert out["barres"] == []


def test_validate_chord_diagram_wrong_frets_length_raises():
    spec = {"name": "G", "frets": [3, 2, 0, 0, 0], "fingers": [3, 2, 0, 0, 0, 4]}
    with pytest.raises(ValidationError):
        validate_spec("chord_diagram", spec)


def test_validate_chord_diagram_finger_out_of_range_raises():
    spec = {"name": "G", "frets": [3, 2, 0, 0, 0, 3], "fingers": [3, 2, 0, 0, 0, 5]}
    with pytest.raises(ValidationError):
        validate_spec("chord_diagram", spec)


def test_validate_chord_diagram_base_fret_zero_raises():
    """Regression guard for `ChordDiagramSpec.baseFret`'s `ge=1` constraint:
    `baseFret: 0` is schema-shaped (a plain int) but not renderable — Task
    5's real end-to-end LLM generation produced exactly this value, and the
    client svguitar renderer throws ("Position cannot be less than 1") for
    anything under 1. Without this constraint (and this test guarding it)
    such a spec would validate fine and only fail much later, client-side.
    """
    spec = {
        "name": "G", "frets": [3, 2, 0, 0, 0, 3], "fingers": [3, 2, 0, 0, 0, 4],
        "baseFret": 0,
    }
    with pytest.raises(ValidationError):
        validate_spec("chord_diagram", spec)


def test_validate_amp_settings_dial_over_10_raises():
    spec = {"amp": "Fender Twin", "dials": [{"label": "Gain", "value": 11}]}
    with pytest.raises(ValidationError):
        validate_spec("amp_settings", spec)


def test_validate_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        validate_spec("banjo_diagram", {})


def test_validate_tone_recipe_required_fields():
    spec = {
        "guitar": "Fender Stratocaster",
        "amp": "Fender Twin Reverb",
        "chain": "guitar -> amp",
    }
    out = validate_spec("tone_recipe", spec)
    assert out["guitar"] == "Fender Stratocaster"
    assert out["amp"] == "Fender Twin Reverb"
    assert out["chain"] == "guitar -> amp"
    assert out["listen"] == []          # default filled in
    assert out["artist"] is None


def test_validate_tab_empty_alphatex_raises():
    with pytest.raises(ValidationError):
        validate_spec("tab", {"alphaTex": "   "})


def test_validate_tab_plain_english_label_raises():
    """The exact Plan 11 live-acceptance bug: the model filled `alphaTex`
    with a plain-English LABEL ("G Major Scale Tab") instead of real
    alphaTex notation. This was schema-valid under the old "non-empty
    string" validator and reached AlphaTab client-side, which throws "No
    alphaTex data found" — a schema-valid but musically-empty artifact the
    tutor clicks into and sees broken. Must be rejected here instead.
    """
    with pytest.raises(ValidationError):
        validate_spec("tab", {"alphaTex": "G Major Scale Tab"})


def test_validate_tab_real_alphatex_accepts():
    """Confirmed-renders alphaTex (verified live against the vendored
    AlphaTab build — 2 bars, real notation + tab — see this task's report):
    a G major scale run, `fret.string.duration` note tokens separated by
    spaces, bars separated by `|`.
    """
    real_alphatex = "3.6.4 5.6.4 2.5.4 3.5.4 | 5.5.4 2.4.4 4.4.4 5.4.4"
    out = validate_spec("tab", {"alphaTex": real_alphatex})
    assert out["alphaTex"] == real_alphatex


def test_validate_signal_chain_requires_at_least_one_node():
    with pytest.raises(ValidationError):
        validate_spec("signal_chain", {"nodes": []})


# ---------------------------------------------------------------------------
# Plan 12 Task 5 (G6): "substantively empty" specs must fail loudly
# ---------------------------------------------------------------------------
#
# Chris: "it generated nothing i could only see a 'TAB' component" — a spec
# that is schema-valid (every field present, every type/range check passes)
# but carries no actual musical content must be REJECTED here, the same
# class of bug as `TabSpec.alphaTex` accepting a plain-English label (Plan
# 11) — just the broader case, across every other kind that can go
# schema-valid-but-empty. One test per kind guarded.

def test_validate_chord_diagram_all_muted_raises():
    """`frets` all `-1` (every string muted) is schema-shaped (6 ints,
    each >= -1) but describes NO chord at all — nothing for the tutor to
    play. A real chord always has at least one sounded string.
    """
    spec = {"name": "G", "frets": [-1, -1, -1, -1, -1, -1], "fingers": [0, 0, 0, 0, 0, 0]}
    with pytest.raises(ValidationError):
        validate_spec("chord_diagram", spec)


def test_validate_scale_diagram_empty_positions_raises():
    """`positions: []` was previously schema-valid (no `min_length`) — a
    scale diagram with no positions has nothing to draw on the fretboard.
    """
    with pytest.raises(ValidationError):
        validate_spec("scale_diagram", {"name": "G major", "root": "G", "positions": []})


def test_validate_scale_diagram_real_positions_accepts():
    spec = {
        "name": "G major", "root": "G",
        "positions": [{"string": 6, "fret": 3, "degree": "1"}],
    }
    out = validate_spec("scale_diagram", spec)
    assert out["positions"] == [{"string": 6, "fret": 3, "degree": "1"}]


def test_validate_signal_chain_all_blank_labels_raises():
    """`nodes` non-empty (passes the existing `min_length=1`) but every
    node's `label` is blank — schema-shaped (a `str`, and `""` is a valid
    `str`), but there is nothing to actually show in the chain diagram.
    """
    with pytest.raises(ValidationError):
        validate_spec("signal_chain", {"nodes": [{"label": ""}, {"label": "   "}]})


def test_validate_amp_settings_empty_dials_raises():
    """`dials: []` was previously schema-valid (no `min_length`) — an amp
    settings card with no dials has no settings at all.
    """
    with pytest.raises(ValidationError):
        validate_spec("amp_settings", {"amp": "Fender Twin", "dials": []})


def test_validate_tone_recipe_blank_required_fields_raises():
    """`guitar`/`amp`/`chain` are required `str` fields, but `""` satisfies
    "is a string" fine — a tone recipe with blank core settings is exactly
    the "succeeded but contains nothing" lie this task targets.
    """
    spec = {"guitar": "  ", "amp": "Fender Twin Reverb", "chain": "guitar -> amp"}
    with pytest.raises(ValidationError):
        validate_spec("tone_recipe", spec)


def test_validate_gear_card_empty_specs_raises():
    """`specs: []` was previously schema-valid (no `min_length`) — a gear
    card with no spec rows has nothing to show.
    """
    with pytest.raises(ValidationError):
        validate_spec("gear_card", {"name": "Fender Twin Reverb", "kind": "amp", "specs": []})


def test_validate_gear_card_real_specs_accepts():
    spec = {
        "name": "Fender Twin Reverb", "kind": "amp",
        "specs": [{"k": "Wattage", "v": "85W"}],
    }
    out = validate_spec("gear_card", spec)
    assert out["specs"] == [{"k": "Wattage", "v": "85W"}]


def test_specs_registry_has_all_seven_kinds():
    assert set(SPECS) == {
        "chord_diagram", "scale_diagram", "tab", "signal_chain",
        "amp_settings", "tone_recipe", "gear_card",
    }


# ---------------------------------------------------------------------------
# Artifact model round-trip
# ---------------------------------------------------------------------------

def test_artifact_persists_and_rereads_with_block_id():
    db = SessionLocal()
    try:
        block = Block(kind="lesson", title="Open G chord", order=0, language="en")
        db.add(block); db.flush()

        spec = validate_spec("chord_diagram", {
            "name": "G", "frets": [3, 2, 0, 0, 0, 3], "fingers": [3, 2, 0, 0, 0, 4],
        })
        artifact = Artifact(kind="chord_diagram", spec=spec, title="G major (open)",
                             tags=["beginner", "open-chord"], block_id=block.id)
        db.add(artifact); db.commit()
        artifact_id, block_id = artifact.id, block.id
    finally:
        db.close()

    # Fresh session -> a genuine DB round-trip, not the identity-map object
    # reused under expire_on_commit=False (mirrors test_models_roundtrip.py).
    db2 = SessionLocal()
    try:
        got = db2.get(Artifact, artifact_id)
        assert got is not None
        assert got.block_id == block_id
        assert got.kind == "chord_diagram"
        assert got.spec["name"] == "G"                       # JSON round-trip
        assert got.tags == ["beginner", "open-chord"]         # JSON list round-trip
        assert got.source == "ai"                             # column default applied
    finally:
        db2.close()


def test_artifact_defaults_tags_empty_and_block_id_nullable():
    db = SessionLocal()
    try:
        artifact = Artifact(kind="tab", spec={"alphaTex": "."}, title="Warm-up lick")
        db.add(artifact); db.commit()
        db.refresh(artifact)
        assert artifact.tags == []       # default=list, not a shared mutable default
        assert artifact.source == "ai"
        assert artifact.block_id is None
    finally:
        db.close()
