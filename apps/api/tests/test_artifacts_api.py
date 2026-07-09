"""Integration tests for the `/artifacts` HTTP routes: create-from-spec,
retrieval + filters, delete, and (one live test) LLM generation.

Mirrors `test_curriculum_api.py`'s split: fast DB-only tests (not marked
`@pytest.mark.integration` — "that marker means 'hits live vLLM' per
pyproject.toml") build fixtures directly via ORM/HTTP, and exactly the two
tests that drive `POST /artifacts/generate` against the real LLM+embed stack
ARE marked `@pytest.mark.integration`.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.artifacts.generate import TITLE_MAX_LEN
from app.brain.ingest import IngestPayload, ingest_source
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.knowledge import KnowledgeSource

# Skip cleanly (not error) when no DB is reachable — mirrors test_curriculum_api.py.
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


client = TestClient(app)

_VALID_CHORD_SPEC = {"name": "G", "frets": [3, 2, 0, 0, 0, 3], "fingers": [3, 2, 0, 0, 0, 4]}


def _create_block(title: str = "Open G chord lesson") -> Block:
    db = SessionLocal()
    try:
        block = Block(kind="lesson", title=title, order=0, language="en")
        db.add(block)
        db.commit()
        db.refresh(block)
        return block
    finally:
        db.close()


# --- POST /artifacts (create-from-spec) -------------------------------------

def test_create_artifact_round_trips_and_normalizes_defaults():
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "G major (open)",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "chord_diagram"
    assert body["title"] == "G major (open)"
    assert body["spec"]["name"] == "G"
    assert body["spec"]["baseFret"] == 1     # default filled in -> genuinely normalized
    assert body["spec"]["barres"] == []
    assert body["source"] == "ai"
    assert body["tags"] == []
    assert body["block_id"] is None
    assert body["id"]

    # Round-trip via a separate request — genuinely persisted, not just echoed.
    r2 = client.get(f"/artifacts/{body['id']}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["spec"]["name"] == "G"


def test_create_artifact_invalid_spec_returns_422():
    bad_spec = {"name": "G", "frets": [3, 2, 0, 0, 0], "fingers": [3, 2, 0, 0, 0, 4]}  # 5 frets
    r = client.post("/artifacts", json={"kind": "chord_diagram", "spec": bad_spec, "title": "Bad G"})
    assert r.status_code == 422, r.text


def test_create_artifact_unknown_kind_returns_422():
    r = client.post("/artifacts", json={"kind": "banjo_diagram", "spec": {}, "title": "?"})
    assert r.status_code == 422, r.text


def test_create_artifact_derives_title_when_title_omitted():
    r = client.post("/artifacts", json={"kind": "chord_diagram", "spec": _VALID_CHORD_SPEC})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "G"          # derived from spec["name"]


def test_create_artifact_rejects_empty_string_title():
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "",
    })
    assert r.status_code == 422, r.text


def test_create_artifact_truncates_title_over_column_limit():
    """Fix 1 (Plan 4 final review): a client-supplied `title` longer than
    `Artifact.title`'s column cap (String(300)) must be clamped the same
    way `derive_title` clamps a generated one — otherwise it reaches
    Postgres uncaught (StringDataRightTruncation, not a ValueError, so
    create_artifact's own `except ValueError` never sees it) and the
    request 500s instead of succeeding.
    """
    long_title = "T" * (TITLE_MAX_LEN + 100)
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": long_title,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == long_title[:TITLE_MAX_LEN]
    assert len(body["title"]) == TITLE_MAX_LEN

    # Round-trip via a separate GET — genuinely persisted at the clamped
    # length, not just echoed back by the create response.
    r2 = client.get(f"/artifacts/{body['id']}")
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["title"]) == TITLE_MAX_LEN


def test_create_artifact_with_valid_block_id_persists_link():
    block = _create_block()
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "G major (open)",
        "block_id": str(block.id),
    })
    assert r.status_code == 200, r.text
    assert r.json()["block_id"] == str(block.id)


def test_create_artifact_missing_block_id_404s():
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "G",
        "block_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 404, r.text


def test_create_artifact_with_tags():
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "G",
        "tags": ["beginner", "open-chord"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["beginner", "open-chord"]


# --- GET /artifacts/{id} -----------------------------------------------------

def test_get_artifact_404_for_unknown_id():
    r = client.get("/artifacts/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- GET /artifacts (filters) ------------------------------------------------

def test_list_artifacts_filters_by_kind():
    tag = uuid.uuid4().hex[:8]
    chord_title = f"Chord {tag}"
    tab_title = f"Tab {tag}"
    r1 = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": chord_title,
    })
    r2 = client.post("/artifacts", json={
        "kind": "tab", "spec": {"alphaTex": "."}, "title": tab_title,
    })
    assert r1.status_code == 200 and r2.status_code == 200

    r = client.get("/artifacts", params={"kind": "chord_diagram"})
    assert r.status_code == 200, r.text
    titles = {a["title"] for a in r.json()}
    assert chord_title in titles
    assert tab_title not in titles


def test_list_artifacts_filters_by_block_id():
    block_a = _create_block("Block A")
    block_b = _create_block("Block B")
    ra = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "On A",
        "block_id": str(block_a.id),
    })
    rb = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "On B",
        "block_id": str(block_b.id),
    })
    assert ra.status_code == 200 and rb.status_code == 200

    r = client.get("/artifacts", params={"block_id": str(block_a.id)})
    assert r.status_code == 200, r.text
    titles = {a["title"] for a in r.json()}
    assert titles == {"On A"}


# --- DELETE /artifacts/{id} ---------------------------------------------------

def test_delete_artifact_204_then_get_404s():
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "To delete",
    })
    artifact_id = r.json()["id"]

    rd = client.delete(f"/artifacts/{artifact_id}")
    assert rd.status_code == 204

    rg = client.get(f"/artifacts/{artifact_id}")
    assert rg.status_code == 404


def test_delete_unknown_artifact_404s():
    r = client.delete("/artifacts/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_delete_artifact_clears_block_id_on_the_block_but_does_not_delete_block():
    """`Artifact.block_id` is `ondelete=SET NULL` — deleting the ARTIFACT
    itself doesn't touch the Block at all (this is the reverse direction:
    that FK behavior fires when the BLOCK is deleted, not the artifact); this
    test just pins that deleting an artifact attached to a block leaves the
    block completely untouched.
    """
    block = _create_block("Still here")
    r = client.post("/artifacts", json={
        "kind": "chord_diagram", "spec": _VALID_CHORD_SPEC, "title": "Attached",
        "block_id": str(block.id),
    })
    artifact_id = r.json()["id"]

    client.delete(f"/artifacts/{artifact_id}")

    r2 = client.get(f"/blocks/{block.id}")
    assert r2.status_code == 200, r2.text


# --- POST /artifacts/generate: block_id 404 (fast — before any LLM call) ----

def test_generate_artifact_endpoint_missing_block_id_404s_before_calling_generate(monkeypatch):
    import app.routers.artifacts as artifacts_router

    def _fail_if_called(*a, **k):
        raise AssertionError("generate_artifact must not be called for an unknown block_id")

    monkeypatch.setattr(artifacts_router, "generate_artifact", _fail_if_called)

    r = client.post("/artifacts/generate", json={
        "kind": "chord_diagram", "prompt": "G major open chord",
        "block_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 404, r.text


# --- POST /artifacts/generate (live) -----------------------------------------

@pytest.mark.integration
def test_generate_artifact_endpoint_chord_diagram_returns_plausible_em():
    """Hits the live LLM stack — allow it to be slow (guided-JSON generation
    measured at 49-179s/call, see Plan 3 Task 2's report; same underlying
    `QwenVLLM.guided_json`).
    """
    r = client.post("/artifacts/generate", json={
        "kind": "chord_diagram", "prompt": "E minor open chord",
    })
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["kind"] == "chord_diagram"
    assert body["source"] == "ai"
    spec = body["spec"]

    assert spec["name"]
    name_lower = spec["name"].lower()
    assert "em" in name_lower or "minor" in name_lower or "min" in name_lower, (
        f"expected an E-minor-ish name, got {spec['name']!r}"
    )

    frets = spec["frets"]
    assert len(frets) == 6
    assert frets[0] == 0, f"expected low-E open (0), got frets={frets}"
    assert frets[1] == 2 and frets[2] == 2, f"expected A/D fretted at 2, got frets={frets}"

    print("\nGenerated Em chord_diagram spec:", spec)

    # Persisted for real — round-trips via a separate GET.
    r2 = client.get(f"/artifacts/{body['id']}")
    assert r2.status_code == 200
    assert r2.json()["spec"]["name"] == spec["name"]


@pytest.mark.integration
def test_generate_artifact_endpoint_tone_recipe_grounded_has_required_fields():
    """Seeds one real tone-related KnowledgeSource so `ground=True` actually
    has something to retrieve — isolation comes from `conftest.py`'s
    autouse per-test TRUNCATE fixture (this module's other tests don't
    leave stray KnowledgeSource rows behind), not a uuid-tagged domain: this
    call has no `domain` param to scope by (`generate_artifact`'s exact
    signature per this task's brief takes no `domain` kwarg).
    """
    tone_text = (
        "Stevie Ray Vaughan's tone came from a Fender Stratocaster into a "
        "cranked tube amplifier (a modified Fender Vibroverb/Super Reverb "
        "stack), with an Ibanez Tube Screamer overdrive pedal pushing the "
        "front end for extra sustain and grit. Heavy-gauge strings and an "
        "aggressive picking hand gave the tone its thick, dynamic snap."
    )
    db = SessionLocal()
    try:
        source = KnowledgeSource(type="text", title="SRV Tone", language="en", domain="tone")
        db.add(source)
        db.commit()
        ingest_source(db, source.id, IngestPayload(kind="text", text=tone_text))
        db.refresh(source)
        assert source.status == "ready", f"seed ingestion failed: {source.status}/{source.error}"
    finally:
        db.close()

    r = client.post("/artifacts/generate", json={
        "kind": "tone_recipe", "prompt": "Stevie Ray Vaughan Texas Flood", "ground": True,
    })
    assert r.status_code == 200, r.text
    spec = r.json()["spec"]

    assert spec["guitar"] and spec["guitar"].strip()
    assert spec["amp"] and spec["amp"].strip()
    assert spec["chain"] and spec["chain"].strip()

    print("\nGenerated grounded tone_recipe spec:", spec)
