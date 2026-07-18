"""`/prompts` — every prompt this app sends to a model, readable; the two or
three sentences that carry no contract, editable.

READING IS THE FEATURE. EDITING IS THE FOOTNOTE. Chris asked for this on day one
and then narrowed it himself: *"we cant degrade their quality so the teacher
understands them better. He wont tweak them himself, but he just needs to watch
them. he might tweak only some text explaining stuff."* So four of these five
routes exist to SHOW, one exists to change, and the thing it can change is one
sentence about how to address a student.

WHAT THE ROUTES RETURN IS RENDERED, NOT STORED. "The full prompt" is not a string
anywhere in this codebase — only two constants are byte-stable, everything else is
assembled from f-strings at the moment of the call. So `GET /prompts/{id}` BUILDS
one, with the same builders the live path uses (`app/prompts/registry.py`), and
`db` goes all the way down so that a slice he has overridden renders as HIS
sentence. A preview showing the code default underneath a textarea showing his
edit is a page that contradicts itself, and he cannot tell which half is lying.

EVERY FAILURE IS A `code`, following `routers/settings.py`. The tutor is a total
beginner with computers (`settings/page.tsx:22-37`): he never sees JSON, a stack
trace, a status code, or an English string we forgot to translate. The seven codes
this router can emit — `unknown_prompt`, `unknown_slice`, `empty`, `too_long`,
`missing_placeholder`, `unknown_placeholder`, `malformed_braces` — each need one
Greek sentence in `apps/web/src/messages/el.json` under `prompts.errors.*`, which
`t.has(...)` looks up exactly as `settings/page.tsx:215` already does.

NO EXTRA AUTH. The whole cockpit sits behind one session gate
(`auth/middleware.py`, `AUTH_ENABLED=1`); a second, different rule on this router
would be a second thing to get wrong on a single-user app.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.i18n import SUPPORTED_LOCALES, locale_dep
from app.prompts import overrides, registry

router = APIRouter(prefix="/prompts", tags=["prompts"])


class MessageOut(BaseModel):
    """One message as the provider receives it. `cached` marks the prompt-cache
    breakpoint (`corpus.library_message`) — shown because the entire premise of
    `corpus.py` is that a cache mistake is invisible until the invoice arrives."""

    role: str
    content: str
    cached: bool


class SpanOut(BaseModel):
    """Where an interpolated variable landed, so the UI can draw it as a labelled
    chip instead of a hole. `value` is a representative sample of HIS data — except
    for a slice's span, which is the real resolved text."""

    name: str
    label_el: str
    value: str
    start: int
    end: int


class SliceOut(BaseModel):
    id: str
    prompt_id: str
    label_el: str
    kind: str
    default: str
    effective: str
    has_override: bool
    max_chars: int
    cache_cost_warning: bool


class PromptSummary(BaseModel):
    id: str
    flow: str
    kind: str
    title_el: str
    what_it_does_el: str
    when_it_runs_el: str
    provider: str | None
    cache_prefix: bool
    cache_cost_warning: bool
    has_override: bool
    # True when the model's LANGUAGE for this prompt is decided by the course (which
    # takes it from the student), not by the cockpit locale. The web shows one Greek
    # sentence for these and offers the toggle below — see `registry._SAMPLE_COURSE_LANGUAGE`
    # for the bug this exists to end.
    language_from_course: bool
    # C1: this prompt is one of the ten that actually shape a course/lesson.
    # `prompt-list.tsx` renders the subset as a synthetic "Curriculum" group at the
    # top of the page — a shortcut into the existing groups, not a new API surface.
    curriculum_group: bool


class PromptDetail(PromptSummary):
    # STILL RETURNED, deliberately, though the web no longer draws it. Chris: *"on each
    # prompt i also see where they are inside the code e.g. 'In the code:
    # app/curriculum/corpus.py:289', i dont think this should be seen by the tutor."*
    # He is right — a file path is the same category as a stack trace or a status code,
    # and `settings/page.tsx:22-37` says he sees none of those. But it is load-bearing
    # HERE: `test_source_refs_point_inside_the_real_definition` is what keeps it honest,
    # and a developer opening this route is exactly who it is for. Presentation change,
    # not an API one.
    source_ref: str
    # The language this preview was rendered at. Only meaningful when
    # `language_from_course` — it is the answer to "which language am I looking at?",
    # which the cockpit locale can no longer be trusted to answer for these prompts.
    course_language: str | None
    text: str
    messages: list[MessageOut]
    spans: list[SpanOut]
    slices: list[SliceOut]


class HistoryOut(BaseModel):
    id: uuid.UUID
    text: str
    replaced_at: datetime


class SliceIn(BaseModel):
    text: str


def _entry(prompt_id: str) -> registry.PromptEntry:
    try:
        return registry.REGISTRY[prompt_id]
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_prompt", "message": f"no prompt {prompt_id!r}"},
        ) from None


def _slice(slice_id: str) -> tuple[registry.PromptEntry, registry.Slice]:
    try:
        return registry.SLICES[slice_id]
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_slice", "message": f"no slice {slice_id!r}"},
        ) from None


def _slice_out(db, entry: registry.PromptEntry, sl: registry.Slice) -> SliceOut:
    stored = overrides.stored(db, sl.id)
    return SliceOut(
        id=sl.id,
        prompt_id=entry.id,
        label_el=sl.label_el,
        kind=sl.kind,
        default=sl.default,
        effective=stored or sl.default,
        has_override=stored is not None,
        max_chars=sl.max_chars,
        # THE OWNER'S flag, not the slice's — the slice has none. Conservative on
        # purpose: the spec puts future append slots in the volatile tail of
        # curriculum prompts, where an edit costs nothing, and this would warn
        # about them anyway. Over-warning annoys; under-warning bills. The fix, on
        # the day such a slice exists, is a per-slice flag — not today, for a field
        # that would be `False` on every row.
        cache_cost_warning=entry.cache_prefix,
    )


def _summary(entry: registry.PromptEntry, overridden: set[str]) -> dict:
    return {
        "id": entry.id,
        "flow": entry.flow,
        "kind": entry.kind,
        "title_el": entry.title_el,
        "what_it_does_el": entry.what_it_does_el,
        "when_it_runs_el": entry.when_it_runs_el,
        "provider": entry.provider,
        "cache_prefix": entry.cache_prefix,
        # Two names, one value today, two different questions. `cache_prefix` is
        # WHERE this prompt sits on the wire — a fact about the app. This is
        # whether the UI must warn before a change — a fact about editing. They
        # coincide because everything in the cached prefix is expensive to change
        # and nothing outside it is; collapsing them would make P3 re-derive a
        # rule the API already knows.
        "cache_cost_warning": entry.cache_prefix,
        "language_from_course": entry.language_from_course,
        "curriculum_group": entry.curriculum_group,
        "has_override": any(s.id in overridden for s in entry.slices),
    }


@router.get("", response_model=list[PromptSummary])
def list_prompts(db: Session = Depends(get_db)) -> list[PromptSummary]:
    """Every prompt, in REGISTRATION ORDER — chat first, because it is the thing
    he uses every day (`registry.by_flow`). Grouping is P3's to do; the order it
    groups is this one.

    No `text` here, deliberately: rendering all 31 means building all 31, and the
    largest is 14,000 characters of tool schemas. The list draws the cards; the
    detail route fills one in when he opens it.
    """
    overridden = overrides.overridden_ids(db)
    return [PromptSummary(**_summary(e, overridden)) for e in registry.REGISTRY.values()]


@router.get("/{prompt_id}", response_model=PromptDetail)
def get_prompt(
    prompt_id: str,
    course_language: str | None = None,
    db: Session = Depends(get_db),
    locale: str = Depends(locale_dep),
) -> PromptDetail:
    """One prompt, rendered as the model would receive it right now.

    `locale` comes from `X-App-Locale` like every other route (`i18n.locale_dep`)
    because `language_directive` is injected into 8 of these prompts and is the
    only locale-varying text in the app — a preview that ignored it would show him
    a prompt this app never sends.
    """
    entry = _entry(prompt_id)
    if course_language is not None and course_language not in SUPPORTED_LOCALES:
        # Not a 422: a language is a presentation preference, not user input to be
        # validated (`i18n.normalize_locale` NEVER raises, for the same reason). An
        # unknown value falls back to the sample course's own language.
        course_language = None
    rendered = entry.render(locale, db, course_language)
    overridden = overrides.overridden_ids(db)
    return PromptDetail(
        **_summary(entry, overridden),
        source_ref=entry.source_ref,
        course_language=(
            registry.course_language_of(course_language)
            if entry.language_from_course else None
        ),
        text=rendered.text,
        messages=[MessageOut(**vars(m)) for m in rendered.messages],
        spans=[SpanOut(**vars(s)) for s in rendered.spans],
        slices=[_slice_out(db, entry, s) for s in entry.slices],
    )


@router.put("/slices/{slice_id}", response_model=SliceOut)
def put_slice(
    slice_id: str, payload: SliceIn, db: Session = Depends(get_db)
) -> SliceOut:
    """Save his text for one slice, snapshotting whatever it replaced.

    Validation runs BEFORE the write and the write is all-or-nothing, so a
    rejected edit leaves the stored text exactly as it was — he does not lose a
    good paragraph by pasting a bad one over it and getting a red message.
    """
    entry, sl = _slice(slice_id)
    try:
        text = overrides.validate(
            payload.text, default=sl.default, max_chars=sl.max_chars
        )
    except overrides.SliceInvalid as e:
        raise HTTPException(status_code=422, detail=e.detail) from e

    overrides.save(db, slice_id, text)
    return _slice_out(db, entry, sl)


@router.delete("/slices/{slice_id}", response_model=SliceOut)
def delete_slice(slice_id: str, db: Session = Depends(get_db)) -> SliceOut:
    """Reset to the code default — the reset TARGET being in code is the whole
    reason defaults are not seeded into the table.

    Idempotent: resetting something never overridden is a 200, not a 404. It is
    already at its default, which is what he asked for; reporting that as an error
    would put a failure in front of a man whose wish was granted.
    """
    entry, sl = _slice(slice_id)
    overrides.reset(db, slice_id)
    return _slice_out(db, entry, sl)


@router.get("/slices/{slice_id}/history", response_model=list[HistoryOut])
def get_slice_history(slice_id: str, db: Session = Depends(get_db)) -> list[HistoryOut]:
    """Every text this slice used to have, newest first — including the one Reset
    destroyed, which is the only text in the system with no other copy.

    Empty for a slice he has never edited, and for the first edit of one: what the
    first save replaced was the code default, and that is in git.
    """
    _slice(slice_id)
    return [
        HistoryOut(id=h.id, text=h.text, replaced_at=h.replaced_at)
        for h in overrides.history(db, slice_id)
    ]
