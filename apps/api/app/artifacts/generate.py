"""LLM artifact-spec generation: a `kind` + free-text `prompt` (optionally
Brain-grounded) -> guided-JSON spec -> validated -> persisted `Artifact`.

Mirrors `app.curriculum.generate`'s shape (a pure messages builder + the live
call, well-commented) but the guided_json schema is derived straight from the
kind's own Pydantic model (`SPECS[kind].model_json_schema()`) rather than
hand-written per kind, since Task 1 already built one spec model per kind —
hand-writing a second, parallel schema here would just duplicate
`app.artifacts.specs`. The generated output is a flat spec (one object),
not a nested tree, so there is no persist-a-tree step to mirror; the
"persist" side of this module is just constructing and committing one
`Artifact` row.
"""
import logging

from pydantic import ValidationError

from app.artifacts.specs import SPECS, validate_spec
from app.brain.retrieve import search
from app.llm.factory import get_provider
from app.models.artifact import Artifact

log = logging.getLogger(__name__)

_TITLE_MAX_LEN = 300  # Artifact.title column cap (String(300))


def _build_messages(
    *, kind: str, prompt: str, hits: list, repair_error: str | None = None,
) -> list[dict]:
    """Pure function: the {system,user} messages for one guided_json call —
    kept separate from `generate_artifact` (which also calls the live model)
    so the prompt shape is unit-testable without a model or a DB, mirroring
    `app.curriculum.generate._build_messages`.

    Tool-first/imperative and short, same rationale as curriculum's own
    `_build_messages` docstring: guided decoding already constrains output
    *shape* regardless of prompt wording, but a rambling prompt still
    degrades *content* quality inside that shape. `hits`, when non-empty,
    are folded in as numbered CONTEXT the model should ground its answer in
    (mirrors `retrieve.build_grounded_messages`'s numbered-context shape);
    empty when `ground=False` (or the caller just didn't pass any), in which
    case the user message is just the bare prompt. `repair_error`, when
    given (the one-retry repair path — see `generate_artifact`), appends the
    previous attempt's Pydantic validation error so the retry can actually
    fix the specific problem instead of blindly repeating it.
    """
    system = (
        f"You generate ONLY the JSON spec for a {kind}, matching the given "
        "schema — no prose, no markdown, no commentary outside the JSON object."
    )
    if hits:
        context = "\n\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
        user = f"{prompt}\n\nCONTEXT:\n{context}"
    else:
        user = prompt
    if repair_error:
        user += (
            f"\n\nYour previous attempt was INVALID: {repair_error}\n"
            "Return a corrected spec that fixes this and still matches the schema."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def derive_title(kind: str, spec: dict, prompt: str | None) -> str:
    """A sensible display `title` for a persisted Artifact.

    Public (not `_`-prefixed): shared by `generate_artifact` below AND
    `routers.artifacts.create_artifact`'s title-fallback for a `POST
    /artifacts` create-from-spec call that omits `title` — genuinely shared
    logic (not a private implementation detail of this module), so it's
    imported rather than re-derived a second time in the router (unlike
    e.g. `routers/curriculum.py`'s deliberate small duplication of segment.py's
    *module-private* `_get_or_create_delivery_root` — this is the opposite
    call: non-trivial, multi-branch logic that would drift if duplicated).

    `Artifact.title` is a NOT NULL column with no default, but not every
    kind carries an obvious name-like field (`signal_chain` has none at all;
    `amp_settings.amp` and both `tone_recipe.artist`/`song` are optional and
    may be absent) — so this tries, in order: a `name` or `title` field on
    the spec itself (covers chord/scale/gear_card's `name`, tab's `title`),
    then `tone_recipe`'s artist/song combo, then falls back to the request
    `prompt` itself (truncated to the column's cap), and only as an absolute
    last resort a generic label — reachable only for a spec-less kind given
    neither a title nor a prompt (a `POST /artifacts` call with no `title`
    for a name-less kind like `signal_chain`).
    """
    for field in ("name", "title"):
        value = spec.get(field)
        if value:
            return str(value)
    if kind == "tone_recipe":
        label = " — ".join(v for v in (spec.get("artist"), spec.get("song")) if v)
        if label:
            return label
    if prompt and prompt.strip():
        return prompt.strip()[:_TITLE_MAX_LEN]
    return f"{kind} artifact"


def generate_artifact(
    db, *, kind: str, prompt: str, block_id=None, ground: bool = False,
) -> Artifact:
    """Guided-JSON generate a `kind` spec from `prompt`, validate it (one
    repair retry on a structural/semantic failure), persist as an
    `Artifact` (source="ai"). Returns the persisted `Artifact`.

    `db` is a caller-owned SQLAlchemy Session (mirrors `generate_
    curriculum`): not closed here, but committed here — the persisted
    Artifact is this function's entire observable output. `block_id` is
    passed straight through to `Artifact` uninspected — validating it
    refers to a real `Block` (404 if not) is the caller's (router's) job,
    same split as `routers.curriculum`'s `_get_block_or_404` running before
    it ever calls into `segment_block`/`generate_curriculum`.

    `ground`: a plain caller-controlled toggle, deliberately NOT restricted
    to a hardcoded kind allowlist even though tone/gear specs are the
    typical case where it helps (a chord diagram doesn't need Brain
    context) — the caller already knows which kind it's asking for and
    decides whether grounding makes sense; a second, hidden kind-gate here
    that silently no-ops an explicit `ground=True` for some kind would be a
    surprise, not a safety net. Keeps this function's behavior simple and
    predictable instead.

    THE CRITICAL DESIGN POINT: vLLM's guided decoding (`guided_json`)
    CONSTRAINS output *shape* to `SPECS[kind]`'s own JSON schema, but does
    not reliably enforce every *semantic* constraint inside that shape —
    e.g. a chord's exactly-6-length `frets`/`fingers` arrays are schema'd
    via JSON Schema `minItems`/`maxItems`, which Plan 3's curriculum work
    left unverified as an actually-enforced constraint (only integer
    `minimum` was live-verified there). So the raw dict is ALWAYS
    re-validated here via `validate_spec`, which is where a semantic
    violation actually surfaces (as `pydantic.ValidationError`, itself a
    `ValueError` — see `app.artifacts.specs`'s own docstring). On that
    failure, ONE repair retry re-prompts with the validation error text
    folded in (`_build_messages(..., repair_error=...)`) and validates
    again; a second failure propagates uncaught (the router maps it to a
    422 "could not generate a valid {kind} spec") rather than looping
    further — a bounded single retry, not an open-ended repair loop.
    """
    if kind not in SPECS:
        raise ValueError(f"unknown artifact kind: {kind!r}")

    # A single artifact spec needs far less grounding context than a whole
    # curriculum tree (`generate_curriculum` uses k=12 there); k=6 is plenty
    # for one tone recipe or gear card.
    hits = search(db, prompt, k=6) if ground else []

    schema = SPECS[kind].model_json_schema()
    provider = get_provider()

    messages = _build_messages(kind=kind, prompt=prompt, hits=hits)
    raw = provider.guided_json(messages, schema)
    try:
        spec = validate_spec(kind, raw)
    except ValidationError as e:
        log.warning(
            "artifact generation: kind=%r failed validation on the first "
            "attempt, retrying once with the error folded back in: %s", kind, e,
        )
        repair_messages = _build_messages(
            kind=kind, prompt=prompt, hits=hits, repair_error=str(e),
        )
        raw = provider.guided_json(repair_messages, schema)
        spec = validate_spec(kind, raw)  # a second failure propagates uncaught

    artifact = Artifact(
        kind=kind, spec=spec, title=derive_title(kind, spec, prompt), block_id=block_id,
    )
    db.add(artifact)
    db.commit()
    return artifact
