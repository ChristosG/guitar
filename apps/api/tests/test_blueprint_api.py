"""Plan C, Task 4 — the `/blueprint/default` read/edit/reset API.

Follows `routers/prompts.py`'s `code`-error convention: a `BlueprintInvalid` becomes
`422 {"detail": {"code": ...}}`, exactly like `put_slice`. GET returns the RESOLVED
default plus `is_override` (whether a row exists), so the Settings UI can show "you've
customised this" and diff against `/blueprint/code-default`.
"""
from app.curriculum.blueprint import default_blueprint, validate_blueprint


def test_get_default_is_the_code_default_when_no_override(client):
    r = client.get("/blueprint/default")
    assert r.status_code == 200
    body = r.json()
    assert body["blueprint"] == default_blueprint()
    assert body["is_override"] is False


def test_get_code_default_is_always_the_code_default(client):
    r = client.get("/blueprint/code-default")
    assert r.status_code == 200
    assert r.json()["blueprint"] == default_blueprint()


def test_put_then_get_round_trips_the_custom_default(client):
    custom = default_blueprint()
    custom["sections"][0]["weight"] = 0.11
    custom["sections"].insert(1, {
        "key": "improv",
        "label": {"el": "Αυτοσχεδιασμός", "en": "Improvisation"},
        "description": "Improvise four bars over the lesson's key before theory.",
        "weight": 0.05, "kind": "prose", "audience": "student", "enabled": True,
    })

    put = client.put("/blueprint/default", json={"blueprint": custom})
    assert put.status_code == 200
    assert put.json()["is_override"] is True
    assert put.json()["blueprint"] == validate_blueprint(custom)

    got = client.get("/blueprint/default")
    assert got.json()["is_override"] is True
    assert got.json()["blueprint"] == validate_blueprint(custom)
    # The code default is unaffected — it is what Restore returns to.
    assert client.get("/blueprint/code-default").json()["blueprint"] == default_blueprint()


def test_put_an_invalid_blueprint_is_a_422_with_a_code(client):
    renamed = default_blueprint()
    # Rename the structured `exercises` section — forbidden (invariant #4).
    for s in renamed["sections"]:
        if s["kind"] == "exercises":
            s["key"] = "drills"
    r = client.put("/blueprint/default", json={"blueprint": renamed})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "structured_section_renamed"
    # A rejected edit does not create a row — still the code default.
    assert client.get("/blueprint/default").json()["is_override"] is False


def test_put_a_bad_weight_is_a_422_with_its_code(client):
    bad = default_blueprint()
    bad["sections"][0]["weight"] = 9.0
    r = client.put("/blueprint/default", json={"blueprint": bad})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "bad_weight"


def test_delete_resets_to_the_code_default(client):
    custom = default_blueprint()
    custom["sections"][0]["weight"] = 0.13
    client.put("/blueprint/default", json={"blueprint": custom})
    assert client.get("/blueprint/default").json()["is_override"] is True

    d = client.delete("/blueprint/default")
    assert d.status_code == 200
    assert d.json()["is_override"] is False
    assert d.json()["blueprint"] == default_blueprint()

    back = client.get("/blueprint/default")
    assert back.json()["is_override"] is False
    assert back.json()["blueprint"] == default_blueprint()


def test_delete_when_nothing_saved_is_idempotent(client):
    # Resetting an already-default settings is a 200, not a 404 — the wish is granted.
    d = client.delete("/blueprint/default")
    assert d.status_code == 200
    assert d.json()["is_override"] is False
    assert d.json()["blueprint"] == default_blueprint()
