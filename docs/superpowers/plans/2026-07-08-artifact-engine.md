# Artifact Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** The "generative-spec → deterministic-render" engine: the LLM emits a validated **spec** (chord fingering / tone recipe / signal chain / tab), and a deterministic **client renderer** draws a pixel-perfect artifact — chord diagrams, tab/staff (with playback), tone-recipe cards, signal-chain & amp-dial visuals — persisted and attachable to lessons.

**Architecture:** An `Artifact` model (`kind`, `spec` JSON, `title`, links) + per-kind **Pydantic spec validators** server-side; **client-side React/SVG renderers** (svguitar for chords, AlphaTab for tab/staff, custom SVG for signal-chain/amp-dials/tone-recipe). The LLM produces specs via `guided_json` (schema per kind). An Artifacts API + a gallery/preview in the cockpit; artifacts attach to curriculum `Block` segments.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic; React/Next.js; `svguitar` (chord diagrams), `@coderline/alphatab` (tab/staff + playback), custom SVG components; the `QwenVLLM.guided_json` seam.

## Global Constraints (verbatim)

- **The LLM only ever emits structured specs, never draws.** Every artifact = `{kind, spec}`; the spec is **validated server-side** (Pydantic per kind; reject/repair invalid) before persist; rendering is deterministic and client-side. (Same pattern as the curriculum guided-JSON; never trust freeform.)
- **Spec kinds (this plan):** `chord_diagram` (frets/fingers/barres), `scale_diagram` (fretboard positions), `tab` (alphaTex string), `signal_chain` (ordered nodes), `amp_settings` (named dials 0–10), `tone_recipe` (guitar/amp/drive/chain/hands/listen), `gear_card`. (Beginner-track chords + tab, tone-track recipe/chain/amp.)
- **Consume, don't re-implement:** `get_provider().guided_json(messages, schema)`; `app.brain.retrieve.search` (ground tone recipes/gear in the Brain); `app.models.block.Block` (attach via `block_id`); `app.db`. Tests isolate to `guitar_test` (conftest). Host env: `DATABASE_URL=…localhost:5434… LLM_BASE_URL=… EMBED_BASE_URL=…`.
- **Bilingual** labels (GR/EN) on card artifacts. **Web dev/test** on port 3100 (`fuser -k 3100/tcp` if stale).
- **Self-contained rendering:** renderers must work offline in the standalone Next build (bundle svguitar/alphatab; no external CDN — matches the app's CSP/offline posture).

## File Structure
```
apps/api/app/
  models/artifact.py            # Artifact(kind, spec JSON, title, tags, source, block_id?)
  artifacts/{__init__,specs,generate}.py  # Pydantic spec models per kind; LLM spec generation
  routers/artifacts.py          # CRUD + generate
  schemas/artifacts.py
apps/web/src/
  components/artifacts/
    chord-diagram.tsx           # svguitar wrapper
    tab-view.tsx                # alphatab wrapper (+ play button)
    signal-chain.tsx amp-dials.tsx tone-recipe-card.tsx scale-diagram.tsx gear-card.tsx
    artifact.tsx                # switch on kind -> renderer
  app/[locale]/(cockpit)/artifacts/page.tsx   # gallery + create/preview
  lib/api.ts (extend)
```

---

## Task 1: Artifact schema + per-kind spec validators
**Files:** `app/models/artifact.py`; `app/artifacts/{__init__,specs}.py`; migration; tests.
**Produces:** `Artifact(id, kind, spec JSON, title, tags[], source str("ai"|"uploaded"), block_id FK nullable, timestamps)`; a `SPECS: dict[str, type[BaseModel]]` mapping each kind → a Pydantic model, and `validate_spec(kind, spec) -> dict` (raises 422-able error on invalid). Spec models:
- `ChordDiagramSpec{name:str, frets:list[int](len 6, -1=mute), fingers:list[int](len 6, 0-4), barres?:list[{fret,fromString,toString}], baseFret:int=1}`
- `ScaleDiagramSpec{name, root:str, positions:list[{string:int(1-6), fret:int, degree?:str}]}`
- `TabSpec{alphaTex:str, title?:str}`
- `SignalChainSpec{nodes:list[{label:str, type?:str}]}` (ordered guitar→…→amp)
- `AmpSettingsSpec{amp?:str, dials:list[{label:str, value:float(0-10)}]}`
- `ToneRecipeSpec{artist?:str, song?:str, guitar:str, amp:str, drive?:str, chain:str, hands?:str, listen?:list[str]}`
- `GearCardSpec{name:str, kind:str, specs:list[{k:str,v:str}]}`
- [ ] TDD: `validate_spec("chord_diagram", {...valid...})` returns normalized dict; invalid (wrong fret length, value>10 on a dial) raises. Migration up/down. RED→GREEN. Commit.

## Task 2: Chord + scale + tone-recipe + signal-chain + amp-dial renderers (the visible wins)
**Files:** `apps/web/src/components/artifacts/*` for those kinds + `artifact.tsx` switch; a preview harness.
**Produces:** React components that take a validated spec and render deterministically: `ChordDiagram` (via `svguitar` into an SVG), `ScaleDiagram` (custom fretboard SVG), `ToneRecipeCard` (styled card — the on-brand money artifact), `SignalChain` (boxes + arrows SVG), `AmpDials` (knob SVGs with the 0–10 values). Clean, theme-aware, print-friendly.
- [ ] `npm i svguitar`. Build each component; a Playwright test (port 3100) mounts a demo `/en/artifacts?demo=1` (or a Storybook-less test page) rendering one of each from a fixed spec and asserts the SVG/card is present with expected text (e.g. the chord name, the recipe's amp text, the chain node labels). RED→GREEN. Commit.

## Task 3: Tab/staff via AlphaTab (notation + playback)
**Files:** `apps/web/src/components/artifacts/tab-view.tsx`; wire into `artifact.tsx`.
**Produces:** a `TabView` that renders a `TabSpec.alphaTex` via `@coderline/alphatab` into notation + tab, with a **Play** button (AlphaTab's synth). Bundle assets for offline/standalone (copy alphaTab's font/soundfont into `public/` or configure the bundler). 
- [ ] `npm i @coderline/alphatab`. Render a small alphaTex sample (e.g. an E-minor pentatonic lick); Playwright asserts the alphaTab container renders (canvas/svg present) and a Play control exists. (Audio itself not asserted.) RED→GREEN. Commit. *(If offline asset bundling proves heavy, ship notation-only first and note playback as a follow-up.)*

## Task 4: LLM artifact generation + Artifacts API
**Files:** `app/artifacts/generate.py`; `app/routers/artifacts.py`; `app/schemas/artifacts.py`; wire `main.py`.
**Produces:**
- `generate_artifact(db, *, kind, prompt, block_id=None, ground=False) -> Artifact`: builds a `guided_json` schema from `SPECS[kind]` (Pydantic `.model_json_schema()`), a tool-first system prompt ("emit ONLY the spec for a {kind}"), optional Brain grounding (`search`) for tone/gear kinds, → validated spec → persisted `Artifact`. E.g. `kind="chord_diagram", prompt="G major open chord"` → a correct G spec; `kind="tone_recipe", prompt="Stevie Ray Vaughan Texas Flood", ground=True` → a grounded recipe.
- Routes: `POST /artifacts` (kind, spec) create-from-spec; `POST /artifacts/generate` (kind, prompt, block_id?, ground?) → generate+persist; `GET /artifacts` (filter by block_id?/kind?); `GET /artifacts/{id}`; `DELETE /artifacts/{id}`.
- [ ] TDD: create-from-spec round-trips + validates; `POST /artifacts/generate {kind:"chord_diagram", prompt:"E minor open chord"}` (integration, live LLM) returns a spec whose frets look like Em (e.g. low-E open, A/D fretted at 2) — assert structural validity + name; a `tone_recipe` generate (grounded) returns the required fields. RED→GREEN. Commit.

## Task 5: Artifacts in the cockpit + attach-to-lesson + e2e
**Files:** `app/[locale]/(cockpit)/artifacts/page.tsx`; extend `lib/api.ts`, `block-card.tsx` (show/attach artifacts on a segment); message keys.
**Produces:** an **Artifacts** gallery page (create via a kind picker + prompt → `generate` with a loading state → live render; list existing) and, on the curriculum board, a segment card can **generate/attach** an artifact (e.g. a chord diagram for a chord lesson) rendered inline. Add "Artifacts" to the nav.
- [ ] Playwright (mocked API, 3100): the Artifacts page renders a generated chord diagram + a tone-recipe card from mocked specs; a board segment shows an attached artifact. Assert render + payloads. RED→GREEN.
- [ ] **Real e2e (report):** via the running stack, `POST /artifacts/generate` a G-major chord diagram and an SRV tone recipe; open `/en/artifacts` in a browser and confirm they render; screenshot. Commit + update `docs/superpowers/plans/README.md`.

## Self-Review
Coverage: spec→validate→render pattern ✓ · chord/scale/tab/signal-chain/amp/tone-recipe/gear kinds ✓ · client renderers (svguitar/alphatab/SVG) ✓ · LLM guided-JSON spec generation + Brain grounding ✓ · Artifacts API + gallery + attach-to-segment ✓ · real e2e ✓. Deferred (named): audio-playback polish if heavy (T3); uploaded-image artifacts → Cockpit/Notes plan; MusicXML/music21 server validation → later (Pydantic covers PoC). Types: `SPECS[kind]`(Pydantic)→`validate_spec`→`Artifact.spec`→`Artifact` React switch→per-kind renderer; `generate_artifact` uses `SPECS[kind].model_json_schema()` with `guided_json`.
