"""The resolution rule — `override if he saved one, else the code default` — and
the only module that reads or writes `prompt_override`.

WHY THIS IS NOT IN `registry.py`, WHICH IS WHERE THE BRIEF PUT IT. The registry
imports twelve live modules (`agent/loop`, `curriculum/corpus`, `students/context`,
…) so it can POINT AT their prompts rather than copy them. That makes it a VIEWER:
it depends on the live path, and the live path must not depend on it. The moment
`students/context.py` imports the registry to resolve its own slice, the arrow
points both ways — an import cycle today, and, worse, a viewer that has become
load-bearing for the prompts it claims only to watch. Delete the Settings page and
lessons stop generating.

So the rule lives HERE, in a leaf that imports nothing but its own table:

    students/context.py ─┐
                         ├─> prompts/overrides.py ──> models/prompt.py
    prompts/registry.py ─┘        (this module)
    routers/prompts.py ──┘

The live builder and the viewer read the same function, so they cannot disagree
about what the model is being sent — which is the entire point of the exercise —
and neither one imports the other.

`resolve(db, slice_id, default)` TAKES THE DEFAULT AS AN ARGUMENT rather than
looking it up. That is not indirection, it is the layering rule made explicit at
every call site: the caller owns its default (it is a constant three lines up in
the same file), and this module owns only the question "has he changed it?". A
version that fetched the default from the registry would put the registry back in
the live path through the back door.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.models.prompt import PromptOverride, PromptOverrideHistory

log = logging.getLogger(__name__)


class SliceInvalid(Exception):
    """A rejected edit, carrying the machine-readable `code` the web owns a Greek
    sentence for. Never a message at the tutor: `routers/prompts.py` turns this
    into `{"code": ..., ...}` and `settings/page.tsx`'s `t.has(...)` guard turns
    THAT into one sentence in his language. He never reads a word we did not
    write for him.
    """

    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code = code
        self.detail = {"code": code, **detail}


def stored(db, slice_id: str) -> str | None:
    """His override for `slice_id`, or None if he has never touched it.

    `db=None` means "no database available — code defaults only", which is the
    honest answer for `registry.render()` called outside a request (P1's own
    tests do exactly that). Not an error: a preview with no session is a preview
    of the code, and the code is what an un-edited install sends.
    """
    if db is None:
        return None
    row = db.get(PromptOverride, slice_id)
    return row.text if row is not None else None


def resolve(db, slice_id: str, default: str) -> str:
    """THE RULE, and the only implementation of it. Override on top; code
    underneath; never the reverse.

    Returns `default` — the identical object, not a copy — when nothing is
    stored, so `resolve(None, sid, X) is X`. That matters for the reset target:
    `Slice.default` is the live constant (P1 `is`-tests it), and a resolution
    that rebuilt the string would quietly break that chain.
    """
    return stored(db, slice_id) or default


def overridden_ids(db) -> set[str]:
    """Every slice he has changed, in one query.

    Exists so `GET /prompts` can flag 31 prompts without 31 round trips — the
    list route's `has_override` is the only reason to ask, and asking per prompt
    would be a query per card on a page that draws every card.
    """
    if db is None:
        return set()
    return set(db.scalars(select(PromptOverride.slice_id)).all())


def validate(text: str, *, default: str, max_chars: int) -> str:
    """The edit he is about to save, or `SliceInvalid` with a code.

    NO `kind` PARAMETER, though the spec's rule 3 is worded "is empty for a
    `replace` slice". It took one on the first pass and never read it, which is
    the tell: the answer for an `append` slice is the same answer. Empty append
    text is *identical* to having no override, so it would store a row that does
    nothing and report success — the same lie, reached by a different route, with
    the same fix (Reset). A parameter that cannot change an outcome is a branch
    the reader has to check for and a test that can never be written.

    1. EMPTY -> `empty`. For a `replace` slice an empty string would delete a
       sentence from the prompt while leaving a row claiming it is set. Reset is
       the explicit act for that, and it is one button away.

    2. TOO LONG -> `too_long`. Not a tidiness rule: this text ships on every call
       the prompt makes, and for a prefix slice it re-mints the cache. A pasted
       chapter here is a recurring invoice, and wasted spend is the top severity
       class in this app.

    3. THE PLACEHOLDER RULES, which only bite a slice whose default has one.
       Today none does — that is precisely what makes `student.pitch` safe to
       hand to a textarea — so the whole block short-circuits and his text is
       taken as the literal characters it is. It exists because the `Slice` type
       permits a placeholder-bearing default, and the day one is earned this is
       already here rather than being remembered.

       The required set is DERIVED from the default rather than configured per
       slice: the default IS the string the builder formats, so every `{name}` in
       it is a key the caller supplies. Deriving means a future slice cannot be
       registered with the wrong list — there is no list to get wrong.

       - `missing_placeholder` — he dropped one the default had. To be precise
         about the mechanism, because the brief's rationale is a little off:
         under `str.format` a DROPPED key does not raise (unused kwargs are
         fine), so the harm is not a crash but something quieter and arguably
         worse — an instruction silently emptied of the thing it was instructing
         about, e.g. a length rule that no longer names a length. Under
         `%`-formatting it does raise. Rejected either way.
       - `unknown_placeholder` — he ADDED one the builder does not supply. THIS
         is the `KeyError` at call time: a 500 where a lesson should be, possibly
         hours into an unattended twenty-lesson run.
       - `malformed_braces` — his text does not parse as a format string at all
         (a stray `{`). `.format` would raise `ValueError` at call time. Checked
         only when the text is actually going to be formatted, because for a slice
         that is appended raw — every slice today — a lone brace is just a
         character he typed, and refusing it would be the app inventing a rule.

    Returns the text with surrounding whitespace stripped. Stripping is not
    cosmetic here: `build_student_brief` joins its lines with "\\n", so a trailing
    newline he cannot see would put a blank line in the prompt.
    """
    text = text.strip()
    if not text:
        raise SliceInvalid("empty")
    if len(text) > max_chars:
        raise SliceInvalid("too_long", length=len(text), max_chars=max_chars)

    required = _placeholders(default)
    if not required:
        return text

    try:
        present = _placeholders(text)
    except ValueError as e:
        raise SliceInvalid("malformed_braces") from e

    missing = sorted(required - present)
    if missing:
        raise SliceInvalid("missing_placeholder", missing=missing)
    unknown = sorted(present - required)
    if unknown:
        raise SliceInvalid("unknown_placeholder", unknown=unknown)
    return text


def save(db, slice_id: str, text: str) -> PromptOverride:
    """Write the override, snapshotting whatever it replaced.

    The snapshot is taken BEFORE the write and only when there is something to
    snapshot: the first save replaces the code default, which is in git and is
    what Reset restores, so recording it would be filing a copy of something
    permanent. Every save after that replaces text that exists nowhere else.

    Assumes `validate` has already run — this is the storage half, and a caller
    that skips validation is a bug in the caller, not a second place to enforce
    the rules.
    """
    row = db.get(PromptOverride, slice_id)
    if row is None:
        row = PromptOverride(slice_id=slice_id, text=text)
        db.add(row)
    else:
        _snapshot(db, slice_id, row.text)
        row.text = text
    db.commit()
    log.info("prompt override saved: %s (%d chars)", slice_id, len(text))
    return row


def reset(db, slice_id: str) -> bool:
    """Drop the override; return whether there was one. Idempotent.

    THIS SNAPSHOTS, and it is the most important snapshot in the module. A PUT's
    previous text is kept by `save`; the code default is kept by git. Reset is the
    only operation that destroys text with no copy anywhere — so without this,
    the one irreversible button on the page would be the one labelled
    "Επαναφορά", pressed by a tutor who is a total beginner with computers and
    reasonably expects an undo.

    Returns False rather than raising when nothing is stored: the slice is
    already at its default, which is what he asked for. Reporting that as an
    error would put a failure in front of a man whose wish was granted.
    """
    row = db.get(PromptOverride, slice_id)
    if row is None:
        return False
    _snapshot(db, slice_id, row.text)
    db.delete(row)
    db.commit()
    log.info("prompt override reset to the code default: %s", slice_id)
    return True


def history(db, slice_id: str) -> list[PromptOverrideHistory]:
    """Every text this slice used to have, NEWEST FIRST.

    Newest first because the question this list answers is "give me back what I
    just lost", and the answer is almost always the top row. Oldest-first would
    put the most likely answer at the bottom of a list that grows forever.
    """
    return list(
        db.scalars(
            select(PromptOverrideHistory)
            .where(PromptOverrideHistory.slice_id == slice_id)
            .order_by(PromptOverrideHistory.replaced_at.desc())
        ).all()
    )


def _snapshot(db, slice_id: str, text: str) -> None:
    db.add(PromptOverrideHistory(slice_id=slice_id, text=text))


def _placeholders(text: str) -> set[str]:
    """The `{name}` keys in `text`, as `str.format` would read them.

    `string.Formatter` rather than a regex, so this agrees with the thing that
    actually raises the `KeyError` — `{a[0]}`, `{a.b}` and `{x:,}` all resolve to
    the key `format` would look up, `{{escaped}}` correctly yields nothing, and
    positional `{}` yields nothing to require. A regex would have to re-implement
    that and would be wrong in exactly the cases nobody tests.

    RAISES `ValueError` on an unparseable string. For the default that is correct
    and loud: it is our own constant, live code already formats it, and claiming
    it has no placeholders would silently disable the rules below. For the
    tutor's text `validate` catches it — see there.
    """
    import string

    return {
        name.split("[")[0].split(".")[0]
        for _, name, _, _ in string.Formatter().parse(text)
        if name
    }
