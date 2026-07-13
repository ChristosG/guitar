"""Per-kind Pydantic v2 spec models for `Artifact.spec` (Plan 4 Task 1).

Plan 4's global constraint: "The LLM only ever emits structured specs, never
draws." Every artifact is `{kind, spec}`; these models are the server-side
validation gate between an untrusted spec payload (LLM-generated via
`guided_json`, Task 4, or hand-authored/uploaded) and a persisted
`Artifact.spec` — rendering from a *validated* spec is deterministic and
lives client-side (Task 2+), never re-validated/re-interpreted there.

`SPECS` maps each `Artifact.kind` string to its model; `validate_spec` is the
single entry point callers (the Task 4 artifacts router, generation code)
should use:
  - unknown `kind` -> plain `ValueError`
  - structurally invalid `spec` -> `pydantic.ValidationError` propagates
    uncaught (it is itself a `ValueError` subclass)
Both are left for callers to map to HTTP 422; that mapping is not this
module's concern.
"""
import re
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

# A real alphaTex note is `fret.string[.duration]` (e.g. `3.6.4` = fret 3,
# string 6, quarter note) — confirmed against the vendored AlphaTab build
# (apps/web/public/alphatab) by rendering a worked example live in a
# browser (see this task's report). At minimum every genuine alphaTex
# string contains one `<digits>.<digits>` note token; a plain-English label
# like "G Major Scale Tab" (Plan 11's live-acceptance bug — the model filled
# `alphaTex` with a description instead of notation) contains no digits at
# all and never matches. This is deliberately a low bar (real generations
# will have many such tokens) rather than a full alphaTex grammar check —
# just enough to catch "not notation at all" before it reaches AlphaTab and
# throws "No alphaTex data found" client-side.
_ALPHATEX_NOTE_RE = re.compile(r"\d+\.\d+")


class BarreSpec(BaseModel):
    fret: int
    fromString: int
    toString: int


class ChordDiagramSpec(BaseModel):
    name: str
    # One entry per string, low-E -> high-E; -1 = muted string (no upper
    # bound — a fretted note above the diagrammed range is still a valid
    # fret number, just off-diagram).
    frets: list[Annotated[int, Field(ge=-1)]] = Field(min_length=6, max_length=6)
    # One entry per string; 0 = open/no fretting finger, 1-4 = fret-hand finger.
    fingers: list[Annotated[int, Field(ge=0, le=4)]] = Field(min_length=6, max_length=6)
    barres: list[BarreSpec] = []
    # >=1: a real fretboard position (1 = nut/open position). 0 or negative
    # is schema-shaped but not renderable — found via Task 5's real
    # end-to-end LLM generation, which produced `baseFret: 0`; the client
    # svguitar renderer throws ("Position cannot be less than 1") for
    # anything under 1. Constraining it here means a future out-of-range
    # generation fails `validate_spec` and gets `generate_artifact`'s
    # existing one-shot repair retry instead of silently persisting an
    # artifact the client can't render.
    baseFret: int = Field(default=1, ge=1)


class PositionSpec(BaseModel):
    string: int = Field(ge=1, le=6)
    fret: int = Field(ge=0)
    degree: str | None = None


class ScaleDiagramSpec(BaseModel):
    name: str
    root: str
    positions: list[PositionSpec]


class TabSpec(BaseModel):
    alphaTex: str
    title: str | None = None

    @field_validator("alphaTex")
    @classmethod
    def _alphatex_looks_like_notation(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("alphaTex must be non-empty")
        # Guards against a schema-valid but musically-empty spec: a plain
        # English sentence passes "non-empty string" fine but throws "No
        # alphaTex data found" in AlphaTab client-side (see module-level
        # `_ALPHATEX_NOTE_RE` docstring for how this rule was derived).
        if not _ALPHATEX_NOTE_RE.search(v):
            raise ValueError(
                "alphaTex must be real alphaTex notation (fret.string.duration "
                "note tokens, e.g. '3.6.4 5.6.4 | 2.5.4 3.5.4'), not a "
                "plain-English description"
            )
        return v


class NodeSpec(BaseModel):
    label: str
    type: str | None = None


class SignalChainSpec(BaseModel):
    nodes: list[NodeSpec] = Field(min_length=1)  # ordered guitar -> ... -> amp


class DialSpec(BaseModel):
    label: str
    value: float = Field(ge=0.0, le=10.0)


class AmpSettingsSpec(BaseModel):
    amp: str | None = None
    dials: list[DialSpec]


class ToneRecipeSpec(BaseModel):
    artist: str | None = None
    song: str | None = None
    guitar: str
    amp: str
    drive: str | None = None
    chain: str
    hands: str | None = None
    listen: list[str] = []


class KVSpec(BaseModel):
    k: str
    v: str


class GearCardSpec(BaseModel):
    name: str
    kind: str
    specs: list[KVSpec]


SPECS: dict[str, type[BaseModel]] = {
    "chord_diagram": ChordDiagramSpec,
    "scale_diagram": ScaleDiagramSpec,
    "tab": TabSpec,
    "signal_chain": SignalChainSpec,
    "amp_settings": AmpSettingsSpec,
    "tone_recipe": ToneRecipeSpec,
    "gear_card": GearCardSpec,
}


def validate_spec(kind: str, spec: dict) -> dict:
    """Validate + normalize a raw spec dict against its kind's model.

    Returns the normalized dict (`model_dump()` — defaults filled in, e.g. a
    `chord_diagram` missing `baseFret` comes back with `baseFret: 1`) on
    success. Raises plain `ValueError` for an unrecognized `kind`; lets
    `pydantic.ValidationError` propagate uncaught for a structurally invalid
    `spec` (wrong length, out-of-range value, missing required field, ...).
    """
    if kind not in SPECS:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    return SPECS[kind].model_validate(spec).model_dump()
