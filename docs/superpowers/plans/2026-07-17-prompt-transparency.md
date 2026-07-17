# Prompt Transparency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Settings card where the tutor can READ every prompt the app sends to a model — verbatim, in Greek-annotated form — and edit the few slices that carry no contract.

**Architecture:** A registry (`app/prompts/registry.py`) that *points at* the existing prompt definitions rather than copying them, and can render each one with sample interpolations so what he reads is what the model gets. Overrides live in a new `prompt_override` table and layer over code-owned defaults. Read-only by default; two contract-free slices are editable.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + Postgres; Next.js client component + next-intl; pytest + Playwright.

**Spec:** `docs/superpowers/specs/2026-07-17-prompt-transparency-design.md` — read it. It carries the reasoning; this plan carries the steps.

## Global Constraints

- **Prompt quality is NOT negotiable.** Never simplify, translate, or restructure a prompt to make it readable. The text the model gets is the text he reads. We *annotate*, never rewrite. Chris: *"we cant degrade their quality so the teacher understands them better."*
- **Reading is the feature; editing is the footnote.** Chris: *"He wont tweak them himself, but he just needs to watch them. he might tweak only some text explaining stuff."*
- **The tutor is a total beginner with computers** (`settings/page.tsx:22-37`). He never sees JSON, a stack trace, a status code, or an untranslated English string. Every failure arrives as a machine-readable `code` the web turns into one Greek sentence.
- **Greek (`el`) is the default locale.** Every user-facing string ships in BOTH `apps/web/src/messages/el.json` and `en.json`. (Note the path — it is `src/messages/`, not `messages/`.)
- **Migrations are additive only.** `alembic upgrade head` runs in the api CMD at boot. Current head: `a1b2c3d4e5f6`.
- **Wasted or duplicated LLM spend is the top severity class.** `CURRICULUM_SYSTEM` sits INSIDE the cached prefix — editing it re-mints the cache once. That must be SHOWN, not hidden.
- **Tests:** `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q`. Baseline **1149 passed, 51 deselected**. Run it ONCE and let it finish (~80s) — concurrent runs contend on the shared `guitar_test` DB and look like a hang.
- **If you change `web`, rebuild and redeploy it** (`docker compose up -d --build web`). An e2e test caught a 19-hour-stale bundle; every existing spec tests `next dev`, not the running image. A source fix that is not deployed is not a fix.

---

## Why "locked" is not paternalism

`agent/prompts.py:67-87` is 1,301 characters and nearly every sentence is a guard that exists because
something broke once: *never invent a citation*, *never write tablature as text*, *never invent a
copyrighted riff*, *call tools silently*. A textarea over that is a brake line with scissors next to it.

But **locked must mean "an editor can't break it by accident", not "Chris can't change it."** It is his
app. Every locked region names its reason in the UI, in Greek, and every one is a normal code change away.
The worked example is already in the spec: he asked to remove the copyright rule; investigating found it
restricted none of his books and that his real complaint was an ordering bug elsewhere (`loop.py:601`
declining before `loop.py:608` searched). **A viewer that shows the prompt honestly is what made that
diagnosable.** That is the feature.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/api/app/prompts/__init__.py` | new. Package. |
| `apps/api/app/prompts/registry.py` | new. `PromptEntry`, `Slice`, `REGISTRY`, `render(id)`, `resolve(slice_id)`. Points at existing definitions; owns no prompt text. |
| `apps/api/alembic/versions/*_prompt_override.py` | new. `prompt_override` + `prompt_override_history`. Additive. |
| `apps/api/app/models/prompt.py` | new. The two models. |
| `apps/api/app/routers/prompts.py` | new. GET list / GET one / PUT slice / DELETE slice / GET history. |
| `apps/api/app/main.py` | modify. Register the router. |
| `apps/web/src/lib/api.ts` | modify. Client fns + types. |
| `apps/web/src/components/settings/prompt-list.tsx` | new. The card. |
| `apps/web/src/app/[locale]/(cockpit)/settings/page.tsx` | modify. Mount the card. |
| `apps/web/src/messages/{el,en}.json` | modify. Greek first. |

---

### Task P1: The registry

**Files:**
- Create: `apps/api/app/prompts/__init__.py`, `apps/api/app/prompts/registry.py`
- Test: `apps/api/tests/test_prompts_registry.py`

**Interfaces:**
- Consumes: the existing prompt definitions (`agent.prompts.SYSTEM_PROMPT`, `curriculum.corpus.CURRICULUM_SYSTEM`, `brain.ocr.OCR_PROMPT`, `i18n.language_directive`, `students.context.build_student_brief`, `agent.tools.TOOLS`, `brain.retrieve._TRANSLATE_SYSTEM`, …).
- Produces: `PromptEntry`, `Slice`, `REGISTRY: dict[str, PromptEntry]`, `render(prompt_id, locale) -> RenderedPrompt`. Tasks P2/P3 consume these.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_prompts_registry.py
"""The registry must POINT AT the prompts, never copy them.

A viewer that holds its own copy of a prompt is how it silently starts lying: it
shows the copy while the model gets the original, and nothing ever tells you.
That is worse than no viewer, because it is a viewer he will trust — the same
shape as the citation failure this codebase is architected against.
"""
import pytest

from app.agent.prompts import SYSTEM_PROMPT
from app.curriculum.corpus import CURRICULUM_SYSTEM
from app.prompts.registry import REGISTRY, render


def test_the_chat_system_prompt_is_the_SAME_OBJECT_not_a_copy():
    """`is`, not `==`. Equality would pass against a copy that has since drifted."""
    rendered = render("chat.system", locale="el")
    assert SYSTEM_PROMPT in rendered.text
    assert REGISTRY["chat.system"].source_of_truth() is SYSTEM_PROMPT


def test_the_curriculum_system_prompt_is_the_same_object():
    assert REGISTRY["curriculum.system"].source_of_truth() is CURRICULUM_SYSTEM


def test_every_entry_has_greek_annotation():
    """He reads Greek. An entry without it is an English string at the tutor,
    which the settings page's own contract forbids."""
    for pid, entry in REGISTRY.items():
        assert entry.title_el.strip(), f"{pid} has no Greek title"
        assert entry.what_it_does_el.strip(), f"{pid} has no Greek description"
        assert entry.when_it_runs_el.strip(), f"{pid} has no Greek trigger"


def test_render_marks_interpolated_variables_rather_than_hiding_them():
    """He must see WHERE the student brief goes, not a prompt with a hole in it."""
    rendered = render("lesson.draft", locale="el")
    assert rendered.spans, "no interpolation spans reported"
    assert any(s.name for s in rendered.spans)


def test_locale_changes_the_language_directive():
    """`i18n.language_directive` is the app's only locale-varying directive and it
    is injected into 8 prompts. If the rendered preview ignores locale, he is
    reading a prompt the model never gets."""
    el = render("chat.system", locale="el").text
    en = render("chat.system", locale="en").text
    assert el != en
    assert "Greek" in el or "Ελλην" in el


def test_registry_covers_every_provider_call_site():
    """THE test that keeps this honest over time.

    15 call sites reach an LLMProvider method outside app/llm/ (counted
    2026-07-17: artifacts/generate.py x2, lessons/draft.py, brain/ocr.py,
    curriculum/draft.py x3, brain/retrieve.py x2, curriculum/extend.py,
    agent/loop.py x2, curriculum/refine.py, curriculum/outline.py,
    routers/settings.py). When someone adds the 16th, this fails and the viewer
    does not silently go stale.
    """
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    call = re.compile(r"\.(chat|guided_json|chat_tools|chat_tools_stream|vision)\(")
    sites = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py") if "llm/" not in str(p.relative_to(root))
        for i, line in enumerate(p.read_text().splitlines(), 1) if call.search(line)
    ]
    covered = {s for e in REGISTRY.values() for s in e.call_sites}
    uncovered = [s for s in sites if s.split(":")[0] not in {c.split(":")[0] for c in covered}]
    assert not uncovered, f"provider call sites with no registry entry: {uncovered}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_prompts_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.prompts'`

- [ ] **Step 3: Implement the registry**

Design notes that are binding:
- `PromptEntry.source_of_truth()` returns the live object/constant, so the `is` test can hold.
- `render()` builds the prompt with the SAME builder the live path uses where one exists (e.g.
  `loop.py`'s `f"{SYSTEM_PROMPT}\n\n{language_directive(locale)}"`), not a re-implementation. Where the
  live builder needs runtime data (a student, a library), pass a representative SAMPLE and report it as
  an interpolation span so the UI can chip it.
- `call_sites` is a tuple of `"path/to/file.py:LINE"` strings, used only by the completeness test.
- Cover at minimum: `chat.system`, `chat.grounding`, `chat.no_hits`, `curriculum.system`,
  `curriculum.outline`, `lesson.draft`, `lesson.gap`, `retrieval.translate`, `retrieval.grounded`,
  `artifacts.generate`, `ocr.transcribe`, `ocr.figure`, `shared.language_directive`,
  `shared.student_brief`, `tools.descriptions`. Group them by `flow`.
- Do NOT invent Greek by machine-translating the English prompt. Write a genuine plain-Greek
  explanation of what the prompt DOES and WHEN it runs. The prompt stays English and untouched.

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_prompts_registry.py -q`
Expected: PASS

- [ ] **Step 5: Full gate**

Run: `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q`
Expected: 1149 + your new tests, 0 regressions.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/prompts/ apps/api/tests/test_prompts_registry.py
git commit -m "feat(prompts): a registry that points at the prompts, never copies them

The completeness test fails when someone adds a 16th provider call site without
registering it — which is what stops the viewer from silently going stale."
```

---

### Task P2: Overrides — table, resolution, routes

**Files:**
- Create: `apps/api/alembic/versions/<rev>_prompt_override.py`, `apps/api/app/models/prompt.py`, `apps/api/app/routers/prompts.py`
- Modify: `apps/api/app/prompts/registry.py` (add `resolve`), `apps/api/app/main.py`
- Test: `apps/api/tests/test_prompts_api.py`

**Interfaces:**
- Consumes: `REGISTRY`, `Slice` (P1).
- Produces: `GET /prompts`, `GET /prompts/{id}`, `PUT /prompts/slices/{slice_id}`, `DELETE /prompts/slices/{slice_id}`, `GET /prompts/slices/{slice_id}/history`. Task P3 consumes these.

Schema (additive; `down_revision = "a1b2c3d4e5f6"`):

```
prompt_override:         slice_id varchar(80) PK, text text NOT NULL, created_at, updated_at
prompt_override_history: id uuid PK, slice_id varchar(80) NOT NULL, text text NOT NULL, replaced_at
```

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_prompts_api.py
def test_default_comes_from_code_when_no_override(client):
    """Defaults live in CODE — diffable, reviewable, and the reset target.
    Overrides layer on top. Never the other way round."""
    r = client.get("/prompts/lesson.draft")
    assert r.status_code == 200
    assert r.json()["slices"][0]["effective"] == r.json()["slices"][0]["default"]


def test_saving_an_override_then_resetting_restores_the_code_default(client):
    sid = "student.pitch"
    client.put(f"/prompts/slices/{sid}", json={"text": "Γράψε πιο απλά."})
    assert client.get("/prompts/student.brief").json()["slices"][0]["effective"] == "Γράψε πιο απλά."
    client.delete(f"/prompts/slices/{sid}")
    body = client.get("/prompts/student.brief").json()["slices"][0]
    assert body["effective"] == body["default"]


def test_an_edit_that_drops_a_required_placeholder_is_rejected_with_a_CODE(client):
    """A KeyError at call time is a 500 in the tutor's face. And he never sees a
    stack trace — he gets a machine-readable code the web turns into one Greek
    sentence."""
    r = client.put("/prompts/slices/lesson.draft.length", json={"text": "Make it long."})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "missing_placeholder"


def test_history_lets_a_bad_edit_be_recovered(client):
    sid = "student.pitch"
    client.put(f"/prompts/slices/{sid}", json={"text": "first"})
    client.put(f"/prompts/slices/{sid}", json={"text": "second"})
    hist = client.get(f"/prompts/slices/{sid}/history").json()
    assert [h["text"] for h in hist] == ["first"]


def test_a_locked_prompt_exposes_no_editable_slice(client):
    """SYSTEM_PROMPT is 1,301 chars of guards, each added because something broke.
    It is readable and not editable."""
    body = client.get("/prompts/chat.system").json()
    assert body["text"], "must be readable"
    assert body["slices"] == [] or all(s["kind"] == "append" for s in body["slices"])


def test_editing_a_prefix_slice_reports_its_one_off_cache_cost(client):
    """CURRICULUM_SYSTEM is INSIDE the cached prefix. An edit re-mints it once.
    corpus.py's whole premise is that a cache mistake is invisible until the
    invoice arrives a month later — so this is SHOWN, not hidden."""
    body = client.get("/prompts/curriculum.system").json()
    assert body["cache_cost_warning"] is True
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** — model, migration, `resolve(slice_id)`, router. Errors follow the existing `code` convention (`routers/settings.py` is the reference). Snapshot the previous text into history on every write.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Apply the migration and prove existing data survives**

```bash
docker compose exec -T api alembic upgrade head && docker compose exec -T api alembic current
docker compose exec -T postgres psql -U guitar -d guitar -c "select count(*) from page;"   # still 140
```

- [ ] **Step 6: Full gate + commit**

---

### Task P3: The Settings card

**Files:**
- Create: `apps/web/src/components/settings/prompt-list.tsx`
- Modify: `apps/web/src/lib/api.ts`, `apps/web/src/app/[locale]/(cockpit)/settings/page.tsx`, `apps/web/src/messages/{el,en}.json`
- Test: `apps/web/tests/` (match the existing convention there)

**Interfaces:**
- Consumes: the P2 routes.

Follow `settings/page.tsx` EXACTLY: client component, `useTranslations`, `Card`/`Button`/`Textarea`,
optimistic update reverting on failure, `data-testid` throughout, `t.has()` before rendering an error code.

Shape, grouped by flow, collapsed by default:

```
┌──────────────────────────────────────────────┐
│ Ο βοηθός συνομιλίας                          │
│ Τι κάνει: ...   Πότε τρέχει: ...             │  <- Greek, ours, ALONGSIDE
├──────────────────────────────────────────────┤
│ You are the guitar tutor's copilot. You do   │  <- verbatim, read-only, selectable
│ NOT have his students, curricula, [...]      │
│   [ΚΛΕΙΔΩΜΕΝΟ: εγγυάται αληθινές παραπομπές] │
├──────────────────────────────────────────────┤
│ Επιπλέον οδηγίες (προαιρετικό)               │  <- append slot
│ [Επαναφορά] [Ιστορικό]          [Αποθήκευση] │
└──────────────────────────────────────────────┘
```

- [ ] **Step 1: Write the failing spec** — the card lists prompts; expanding one shows the verbatim English text AND a Greek description; a locked prompt has no textarea; saving an append slot persists.
- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement.** Interpolated variables render as chips with their sample value — never hidden. A prefix-slice edit shows its one-off cost in Greek before saving ("αυτή η αλλαγή κοστίζει μία φορά ~$X").
- [ ] **Step 4: Run to verify it passes.** `npx tsc --noEmit` clean.
- [ ] **Step 5: REBUILD AND REDEPLOY** — `docker compose up -d --build web`, then confirm at http://localhost:8790/el/settings. A source fix that is not deployed is not a fix.
- [ ] **Step 6: Commit**

---

## Self-Review

**Spec coverage:** registry ✅ P1 · annotation-not-rewriting ✅ P1 · slices ✅ P2 · storage+history ✅ P2 · validation ✅ P2 · cache warning ✅ P2/P3 · UI ✅ P3 · completeness test ✅ P1.

**Deliberately deferred:** slice *extraction* by byte-identical refactor (the spec's mechanism for earning more slices later) — ship the two safe ones first. A prompt playground/dry-run — worth wanting, not this plan.

**Placeholder scan:** none.

**Type consistency:** `PromptEntry`/`Slice`/`REGISTRY`/`render`/`resolve` consistent across P1–P3; `slice_id` is the PK in P2 and the path param in P3.
