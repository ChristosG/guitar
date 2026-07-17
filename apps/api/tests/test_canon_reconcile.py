"""Pass 2 — reconcile concept names across books (Part B, Task C3).

THE ASYMMETRY THIS FILE EXISTS TO PIN:

C2 deliberately let every book name concepts in its OWN words, because a fixed
taxonomy is a FILTER and a filter's failure mode is dropping the unique take that
justified buying the tenth book. That decision only pays off if reconciliation can
MERGE SYNONYMS AND NOTHING ELSE. So:

  * a wrong merge silently fuses two teachings          -> the failure
  * a missed merge leaves the canon slightly redundant  -> a cost, not a failure

Every threshold, every tie-break and every veto below is aimed down that gradient.
`concept_alias` is the receipt: each alias keeps its `source_id`, so a bad merge
is reversible with a query rather than by re-spending ~$13 re-reading ten books.

AND: THIS PASS READS NAMES, NOT BOOKS. `rapidfuzz` blocks candidates before the
model sees anything; the model only adjudicates near-misses. A reconcile that
called the model with no candidates would be spending money to be told nothing.
"""
import pytest

import app.canon.reconcile as reconcile_mod
from app.canon.reconcile import reconcile
from app.models.canon import Concept, ConceptAlias, ConceptClaim
from app.models.knowledge import KnowledgeSource


class _FakeProvider:
    """The adjudicator, scripted. `groups` maps a frozenset of names it is asked
    about -> the partition it returns."""

    def __init__(self, decide=None):
        self.decide = decide or (lambda names: [[n] for n in names])
        self.calls = 0
        self.asked = []            # every cluster of names it was shown
        self.messages_seen = []    # the flat transcript of the last call

    def guided_json(self, messages, schema, **kw):
        self.calls += 1
        self.messages_seen = list(messages)
        prompt = "\n".join(m["content"] for m in messages)
        names = [line.split("- ", 1)[1].split("  [", 1)[0].strip()
                 for line in prompt.split("\n") if line.strip().startswith("- ")]
        self.asked.append(names)
        return {"groups": [{"names": g} for g in self.decide(names)]}

    def count_tokens(self, text):
        return len(text) // 3 + 1


def _use(monkeypatch, provider):
    monkeypatch.setattr(reconcile_mod, "get_provider", lambda: provider)
    return provider


def _merge_all(names):
    return [list(names)]


def _merge_none(names):
    return [[n] for n in names]


def _book(db, title):
    source = KnowledgeSource(type="pdf", title=title, status="ready")
    db.add(source)
    db.flush()
    return source


def _concept_from(db, source, name, *, key=None, pages=(1,), stance="a stance"):
    """One book naming one concept — exactly what `compile_book` writes."""
    from app.canon.compile import _concept_key

    key = key or _concept_key(name)
    concept = db.query(Concept).filter_by(key=key).one_or_none()
    if concept is None:
        concept = Concept(key=key, label_en=name)
        db.add(concept)
        db.flush()
    db.add(ConceptClaim(concept_id=concept.id, source_id=source.id,
                        text=f"{source.title} on {name}", pages=list(pages),
                        stance=stance, depth="primary", grounding="author"))
    db.add(ConceptAlias(concept_id=concept.id, source_id=source.id, alias=name))
    db.flush()
    return concept


# ---------------------------------------------------------------------------
# The brief's own three cases
# ---------------------------------------------------------------------------

def test_three_books_naming_one_thing_three_ways_collapse_to_one_concept(db, monkeypatch):
    """The plan's worked example. Three books, three names, one idea — and each
    alias KEEPS the book that said it, because "who called it this" is the whole
    question when someone later asks whether the merge was right."""
    _use(monkeypatch, _FakeProvider(_merge_all))
    s1, s2, s3 = (_book(db, f"Book {i}") for i in (1, 2, 3))
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "pickup adjustment")
    _concept_from(db, s3, "adjusting pickup height")
    db.commit()

    concepts = reconcile(db)

    assert len(concepts) == 1, [c.label_en for c in concepts]
    survivor = concepts[0]
    aliases = db.query(ConceptAlias).filter_by(concept_id=survivor.id).all()
    assert {a.alias for a in aliases} == {
        "pickup height", "pickup adjustment", "adjusting pickup height"}
    # Each alias still knows which book said it — the receipt that makes a bad
    # merge reversible without recompiling.
    assert {a.source_id for a in aliases} == {s1.id, s2.id, s3.id}
    # Every claim followed its concept. Nothing was dropped.
    assert db.query(ConceptClaim).filter_by(concept_id=survivor.id).count() == 3
    assert db.query(ConceptClaim).count() == 3
    assert db.query(Concept).count() == 1


def test_two_genuinely_different_concepts_do_not_merge(db, monkeypatch):
    """A wrong merge silently fuses two teachings, and no test downstream can see
    it — the canon just quietly says one thing where the books said two."""
    _use(monkeypatch, _FakeProvider(_merge_none))
    s1, s2 = _book(db, "Book 1"), _book(db, "Book 2")
    _concept_from(db, s1, "major scale")
    _concept_from(db, s2, "minor scale")
    db.commit()

    concepts = reconcile(db)

    assert len(concepts) == 2
    assert {c.label_en for c in concepts} == {"major scale", "minor scale"}
    for concept in concepts:
        assert db.query(ConceptClaim).filter_by(concept_id=concept.id).count() == 1


def test_reconcile_is_stable_same_inputs_same_canonical_keys(db, monkeypatch):
    """Run it twice, get the same canon. The keys are what C4 renders, what C7
    shows the tutor and what C8 searches; a key that moves on a re-run
    invalidates all three."""
    _use(monkeypatch, _FakeProvider(_merge_all))
    s1, s2, s3 = (_book(db, f"Book {i}") for i in (1, 2, 3))
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "pickup adjustment")
    _concept_from(db, s3, "adjusting pickup height")
    db.commit()

    first = [c.key for c in reconcile(db)]
    second = [c.key for c in reconcile(db)]

    assert first == second
    assert len(first) == 1
    # And the second run is a NO-OP, not a re-merge that happens to land the same
    # way: there is nothing left to adjudicate.
    assert db.query(ConceptAlias).count() == 3


def test_the_canonical_key_does_not_depend_on_the_order_books_were_compiled(db, monkeypatch):
    """"Same inputs, same canonical keys" is only true if `inputs` means the
    names — not the order rows happened to be inserted in. This is the test that
    a `first one wins` tie-break would fail."""
    keys = []
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        db.query(ConceptClaim).delete()
        db.query(ConceptAlias).delete()
        db.query(Concept).delete()
        db.query(KnowledgeSource).delete()
        db.commit()

        _use(monkeypatch, _FakeProvider(_merge_all))
        names = ["pickup height", "pickup adjustment", "adjusting pickup height"]
        books = [_book(db, f"Book {i}") for i in range(3)]
        for i in order:
            _concept_from(db, books[i], names[i])
        db.commit()
        keys.append([c.key for c in reconcile(db)])

    assert keys[0] == keys[1] == keys[2], keys


# ---------------------------------------------------------------------------
# Nothing may be discarded. Ever.
# ---------------------------------------------------------------------------

def test_a_merge_moves_every_claim_and_never_deletes_one(db, monkeypatch):
    """`concept_claim.concept_id` is ON DELETE CASCADE. So a merge that deletes
    the loser's row BEFORE reparenting its claims takes the claims with it —
    silently, and the only symptom is a canon that is quietly missing a book's
    entire take on a concept."""
    _use(monkeypatch, _FakeProvider(_merge_all))
    s1, s2 = _book(db, "Book 1"), _book(db, "Book 2")
    a = _concept_from(db, s1, "pickup height", pages=(47, 48))
    b = _concept_from(db, s2, "adjusting pickup height", pages=(112,))
    db.add(ConceptClaim(concept_id=b.id, source_id=s2.id, text="a second claim",
                        pages=[113], stance="another", depth="mention",
                        grounding="figure"))
    db.flush()
    db.commit()
    before = {(c.source_id, c.text, tuple(c.pages), c.grounding)
              for c in db.query(ConceptClaim).all()}
    assert len(before) == 3

    concepts = reconcile(db)

    assert len(concepts) == 1
    after = {(c.source_id, c.text, tuple(c.pages), c.grounding)
             for c in db.query(ConceptClaim).all()}
    assert after == before, "a merge lost a claim"
    assert all(c.concept_id == concepts[0].id for c in db.query(ConceptClaim).all())


def test_reconcile_has_no_path_that_deletes_a_concept_nobody_merged(db, monkeypatch):
    """The no-taxonomy decision is only sound if reconciliation cannot discard.
    A concept the model never even saw must come out the far side untouched."""
    _use(monkeypatch, _FakeProvider(_merge_none))
    s1 = _book(db, "Book 1")
    for name in ("string bending", "tube amp bias", "wah pedal placement"):
        _concept_from(db, s1, name)
    db.commit()

    concepts = reconcile(db)

    assert {c.label_en for c in concepts} == {
        "string bending", "tube amp bias", "wah pedal placement"}
    assert db.query(ConceptClaim).count() == 3


def test_a_model_that_invents_a_name_cannot_delete_or_rename_a_concept(db, monkeypatch):
    """The adjudicator's answer is VALIDATED, not trusted — the same discipline as
    C2's citations. A group naming something that is not in the cluster is a
    hallucination, and it must not be able to take a real concept with it."""
    _use(monkeypatch, _FakeProvider(
        lambda names: [["pickup height", "a concept nobody wrote down"]]))
    s1, s2 = _book(db, "Book 1"), _book(db, "Book 2")
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "adjusting pickup height")
    db.commit()

    concepts = reconcile(db)

    # The invented name is ignored; the real concepts both survive (the group
    # collapsed to one real member, which is not a merge).
    assert {c.label_en for c in concepts} == {"pickup height", "adjusting pickup height"}
    assert db.query(ConceptClaim).count() == 2


def test_one_book_that_named_two_concepts_separately_does_not_get_them_merged(db, monkeypatch):
    """C2 already told the compile "do not force two different ideas together
    because they sound similar" — at book granularity, that job is done. If ONE
    author drew a distinction between two names, C3 is not the pass that erases
    it, and the merge is refused however confident the model was.

    It is also what keeps `UNIQUE (concept_id, source_id)` on `concept_alias` an
    invariant by construction rather than by luck.
    """
    _use(monkeypatch, _FakeProvider(_merge_all))
    s1 = _book(db, "Book 1")
    _concept_from(db, s1, "hammer-ons")
    _concept_from(db, s1, "hammer-ons and pull-offs")
    db.commit()

    concepts = reconcile(db)

    assert len(concepts) == 2, (
        "one book's own distinction between two concepts was erased by reconcile"
    )
    assert db.query(ConceptAlias).count() == 2


def test_a_merge_admits_the_books_it_can_and_refuses_only_the_clash(db, monkeypatch):
    """The veto is per-book, not per-group: a three-way merge where one member
    clashes must still merge the two that do not. Refusing the whole group would
    make one book's over-split contagious."""
    _use(monkeypatch, _FakeProvider(_merge_all))
    s1, s2 = _book(db, "Book 1"), _book(db, "Book 2")
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "adjusting pickup height")
    _concept_from(db, s1, "pickup height adjustment")     # s1 again -> clashes
    db.commit()

    concepts = reconcile(db)

    assert len(concepts) == 2
    alias_counts = sorted(
        db.query(ConceptAlias).filter_by(concept_id=c.id).count() for c in concepts)
    assert alias_counts == [1, 2], (
        "the clash should have refused ONE book, not the whole group"
    )
    assert db.query(ConceptClaim).count() == 3      # nothing lost


# ---------------------------------------------------------------------------
# This pass reads names, not books — and it must not spend when it need not
# ---------------------------------------------------------------------------

def test_the_model_is_never_asked_about_names_that_are_nothing_alike(db, monkeypatch):
    """Blocking is the point: `rapidfuzz` is deterministic and free, and the
    model only sees what survives it."""
    provider = _use(monkeypatch, _FakeProvider(_merge_none))
    s1, s2 = _book(db, "Book 1"), _book(db, "Book 2")
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "adjusting pickup height")
    _concept_from(db, s1, "reverb tank impedance")
    _concept_from(db, s2, "the philosophy of daily practice")
    db.commit()

    reconcile(db)

    assert provider.calls == 1
    asked = set(provider.asked[0])
    assert asked == {"pickup height", "adjusting pickup height"}, asked


def test_a_canon_with_no_near_misses_costs_nothing(db, monkeypatch):
    """Wasted LLM spend is the top severity class. Nothing to adjudicate means
    nothing to pay for."""
    provider = _use(monkeypatch, _FakeProvider(_merge_none))
    s1 = _book(db, "Book 1")
    _concept_from(db, s1, "string bending")
    _concept_from(db, s2 := _book(db, "Book 2"), "reverb tank impedance")
    db.commit()

    concepts = reconcile(db)

    assert provider.calls == 0
    assert len(concepts) == 2


def test_an_empty_canon_calls_nothing_and_returns_nothing(db, monkeypatch):
    provider = _use(monkeypatch, _FakeProvider())
    assert reconcile(db) == []
    assert provider.calls == 0


def test_the_model_sees_which_book_used_each_name(db, monkeypatch):
    """"Who called it this" is the whole question when deciding whether two books
    meant the same thing — and it is the evidence that lets the model avoid
    proposing a merge that the same-book rule would then have to veto."""
    provider = _use(monkeypatch, _FakeProvider(_merge_none))
    s1 = _book(db, "Tone Manual (Hunter)")
    s2 = _book(db, "Guitar Tone (Gallagher)")
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "adjusting pickup height")
    db.commit()

    reconcile(db)

    prompt = "\n".join(m["content"] for m in provider.messages_seen)
    assert "Tone Manual (Hunter)" in prompt
    assert "Guitar Tone (Gallagher)" in prompt


def test_a_provider_failure_leaves_the_canon_exactly_as_it_was(db, monkeypatch):
    """Reconcile is a merge pass over USER DATA that cost ~$13 to produce. A
    half-applied merge is worse than no merge: it is a canon nobody can reason
    about, and re-running cannot fix it because the evidence moved."""
    class _Boom:
        calls = 0

        def guided_json(self, *a, **k):
            raise RuntimeError("the model fell over")

    _use(monkeypatch, _Boom())
    s1, s2 = _book(db, "Book 1"), _book(db, "Book 2")
    _concept_from(db, s1, "pickup height")
    _concept_from(db, s2, "adjusting pickup height")
    db.commit()

    with pytest.raises(RuntimeError):
        reconcile(db)

    db.rollback()
    assert db.query(Concept).count() == 2
    assert db.query(ConceptClaim).count() == 2
    assert db.query(ConceptAlias).count() == 2
