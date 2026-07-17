# Ingest Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the tutor's four new books into the library with text we trust — re-OCR'd by Claude rather than inheriting someone else's Tesseract — and make the over-budget gate honest before the corpus hits 98.8% of it.

**Architecture:** The font in a PDF's text layer tells us what the text *is*: a real font means publisher text (trust it, free); `GlyphLessFont` means an invisible OCR layer over a scan (discard it, re-OCR). Routing on that fact replaces two coverage/density heuristics that were measured to fail. Vision runs through a new, narrowly-scoped `/v1/vision` endpoint on the existing `claude-bridge` container (`--tools Read` only, read-only media mount), so OCR uses the tutor's Claude subscription today and switches to a real API key later by changing one setting.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + Postgres 16/pgvector; PyMuPDF (`fitz`) for PDF inspection and rasterisation; the `claude-bridge` sidecar (`tools/claude_bridge/bridge.py`) wrapping `claude -p`; pytest; Next.js + Playwright for the frontend gate.

## Global Constraints

- **Prompt quality is not negotiable.** Never simplify a prompt to make it readable. See `docs/superpowers/specs/2026-07-17-prompt-transparency-design.md`.
- **The tutor is a total beginner with computers** (`settings/page.tsx:22-37`). He never sees JSON, a stack trace, a status code, or an untranslated English string. Every failure surfaces as a machine-readable `code` that the web turns into one Greek sentence.
- **Greek (`el`) is the default locale.** Every user-facing string ships in both `el` and `en`.
- **Migrations are additive only.** `alembic upgrade head` runs in the api container's CMD at boot; an app update must never destroy or rewrite the tutor's existing rows. Current head: `d4f1a90c7b28`.
- **Wasted or duplicated LLM spend is the top severity class.** Re-OCR is explicit, resumable, and never automatic.
- **`/v1/complete` on the bridge keeps `--tools ""`.** Only the new `/v1/vision` endpoint may enable a tool, and only `Read`.
- **Tests:** `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q` is the gate. Live-model tests are `-m integration` and are not part of it.
- **No SQLite, ever** (Greek ILIKE + pgvector break silently).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/api/app/brain/textlayer.py` | **new.** Pure functions: what kind of text layer a page has, and whether a page's text is OCR garbage. No DB, no I/O, no model. |
| `apps/api/alembic/versions/*_page_text_source.py` | **new.** Adds `page.text_source`, `page.ocr_reason`. Additive. |
| `apps/api/app/models/knowledge.py` | Modify: two columns on `Page`. |
| `apps/api/app/brain/paginate.py` | Modify: route on `textlayer.text_layer_kind` instead of truthiness. |
| `tools/claude_bridge/bridge.py` | Modify: add `/v1/vision` (`--tools Read`, media-root-scoped). |
| `apps/api/app/llm/claude_cli.py` | Modify: `vision()` calls the bridge instead of delegating to Qwen. |
| `apps/api/app/config.py` | Modify: `ocr_provider`, `ocr_render_dpi`. |
| `apps/api/app/llm/factory.py` | Modify: `get_ocr_provider()`. |
| `apps/api/app/brain/ocr.py` | Modify: render at `ocr_render_dpi`, record provenance, screen garbage. |
| `apps/api/app/curriculum/corpus.py` | Modify: `prefix_messages` honours `fits`. |
| `apps/api/app/agent/loop.py` | Modify: search before the named-song decline. |

---

### Task 1: The text-layer detector

The one fact everything else routes on. Pure, so it is cheap to test exhaustively.

**Files:**
- Create: `apps/api/app/brain/textlayer.py`
- Test: `apps/api/tests/test_brain_textlayer.py`

**Interfaces:**
- Consumes: nothing (pure; takes a `fitz.Page`).
- Produces: `text_layer_kind(page) -> Literal["none","ocr","digital"]`, `looks_like_ocr_garbage(text: str) -> bool`, and the constant `GLYPHLESS_MARKER = "GlyphLess"`. Tasks 3 and 8 consume both functions.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_brain_textlayer.py
"""The font IS the signal. Two heuristics were measured to FAIL before this one:

  1. raster coverage > 25% -> flags 100% of Hunter and Gallagher, because every
     page of a scanned book has a full-page image under it.
  2. chars < 0.5 * book median -> misses Powers p.11 (502 chars, 588 median),
     the exact page the rule was written for.

`GlyphLessFont` is what OCR tools name the invisible text layer they lay over a
scan. It is a fact read out of the PDF, not a guess about it.
"""
import fitz
import pytest

from app.brain.textlayer import looks_like_ocr_garbage, text_layer_kind


def _page(make) -> fitz.Page:
    doc = fitz.open()
    page = doc.new_page()
    make(page)
    return page


def test_no_text_layer_is_none():
    page = _page(lambda p: None)
    assert text_layer_kind(page) == "none"


def test_real_font_is_digital():
    page = _page(lambda p: p.insert_text((72, 72), "Many foot controllers have", fontname="helv"))
    assert text_layer_kind(page) == "digital"


def test_glyphless_font_is_ocr(monkeypatch):
    # Building a real GlyphLessFont PDF needs an OCR toolchain; the contract we
    # care about is the font-name rule, so drive it through get_text("dict").
    page = _page(lambda p: p.insert_text((72, 72), "x", fontname="helv"))
    monkeypatch.setattr(
        type(page), "get_text",
        lambda self, kind="text", **kw: {
            "blocks": [{"lines": [{"spans": [{"font": "GlyphLessFont"}]}]}]
        } if kind == "dict" else "some ocr text",
    )
    assert text_layer_kind(page) == "ocr"


@pytest.mark.parametrize("text", [
    # Kahn p.40, verbatim — what Tesseract produced for a page that renders
    # blank in BOTH MuPDF and poppler. 544 chars the pipeline currently ingests
    # as if it were the page's content.
    "7 ipgges x \na \nRar \nek \nen ee \n& \neile \na= \n@ \nFilip \n«@ \n' \n= \nae \n¢ \n® a \na ie \n_ \n- \n¥ \n= \n, \n» \nve \né \n—- \n= \na \nra \ni \n2“ \n-ohoutea-% \n*. \n® \n' \nPre el \nif \nrene \nGS \n210) ",
])
def test_ocr_garbage_is_detected(text):
    assert looks_like_ocr_garbage(text) is True


@pytest.mark.parametrize("text", [
    # Hunter p.57, verbatim — excellent Tesseract prose. Must NOT be flagged.
    "difficult to replicate with overdrive or distortion pedals. All of this might "
    "seem just a little too easy to be true, but it works for very scientific "
    "reasons that have to do with the electrical interaction between a guitar and "
    "a tube amplifier. Every note you pluck is transmitted to the amp in the form "
    "of an electrical current of a certain voltage.",
    "",                      # empty is not garbage; it is empty
    "Chapter 3",             # short and legitimate
])
def test_good_text_is_not_garbage(text):
    assert looks_like_ocr_garbage(text) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_brain_textlayer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.brain.textlayer'`

- [ ] **Step 3: Write the implementation**

```python
# apps/api/app/brain/textlayer.py
"""What a PDF page's text layer IS — read out of the PDF, not guessed at.

MEASURED, 2026-07-17, on the tutor's four books:

    Powers      LiberationSerif  -> digital text (+ raster tab images)
    Hunter      GlyphLessFont    -> 360dpi SCAN + someone else's Tesseract
    Gallagher   GlyphLessFont    -> 360dpi SCAN + someone else's Tesseract
    Kahn        GlyphLessFont    -> 360dpi SCAN + someone else's Tesseract

`paginate.py` used to take ANY text layer for free and mark the page `ready`,
which means no model ever looks at it again. On a real-font PDF that is a gift.
On these three it silently adopts Tesseract's output as the permanent truth of
the library, and that output loses EVERY fraction glyph: zero of `¼½¾⅓⅔⅛`
survive in 2,012,859 characters. Kahn p.63 reads "¼-inch stereo cables"; the
inherited layer says "4-inch stereo cables" — a confident, plausible, wrong fact
in the exact vocabulary this library exists to teach, and one that cannot be
repaired by regex because some "4-inch" references are real (a 4-inch speaker
dust cap). The only thing that can tell them apart is looking at the page.

TWO HEURISTICS WERE TRIED FIRST AND MEASURED TO FAIL. Recorded so they are not
re-proposed:

  1. "raster coverage > 25% => needs vision" flags 100% of Hunter and Gallagher.
     Every page of a scanned book has a full-page image under it, so coverage
     carries no signal on exactly the books that need one.
  2. "chars < 0.5 * the book's median => needs vision" would not have caught
     Powers p.11 (502 chars against a 588-char median) — the page it was
     written for.

The font name is not a heuristic. It is a fact, and it is free.
"""
from __future__ import annotations

from typing import Literal

# What OCR tools name the invisible text layer they lay over a scan. Substring,
# not equality: the exact name varies by tool and version ("GlyphLessFont" is
# Tesseract's; others differ in suffix).
GLYPHLESS_MARKER = "GlyphLess"

TextLayerKind = Literal["none", "ocr", "digital"]


def text_layer_kind(page) -> TextLayerKind:
    """`none` | `ocr` | `digital` — from the page's fonts.

    `digital` means real embedded fonts: publisher text, trustworthy, free.
    `ocr` means every span is glyphless: an invisible layer over a scan, whose
    quality is whatever some upstream tool produced. We do not inherit it.
    `none` means no text at all — the pre-existing vision path.
    """
    fonts = {
        (span.get("font") or "")
        for block in page.get_text("dict").get("blocks", ())
        for line in block.get("lines", ())
        for span in line.get("spans", ())
    }
    if not fonts or not (page.get_text() or "").strip():
        return "none"
    if all(GLYPHLESS_MARKER in font or not font for font in fonts):
        return "ocr"
    return "digital"


# A page whose render is blank and whose text is noise still enters the library
# as content today. Kahn p.40 is the measured case: it renders blank in BOTH
# MuPDF and poppler (so its content is unrecoverable by any renderer — the page
# is damaged in the PDF itself), and Tesseract wrote 544 characters of
# "7 ipgges x a Rar ek en ee & eile a=" for it, which the pipeline chunks,
# embeds, and feeds to the curriculum as if it were the page.
#
# 9 of 617 pages (~1.5%) across the three scanned books are like this. The tell
# is unmistakable and needs no model: real prose is mostly multi-character words.
_MIN_LEN_TO_JUDGE = 40
_MIN_TOKENS_TO_JUDGE = 20
_STUB_TOKEN_RATIO = 0.55


def looks_like_ocr_garbage(text: str) -> bool:
    """True when `text` is OCR noise rather than a transcription.

    Deliberately conservative: a false positive costs one vision call, a false
    negative puts noise in the citation store permanently.
    """
    text = (text or "").strip()
    if len(text) < _MIN_LEN_TO_JUDGE:
        return False        # too short to judge — "Chapter 3" is not garbage
    tokens = text.split()
    if len(tokens) < _MIN_TOKENS_TO_JUDGE:
        return False
    stubs = sum(1 for token in tokens if len(token) <= 2)
    return stubs / len(tokens) > _STUB_TOKEN_RATIO
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_brain_textlayer.py -q`
Expected: PASS — 7 passed

- [ ] **Step 5: Verify against the real books, not just fixtures**

This is the step that matters: the fixtures encode what I believe, the books are what is true.

```bash
cd /mnt/nvme2TB/guitar_tutor
docker compose cp "books/Guitar Exercises Made Simple (Maxwell Powers).pdf" api:/tmp/p.pdf
docker compose cp "books/Tone Manual Discovering Your Ultimate Electric Guitar Sound (Dave Hunter).pdf" api:/tmp/h.pdf
docker compose exec -T api python3 -c "
import fitz
from app.brain.textlayer import text_layer_kind
for name, path in [('Powers','/tmp/p.pdf'), ('Hunter','/tmp/h.pdf')]:
    d = fitz.open(path)
    kinds = [text_layer_kind(d[i]) for i in range(min(40, d.page_count))]
    print(name, {k: kinds.count(k) for k in set(kinds)})
    d.close()
"
```

Expected: `Powers {'digital': 39, 'none': 1}` and `Hunter {'ocr': 40}`
(Powers p.1 is a full-page cover image with no text — correctly `none`.)

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/brain/textlayer.py apps/api/tests/test_brain_textlayer.py
git commit -m "feat(brain): route OCR on the text layer's FONT, not on coverage

The font is a fact in the PDF; coverage and char-density were guesses, and both
were measured to fail on the actual books. GlyphLessFont => an invisible OCR
layer over a scan, whose 100% fraction-glyph loss we will not inherit."
```

---

### Task 2: Page provenance columns

Without this, Task 3 is a silent behaviour change — and the tutor cannot see which pages fell back or judge whether the detector was right.

**Files:**
- Create: `apps/api/alembic/versions/a1b2c3d4e5f6_page_text_source.py`
- Modify: `apps/api/app/models/knowledge.py:108` (after `ocr_attempts`)
- Test: `apps/api/tests/test_brain_schema.py`

**Interfaces:**
- Consumes: `Page` from Task 1's caller (`app.models.knowledge`).
- Produces: `Page.text_source: str | None` (`"text_layer"` | `"qwen"` | `"claude"` | `"failed"`) and `Page.ocr_reason: str | None` (`"no_text_layer"` | `"inherited_ocr"` | `"image_region"` | `"ocr_garbage"`). Tasks 3, 8 and 11 write them; the Reader reads them.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/api/tests/test_brain_schema.py
def test_page_records_where_its_text_came_from(db):
    """`ready` used to mean "no model will ever look at this page again" with no
    record of who wrote it. Provenance is what makes Task 3's routing auditable
    instead of a silent behaviour change."""
    from app.models.knowledge import KnowledgeSource, Page

    source = KnowledgeSource(type="pdf", title="t", status="ready")
    db.add(source)
    db.flush()
    page = Page(
        source_id=source.id, page_no=1, text="x", status="ready",
        text_source="text_layer", ocr_reason=None,
    )
    db.add(page)
    db.commit()
    db.refresh(page)
    assert page.text_source == "text_layer"
    assert page.ocr_reason is None


def test_page_text_source_defaults_to_null_for_existing_rows(db):
    """Additive migration: rows written before this column exists stay valid.
    NULL means "written before we tracked this", not "unknown failure"."""
    from app.models.knowledge import KnowledgeSource, Page

    source = KnowledgeSource(type="pdf", title="t", status="ready")
    db.add(source)
    db.flush()
    page = Page(source_id=source.id, page_no=1, text="x", status="ready")
    db.add(page)
    db.commit()
    db.refresh(page)
    assert page.text_source is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_brain_schema.py -k text_source -q`
Expected: FAIL — `TypeError: 'text_source' is an invalid keyword argument for Page`

- [ ] **Step 3: Add the columns to the model**

```python
# apps/api/app/models/knowledge.py — immediately after `ocr_attempts`
    # WHERE this page's text came from. NULL for rows written before this
    # column existed (an additive migration must not invent history).
    #
    #   "text_layer" — a real embedded font: publisher text, taken for free.
    #   "qwen"       — the local VL model transcribed the scan.
    #   "claude"     — Claude transcribed the scan (subscription or API).
    #   "failed"     — every attempt failed; `ocr_error` says why.
    #
    # Shown per page in the Reader. Without it, `brain/paginate.py`'s routing is
    # a silent behaviour change the tutor cannot inspect or overrule.
    text_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # WHY this page was sent to vision — the detector's reason, in its own words,
    # so a wrong call is diagnosable rather than merely wrong.
    #   "no_text_layer" | "inherited_ocr" | "image_region" | "ocr_garbage"
    ocr_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
```

- [ ] **Step 4: Write the migration**

```python
# apps/api/alembic/versions/a1b2c3d4e5f6_page_text_source.py
"""page.text_source + page.ocr_reason

Revision ID: a1b2c3d4e5f6
Revises: d4f1a90c7b28
Create Date: 2026-07-17

ADDITIVE. Both columns are nullable with no server default: NULL means "this row
predates provenance tracking", which is true and is different from "unknown".
`alembic upgrade head` runs in the api container's CMD at boot, so this migrates
the tutor's live library in place without touching a single existing value.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d4f1a90c7b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("page", sa.Column("text_source", sa.String(length=20), nullable=True))
    op.add_column("page", sa.Column("ocr_reason", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("page", "ocr_reason")
    op.drop_column("page", "text_source")
```

- [ ] **Step 5: Apply the migration and run the tests**

```bash
cd /mnt/nvme2TB/guitar_tutor
docker compose exec -T api alembic upgrade head
docker compose exec -T api alembic current          # expect a1b2c3d4e5f6 (head)
cd apps/api && ./.venv/bin/python -m pytest tests/test_brain_schema.py -q
```
Expected: `alembic current` prints `a1b2c3d4e5f6 (head)`; tests PASS.

- [ ] **Step 6: Prove the tutor's existing data survived**

The constraint is "when I send him an update of the app, the data of the user must be the same." Assert it, don't assume it.

```bash
docker compose exec -T postgres psql -U guitar -d guitar -c \
  "select count(*) as pages, count(text_source) as with_provenance from page;"
```
Expected: `pages = 83`, `with_provenance = 0` — every pre-existing row intact, none invented.

- [ ] **Step 7: Commit**

```bash
git add apps/api/alembic/versions/a1b2c3d4e5f6_page_text_source.py \
        apps/api/app/models/knowledge.py apps/api/tests/test_brain_schema.py
git commit -m "feat(db): record where each page's text came from

Additive; NULL means 'predates tracking'. Makes the OCR routing auditable in the
Reader instead of a silent behaviour change."
```

---

### Task 3: Route pagination on the text-layer kind

**Files:**
- Modify: `apps/api/app/brain/paginate.py:78-92`
- Test: `apps/api/tests/test_brain_paginate.py`

**Interfaces:**
- Consumes: `text_layer_kind` (Task 1); `Page.text_source` / `Page.ocr_reason` (Task 2).
- Produces: pages whose `status` is `"pending"` for anything needing vision, with `ocr_reason` set. Task 8's `ocr_source` picks them up unchanged.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_brain_paginate.py
"""paginate must not inherit someone else's OCR.

Before: `layer = page.get_text(); status = "ready" if layer else "pending"`.
Any text at all meant no model would ever see the page — so Hunter, Gallagher
and Kahn entered the library as Tesseract output, fraction glyphs and all.
"""
from unittest.mock import patch

from app.brain.paginate import _paginate_pdf


def _fake_doc(kinds: list[str]):
    """A doc whose pages report the given text_layer_kind."""
    class FakePage:
        def __init__(self, kind):
            self.kind = kind
        def get_pixmap(self, dpi=110):
            class Pix:
                def tobytes(self, fmt): return b"\xff\xd8jpeg"
            return Pix()
        def get_text(self, kind="text", **kw):
            return "" if self.kind == "none" else "some text on the page"
    class FakeDoc:
        page_count = len(kinds)
        def __init__(self): self._pages = [FakePage(k) for k in kinds]
        def __getitem__(self, i): return self._pages[i]
        def close(self): pass
    return FakeDoc()


def test_digital_text_is_taken_for_free(db, tmp_media):
    doc = _fake_doc(["digital"])
    with patch("app.brain.paginate.fitz.open", return_value=doc), \
         patch("app.brain.paginate.text_layer_kind", return_value="digital"):
        pages = _paginate_pdf(db, tmp_media.source_id, b"%PDF")
    assert pages[0].status == "ready"
    assert pages[0].text_source == "text_layer"
    assert pages[0].ocr_reason is None


def test_inherited_ocr_is_NOT_taken_for_free(db, tmp_media):
    """The Hunter/Gallagher/Kahn case. A GlyphLessFont layer is someone else's
    Tesseract; we re-OCR rather than adopt its 100% fraction loss."""
    doc = _fake_doc(["ocr"])
    with patch("app.brain.paginate.fitz.open", return_value=doc), \
         patch("app.brain.paginate.text_layer_kind", return_value="ocr"):
        pages = _paginate_pdf(db, tmp_media.source_id, b"%PDF")
    assert pages[0].status == "pending"
    assert pages[0].ocr_reason == "inherited_ocr"
    assert pages[0].text is None, "the inherited layer must not be kept as truth"


def test_no_text_layer_still_goes_to_vision(db, tmp_media):
    doc = _fake_doc(["none"])
    with patch("app.brain.paginate.fitz.open", return_value=doc), \
         patch("app.brain.paginate.text_layer_kind", return_value="none"):
        pages = _paginate_pdf(db, tmp_media.source_id, b"%PDF")
    assert pages[0].status == "pending"
    assert pages[0].ocr_reason == "no_text_layer"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_brain_paginate.py -q`
Expected: FAIL — `AttributeError: <module 'app.brain.paginate'> does not have the attribute 'text_layer_kind'`

- [ ] **Step 3: Rewrite the routing**

```python
# apps/api/app/brain/paginate.py — add to the imports
from app.brain.textlayer import text_layer_kind
```

Replace lines 78-92 (the `for i in range(doc.page_count)` body) with:

```python
        for i in range(doc.page_count):
            page_no = i + 1
            pix = doc[i].get_pixmap(dpi=RENDER_DPI)
            rel = os.path.join(str(source_id), f"{page_no:04d}.jpg")
            with open(os.path.join(settings.media_dir, rel), "wb") as fh:
                fh.write(pix.tobytes("jpeg"))

            # WHAT the text layer is decides whether we may keep it. A real font
            # is publisher text — take it, free, and no model ever needs to look.
            # A GlyphLessFont layer is an invisible OCR layer over a scan: it is
            # someone else's Tesseract, it lost every fraction glyph in the book
            # (measured: 0 of `¼½¾` survive in 2,012,859 chars across the three
            # scanned books), and adopting it would make that permanent, because
            # `ready` means nothing revisits the page. So we drop it and re-read
            # the scan ourselves.
            kind = text_layer_kind(doc[i])
            if kind == "digital":
                text, status, reason, provenance = (
                    (doc[i].get_text() or "").strip(), "ready", None, "text_layer")
            else:
                text, status, provenance = None, "pending", None
                reason = "no_text_layer" if kind == "none" else "inherited_ocr"

            page = Page(
                source_id=source_id, page_no=page_no, image_path=rel,
                text=text or None, status=status,
                text_source=provenance, ocr_reason=reason,
            )
            db.add(page)
            pages.append(page)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_brain_paginate.py -q`
Expected: PASS — 3 passed

- [ ] **Step 5: Run the whole suite — this changes a load-bearing path**

Run: `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q`
Expected: PASS. If `test_library_*` or `test_ocr_*` fail, they encode the old
"any text layer means ready" contract; update them to the new routing and say so
in the commit rather than loosening the assertion.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/brain/paginate.py apps/api/tests/test_brain_paginate.py
git commit -m "fix(brain): stop inheriting someone else's OCR

A GlyphLessFont text layer is an invisible OCR layer over a scan. Taking it for
free marked the page `ready`, which means nothing ever looks at it again — so
Tesseract's 100% fraction-glyph loss became the permanent truth of the library.
Real fonts are still free; scans get re-read."
```

---

### Task 4: `/v1/vision` on the bridge

**Files:**
- Modify: `tools/claude_bridge/bridge.py`
- Modify: `docker-compose.yml` (read-only media mount on `claude-bridge`)
- Test: `tools/claude_bridge/test_bridge.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `POST /v1/vision {"image_path": str, "prompt": str} -> {"text": str, "cost_usd": float}`. `image_path` is relative to the media root. Task 5 is its only client.

- [ ] **Step 1: Write the failing test**

```python
# append to tools/claude_bridge/test_bridge.py
"""`/v1/complete` runs `claude -p --tools ""` — every built-in tool disabled,
deliberately: "Without it this is a coding agent."

Vision cannot work that way: `claude -p` has no image parameter, so the only
route to a page scan is the `Read` tool plus a real file. That is a genuine hole
in the bridge's posture, so it is opened exactly once, as narrowly as possible:
a SEPARATE endpoint, `Read` and nothing else, and a path that must resolve
inside the read-only media root. `/v1/complete` is untouched.
"""
import pytest

from bridge import _vision_argv, _resolve_media_path, MediaPathError


def test_vision_enables_read_and_nothing_else():
    argv = _vision_argv("/media/abc/0001.jpg", "transcribe it")
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == "Read"
    assert '--tools ""' not in " ".join(argv)


def test_complete_still_disables_every_tool():
    from bridge import _complete_argv
    argv = _complete_argv("hi", None)
    assert argv[argv.index("--tools") + 1] == ""


@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "/etc/passwd",
    "abc/../../../root/.ssh/id_rsa",
    "abc/0001.jpg\x00.png",
])
def test_traversal_is_rejected_before_claude_runs(evil):
    """A 400, not a `claude` invocation. The subprocess must never see it."""
    with pytest.raises(MediaPathError):
        _resolve_media_path(evil, media_root="/media")


def test_legitimate_path_resolves():
    assert _resolve_media_path("abc/0001.jpg", media_root="/media") == "/media/abc/0001.jpg"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /mnt/nvme2TB/guitar_tutor && ./apps/api/.venv/bin/python -m pytest tools/claude_bridge/ -q`
Expected: FAIL — `ImportError: cannot import name '_vision_argv' from 'bridge'`

- [ ] **Step 3: Implement**

```python
# tools/claude_bridge/bridge.py

import os
import os.path

MEDIA_ROOT = os.environ.get("CLAUDE_BRIDGE_MEDIA_ROOT", "/media")


class MediaPathError(ValueError):
    """The requested path is not inside the media root. A 400, never a subprocess."""


def _resolve_media_path(rel: str, *, media_root: str = MEDIA_ROOT) -> str:
    """`rel` -> an absolute path proven to sit inside `media_root`.

    Validated BEFORE `claude` starts. The bridge holds the tutor's real OAuth
    session; a path-traversal that reaches the Read tool is an arbitrary-file-read
    with his credentials attached. `commonpath` (not `startswith`) so that
    `/media-evil/x` cannot pass as `/media`.
    """
    if "\x00" in rel:
        raise MediaPathError("NUL in path")
    root = os.path.realpath(media_root)
    target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    if os.path.commonpath([root, target]) != root:
        raise MediaPathError(f"path escapes the media root: {rel!r}")
    return target


def _vision_argv(abs_image_path: str, prompt: str) -> list[str]:
    """`claude -p` with EXACTLY ONE tool enabled.

    `--tools Read` rather than `--tools ""`: `claude -p` takes no image
    parameter, so reading a file is the only way a page scan reaches the model.
    This is why it is a separate endpoint — `/v1/complete` keeps every tool off,
    and this one may open a single, named door.
    """
    return [
        "claude", "-p",
        "--bare",
        "--tools", "Read",
        "--add-dir", os.path.dirname(abs_image_path),
        f"{prompt}\n\nThe page image is at: {abs_image_path}",
    ]
```

Add the handler beside the existing `/v1/complete` route, reusing its token check,
concurrency semaphore and timeout, and returning `{"text": ..., "cost_usd": ...}`.

- [ ] **Step 4: Mount the media directory read-only**

```yaml
# docker-compose.yml, under claude-bridge:
    volumes:
      # ... the existing ~/.claude mount ...
      # The page scans, READ-ONLY. The bridge may read a rendered page and
      # nothing else — not the repo, not the DB, not the rest of the host.
      - ./media:/media:ro
    environment:
      CLAUDE_BRIDGE_MEDIA_ROOT: "/media"
```

- [ ] **Step 5: Run the tests**

Run: `cd /mnt/nvme2TB/guitar_tutor && ./apps/api/.venv/bin/python -m pytest tools/claude_bridge/ -q`
Expected: PASS — 6 passed

- [ ] **Step 6: Prove it end-to-end against a real page**

The unit tests assert the argv; this asserts Claude actually reads the scan.

```bash
docker compose up -d --build claude-bridge
docker compose exec -T api python3 -c "
from app.llm.claude_cli import ClaudeCLIProvider
import fitz
d = fitz.open('/tmp/k.pdf')
open('/media/spike.jpg','wb').write(d[62].get_pixmap(dpi=150).tobytes('jpeg'))
print(ClaudeCLIProvider(model='claude-sonnet-5').vision(open('/media/spike.jpg','rb').read(), 'Transcribe all text verbatim.')[:400])
"
```
Expected: real transcription containing **`¼-inch`** — the glyph Tesseract turned
into `4-inch`. If it says `4-inch`, the render or the prompt is wrong; stop and
diagnose rather than proceeding.

- [ ] **Step 7: Commit**

```bash
git add tools/claude_bridge/bridge.py tools/claude_bridge/test_bridge.py docker-compose.yml
git commit -m "feat(bridge): /v1/vision — claude -p with Read, scoped to a read-only media mount

claude -p takes no image param, so Read + a real file is the only route to a page
scan. Opened as a separate endpoint with one tool and a validated path rather
than by relaxing /v1/complete, which keeps --tools \"\"."
```

---

### Task 5: `ClaudeCLIProvider.vision()` stops delegating to Qwen

**Files:**
- Modify: `apps/api/app/llm/claude_cli.py:403-420`
- Test: `apps/api/tests/test_claude_cli.py`

**Interfaces:**
- Consumes: `POST /v1/vision` (Task 4).
- Produces: `ClaudeCLIProvider.vision(image_bytes, prompt, *, media_type="image/jpeg") -> str`, satisfying the `LLMProvider` ABC. Task 8 calls it through `get_ocr_provider()`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_claude_cli.py
def test_vision_no_longer_delegates_to_qwen(monkeypatch, tmp_path):
    """The docstring said "OCR STAYS ON QWEN. This method delegates, and that is
    the design." The design changed: Qwen inherits none of Claude's glyph
    fidelity, and `claude -p --tools Read` is measured to read `¼` correctly."""
    from app.llm.claude_cli import ClaudeCLIProvider

    calls = {}
    def fake_post(url, **kw):
        calls["url"] = url
        calls["json"] = kw.get("json")
        class R:
            status_code = 200
            def json(self): return {"text": "¼-inch stereo cables", "cost_usd": 0.017}
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr("app.llm.claude_cli.httpx.post", fake_post)
    provider = ClaudeCLIProvider(model="claude-sonnet-5")
    out = provider.vision(b"\xff\xd8jpeg", "Transcribe verbatim.")

    assert out == "¼-inch stereo cables"
    assert calls["url"].endswith("/v1/vision")
    assert "image_path" in calls["json"], "the bridge reads a file; it takes no base64"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_claude_cli.py -k vision -q`
Expected: FAIL — the current `vision()` delegates to `QwenVLLM` and never calls `httpx.post`.

- [ ] **Step 3: Implement**

Replace `ClaudeCLIProvider.vision` (`claude_cli.py:403`). The method writes the
bytes under the shared media root and posts the *path*; the bridge's `Read` tool
needs a file, and shipping base64 through a JSON body for an 11-megapixel page
would be gratuitous.

```python
    def vision(self, image_bytes: bytes, prompt: str, *, media_type: str = "image/jpeg") -> str:
        """One page scan -> its text, via `claude -p --tools Read`.

        THIS USED TO DELEGATE TO QWEN, and the old docstring defended that: "For
        77 pages of an ENGLISH book that Qwen was already measured transcribing
        faithfully, that buys nothing and spends a real slice of a 5-hour
        subscription cap." Two things changed.

        First, the library is no longer 77 pages of one book — it is 888 pages,
        three of whose four new books are SCANS carrying someone else's Tesseract
        layer that lost every fraction glyph in 2M characters. "¼-inch" is the
        standard guitar connector; the inherited text says "4-inch stereo
        cables", and a curriculum built on it teaches that.

        Second, `claude -p --allowedTools Read` was measured reading that exact
        page correctly, volunteering that it had zoomed to 400% to disambiguate
        the glyph. The subscription cost the old docstring worried about is real
        — ~40s and an agentic turn per page — which is why `ocr.py` runs this
        explicitly and resumably, never automatically on upload.
        """
        rel = self._stage_image(image_bytes, media_type=media_type)
        with self._mapped_errors():
            resp = httpx.post(
                f"{self._url}/v1/vision",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"image_path": rel, "prompt": prompt},
                timeout=self._timeout_s + 30,
            )
            resp.raise_for_status()
            body = resp.json()
        self._last_cost_usd = body.get("cost_usd")
        return (body.get("text") or "").strip()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_claude_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/llm/claude_cli.py apps/api/tests/test_claude_cli.py
git commit -m "feat(llm): claude_cli.vision() reads pages via the bridge, not Qwen

Qwen was the right call for 77 pages of one book. It is the wrong call for three
scanned books whose inherited text layer lost every fraction glyph."
```

---

### Task 6: `OCR_PROVIDER`, independent of `LLM_PROVIDER`

**Files:**
- Modify: `apps/api/app/config.py:110`, `apps/api/app/llm/factory.py`
- Modify: `.env.example`
- Test: `apps/api/tests/test_llm_factory.py`

**Interfaces:**
- Consumes: `resolve_llm_config()` / `LLMConfig` (`app.settings_store`).
- Produces: `get_ocr_provider() -> LLMProvider`, and `settings.ocr_provider: str | None`, `settings.ocr_render_dpi: int`. Task 8 calls `get_ocr_provider()` in place of `get_provider()`.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_llm_factory.py
def test_ocr_provider_defaults_to_the_chat_provider(monkeypatch):
    """Unset changes nothing for anyone. The knob exists to be ignored."""
    monkeypatch.setattr("app.config.settings.ocr_provider", None)
    monkeypatch.setattr("app.config.settings.llm_provider", "qwen")
    from app.llm.factory import get_ocr_provider, get_provider
    assert type(get_ocr_provider()) is type(get_provider())


def test_ocr_provider_can_differ_from_chat(monkeypatch):
    """The whole point: chat on the subscription (claude_cli, free), OCR on a
    real key (claude, pennies). Today `ocr.py` calls get_provider() zero-arg, so
    one knob picks both and this combination is unreachable."""
    monkeypatch.setattr("app.config.settings.llm_provider", "claude_cli")
    monkeypatch.setattr("app.config.settings.ocr_provider", "claude")
    monkeypatch.setattr("app.config.settings.llm_api_key", "sk-ant-test")
    from app.llm.claude import ClaudeProvider
    from app.llm.claude_cli import ClaudeCLIProvider
    from app.llm.factory import get_ocr_provider, get_provider
    assert isinstance(get_provider(), ClaudeCLIProvider)
    assert isinstance(get_ocr_provider(), ClaudeProvider)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_llm_factory.py -k ocr -q`
Expected: FAIL — `ImportError: cannot import name 'get_ocr_provider'`

- [ ] **Step 3: Implement**

```python
# apps/api/app/config.py — beside full_context_budget
    # WHICH provider transcribes a page scan. `None` = "whatever chat uses",
    # which is what every existing install gets.
    #
    # It exists because those two answers legitimately differ. `ocr.py` calls
    # `get_provider()` zero-arg, so today one knob picks both — and the
    # combination the tutor actually wants (chat on the subscription via
    # `claude_cli`, which costs nothing per token; OCR on a real key, which is
    # pennies and does not burn a 5-hour cap) is simply unreachable.
    ocr_provider: str | None = None
    # Pages routed to vision are re-rendered at this DPI. RENDER_DPI=110 in
    # paginate.py is a QWEN CEILING, not a quality choice: at 150dpi the local
    # vLLM rejects the image outright ("image item with length 2080 exceeds
    # pre-allocated encoder cache size 2048"). Claude is high-resolution tier —
    # 110dpi is 1,496 visual tokens, 150dpi is 2,714 — so pointing it at the
    # stored 110dpi JPEG silently caps its fidelity on exactly the small glyphs
    # that matter (`¼` vs `⅛` differ by a few pixels at page scale).
    ocr_render_dpi: int = 150
```

```python
# apps/api/app/llm/factory.py
def get_ocr_provider() -> LLMProvider:
    """The provider that transcribes a page scan.

    Defaults to `get_provider()`, so an install that never sets `OCR_PROVIDER`
    behaves exactly as before.
    """
    override = settings.ocr_provider
    if not override:
        return get_provider()
    cfg = resolve_llm_config()
    return _get_cached(LLMConfig(provider=override, model=cfg.model, api_key=cfg.api_key))
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_llm_factory.py -q`
Expected: PASS

- [ ] **Step 5: Document the knob**

```bash
# .env.example, under the LLM section
# WHICH provider reads page scans. Unset = the same one as LLM_PROVIDER.
# Set this when chat should run on the subscription (LLM_PROVIDER=claude_cli,
# free per token) but OCR should run on a real API key (pennies per book, and
# it does not spend the 5-hour subscription cap).
#   OCR_PROVIDER=claude
# Pages sent to vision are re-rendered at this DPI. 110 is a Qwen vLLM ceiling,
# not a quality choice; Claude is high-res tier and wants ~150.
#   OCR_RENDER_DPI=150
```

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/config.py apps/api/app/llm/factory.py .env.example apps/api/tests/test_llm_factory.py
git commit -m "feat(llm): OCR_PROVIDER, independent of LLM_PROVIDER

Chat on the subscription and OCR on a real key is the combination the tutor
wants, and one zero-arg get_provider() made it unreachable."
```

---

### Task 7: Make `fits` actually gate

**Files:**
- Modify: `apps/api/app/curriculum/corpus.py:252-273`
- Test: `apps/api/tests/test_curriculum_grounding.py`

**Interfaces:**
- Consumes: `LibraryContext` (unchanged).
- Produces: `prefix_messages(library)` that omits the library block when `not library.fits`. `draft.py:268`'s retrieval top-up becomes the substitute rather than an addition.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_curriculum_grounding.py
def test_oversized_library_is_NOT_shipped_whole():
    """corpus.py's docstring promises: "Above `full_context_budget` `fits` goes
    False and the caller degrades to per-module retrieval — WITH AN HONEST
    BANNER, never silently."

    It does not. `fits` is computed at corpus.py:208 and consulted by nobody:
    outline.py:148 and extend.py:160 call prefix_messages() unconditionally, and
    draft.py:268 ADDS retrieved passages on top of the still-complete library —
    strictly worse than either path alone. Meanwhile outline.py:361 sets
    `full_context: false` and tree-board.tsx:189 renders a banner reporting a
    degrade that never happened.

    Unreachable at 82K. The four new books put the corpus at 592,841 tokens
    against a 600,000 budget — 11 book pages of headroom.
    """
    from app.curriculum.corpus import LibraryContext, prefix_messages

    oversized = LibraryContext(
        text="<source id='S1' title='t'>[p.1] " + ("x" * 100) + "</source>",
        token_count=700_000, fits=False,
        page_index={"S1": {1}}, ref_to_source_id={},
        sources=[{"ref": "S1", "id": "x", "title": "t", "pages": 1, "chars": 100}],
    )
    messages = prefix_messages(oversized)
    body = "\n".join(m["content"] for m in messages)

    assert "[p.1]" not in body, "the library shipped whole despite fits=False"
    assert not any(m.get("cache") for m in messages), \
        "a 700K block must not be written to the cache at 1.25x"
    assert "too large" in body.lower() or "retrieval" in body.lower(), \
        "the model must be told why it is not seeing the library"


def test_fitting_library_is_unchanged():
    """The 82K path today, and the <=300K path after the canon lands. This is the
    regression guard: the fix must not alter the working case."""
    from app.curriculum.corpus import LibraryContext, prefix_messages

    fits = LibraryContext(
        text="<source id='S1' title='t'>[p.1] hello</source>",
        token_count=90_000, fits=True,
        page_index={"S1": {1}}, ref_to_source_id={}, sources=[],
    )
    messages = prefix_messages(fits)
    assert any(m.get("cache") for m in messages), "the stable prefix must still cache"
    assert "[p.1] hello" in "\n".join(m["content"] for m in messages)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_curriculum_grounding.py -k oversized -q`
Expected: FAIL — `AssertionError: the library shipped whole despite fits=False`
(This failure *is* the bug. Confirm you see it before fixing.)

- [ ] **Step 3: Implement**

```python
# apps/api/app/curriculum/corpus.py — inside prefix_messages, before the is_empty branch
    if not library.is_empty and not library.fits:
        # The docstring promised this for two days and never did it. Above the
        # budget the library block is NOT sent: `draft.py` tops the tail up with
        # retrieval, and that top-up is the substitute, not an addition. Shipping
        # both is what the code did before — strictly worse than either alone, and
        # it re-wrote a 600K block into the cache at 1.25x on every outline.
        messages.append({
            "role": "user",
            "content": (
                "The tutor's library is TOO LARGE to read in full for this course "
                f"({library.token_count:,} tokens). You are not being shown it. "
                "You will instead be given the passages retrieved for each specific "
                "module, below. Tier a module 'library' ONLY where such a passage "
                "actually supports it — never from memory of a book you have not "
                "been shown."
            ),
        })
        return messages
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_curriculum_grounding.py -q`
Expected: PASS, including the pre-existing cache-invariant assertion at
`test_curriculum_grounding.py:769`.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/curriculum/corpus.py apps/api/tests/test_curriculum_grounding.py
git commit -m "fix(curriculum): the oversized-library fallback now actually falls back

corpus.py documented this for two days. `fits` was computed and read by nobody;
above budget the whole library shipped AND retrieval was added on top, while the
UI banner reported a degrade that never happened. Unreachable at 82K — the four
new books land at 592,841 against a 600,000 budget."
```

---

### Task 8: Search the library before declining a named song

**Files:**
- Modify: `apps/api/app/agent/loop.py:601-607` and `:840-846`
- Modify: `apps/api/app/agent/guards.py:340`
- Test: `apps/api/tests/test_agent_guards.py`

**Interfaces:**
- Consumes: `looks_like_named_song_request` (unchanged), `retrieve.search` (unchanged).
- Produces: no new symbols; a reordering.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_agent_guards.py
def test_a_song_the_tutor_OWNS_is_answered_not_declined(db, monkeypatch):
    """The guard is a pre-model short-circuit at loop.py:601. The forced-retrieval
    pre-hop is at loop.py:608. So it declines BEFORE the library is searched —
    and a transcription the tutor OWNS, on a real page, with a real citation
    available, is refused unread.

    That is the actual complaint behind "those books are copywrited, but i bought
    them and they're mine". It is an ordering bug, not a policy.
    """
    from app.agent.loop import run_agent_turn

    hit = type("H", (), {"source_id": "s1", "page": 42, "text": "Intro riff: E5 G5 A5", "score": 0.9})()
    monkeypatch.setattr("app.agent.loop.search", lambda *a, **kw: [hit])
    monkeypatch.setattr("app.agent.loop.NAMED_SONG_DECLINE_MESSAGE", "DECLINED")

    result = run_agent_turn(db, [{"role": "user", "content": "give me the tab for Sweet Child O' Mine"}], locale="el")
    assert result.content != "DECLINED", "his own book was refused unread"
    assert result.citations, "an answer from his library must cite the page"


def test_a_song_the_tutor_does_NOT_own_is_still_declined(db, monkeypatch):
    """The guard's reason survives the reorder. The model cannot recall a specific
    recording's tab; removing the guard yields a confidently wrong one handed to a
    teacher, handed to a student — the same harm as an invalid citation."""
    from app.agent.loop import run_agent_turn

    monkeypatch.setattr("app.agent.loop.search", lambda *a, **kw: [])
    result = run_agent_turn(db, [{"role": "user", "content": "give me the tab for Sweet Child O' Mine"}], locale="el")
    assert "can't reproduce" in result.content or "δεν" in result.content


def test_the_decline_no_longer_cites_copyright():
    """For a private, single-user, non-commercial app over books the tutor owns,
    copyright is noise. Accuracy is the real and sufficient reason."""
    from app.agent.guards import NAMED_SONG_DECLINE_MESSAGE
    assert "copyright" not in NAMED_SONG_DECLINE_MESSAGE.lower()
    assert "memorized" in NAMED_SONG_DECLINE_MESSAGE
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_agent_guards.py -k owns -q`
Expected: FAIL — `AssertionError: his own book was refused unread`

- [ ] **Step 3: Reorder, and drop the copyright parenthetical**

In `loop.py`, move the `looks_like_named_song_request` block from *before* the C1
forced-retrieval pre-hop to *after* it, and gate it on there being no hits:

```python
    # The guard stays a PRE-MODEL short-circuit — guards.py:310-319 is right that
    # "there is no reliable way to make the model itself decline (it is the very
    # thing that fabricates when asked)". But it must not short-circuit RETRIEVAL
    # too. His library is searched first: if his own book has the transcription,
    # he gets it, cited. The decline is for what he does NOT own, where the model
    # would fabricate.
    if last.get("role") == "user" and looks_like_named_song_request(last.get("content") or "") and not hits:
        messages.append({"role": "assistant", "content": NAMED_SONG_DECLINE_MESSAGE})
        return AgentResult(status="answer", content=NAMED_SONG_DECLINE_MESSAGE, citations=[])
```

```python
# apps/api/app/agent/guards.py:340
NAMED_SONG_DECLINE_MESSAGE = (
    "I can't reproduce a specific recording's tab note-for-note — I don't "
    "actually have it memorized, and guessing would just invent a "
    "confidently wrong transcription instead of an honest answer. What I CAN "
    "generate for you: the chord progression in that style, a scale or "
    "technique exercise it draws on, or the riff's rhythmic shape as a generic "
    "pattern — just ask for one of those and I'll generate it as a real artifact."
)
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_agent_guards.py tests/test_agent_loop.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agent/loop.py apps/api/app/agent/guards.py apps/api/tests/test_agent_guards.py
git commit -m "fix(agent): search the library before declining a named-song request

The guard fired at loop.py:601; the library was searched at loop.py:608. So a
transcription the tutor OWNS was declined unread. The rule is anti-hallucination,
not copyright — its own message leads with 'I don't actually have it memorized' —
so it is reordered, not removed, and the copyright parenthetical is dropped as
noise for a private single-user app."
```

---

### Task 9: Re-OCR is explicit, resumable, and visible

**Files:**
- Modify: `apps/api/app/brain/ocr.py` (render at `ocr_render_dpi`, provenance, garbage screen)
- Modify: `apps/api/app/routers/knowledge.py` (`POST /sources/{id}/reocr`)
- Modify: `apps/web/src/components/library/source-row.tsx`
- Modify: `apps/web/messages/{el,en}.json`
- Test: `apps/api/tests/test_ocr.py`

**Interfaces:**
- Consumes: `get_ocr_provider()` (Task 6); `looks_like_ocr_garbage` (Task 1); `Page.text_source`/`ocr_reason` (Task 2).
- Produces: `POST /knowledge/sources/{id}/reocr -> {job_id}`; pages with `text_source` set.

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_ocr.py
def test_vision_pages_are_rendered_at_the_ocr_dpi_not_110(db, monkeypatch, a_pdf_source):
    """RENDER_DPI=110 is a Qwen ceiling ("image item with length 2080 exceeds
    pre-allocated encoder cache size 2048"), not a quality choice. Claude is
    high-res tier: 110dpi = 1,496 visual tokens, 150dpi = 2,714. Feeding it the
    stored 110dpi JPEG caps its fidelity on exactly the glyphs that matter."""
    seen = {}
    monkeypatch.setattr("app.config.settings.ocr_render_dpi", 150)
    monkeypatch.setattr("app.brain.ocr._render_for_vision",
                        lambda page, dpi: seen.setdefault("dpi", dpi) or b"\xff\xd8")
    from app.brain.ocr import ocr_source
    ocr_source(db, a_pdf_source.id)
    assert seen["dpi"] == 150


def test_ocr_records_who_wrote_the_text(db, monkeypatch, a_pdf_source):
    from app.brain.ocr import ocr_source
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider",
                        lambda: type("P", (), {"vision": lambda s, b, p, **k: "¼-inch"})())
    ocr_source(db, a_pdf_source.id)
    page = db.query(Page).filter_by(source_id=a_pdf_source.id).first()
    assert page.text_source == "claude"


def test_garbage_transcription_is_not_committed_as_ready(db, monkeypatch, a_pdf_source):
    """Kahn p.40 renders blank in BOTH MuPDF and poppler — the page is damaged in
    the PDF, so no model can recover it. What must NOT happen is 544 chars of
    "7 ipgges x a Rar ek en ee" entering the citation store as content."""
    from app.brain.ocr import ocr_source
    noise = "7 ipgges x \na \nRar \nek \nen ee \n& \neile \na= \n@ \nFilip \n«@ \n' \n= \nae \n¢ \n® a \n_ \n- \n¥ \n= \n, \n» \nve \né"
    monkeypatch.setattr("app.brain.ocr.get_ocr_provider",
                        lambda: type("P", (), {"vision": lambda s, b, p, **k: noise})())
    ocr_source(db, a_pdf_source.id)
    page = db.query(Page).filter_by(source_id=a_pdf_source.id).first()
    assert page.status == "failed"
    assert page.text_source == "failed"
    assert page.ocr_reason == "ocr_garbage"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && ./.venv/bin/python -m pytest tests/test_ocr.py -k "dpi or records or garbage" -q`
Expected: FAIL — `AttributeError: module 'app.brain.ocr' has no attribute '_render_for_vision'`

- [ ] **Step 3: Implement**

Add `_render_for_vision(page, dpi)` (re-rasterises from the source PDF at
`settings.ocr_render_dpi` rather than reading the stored 110dpi JPEG); swap
`get_provider()` for `get_ocr_provider()`; set `text_source` on success; screen
`looks_like_ocr_garbage` inside the existing retry loop beside
`_SUSPECTED_TRUNCATION_CHARS`, marking the page `failed` / `ocr_garbage` rather
than committing noise.

- [ ] **Step 4: Add the explicit re-OCR route**

`POST /knowledge/sources/{id}/reocr` enqueues a `GenerationJob(kind="ocr")` behind
the existing `SELECT…FOR UPDATE` in-flight guard, resetting `ocr_attempts` for
pages whose `ocr_reason == "inherited_ocr"`. Never called on upload: at ~40s per
page through `claude -p`, 748 pages is 8-12 hours and a repeated slice of a 5-hour
subscription cap. It is a button, and it resumes.

- [ ] **Step 5: Surface it in the Library**

`source-row.tsx` shows compile/OCR status and a "Ξαναδιάβασε με Claude" action,
following the existing `startOcr` pattern. Greek and English keys both.

- [ ] **Step 6: Run the tests**

Run: `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q`
Expected: PASS — the full suite (1,073 unit tests before this plan).

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/brain/ocr.py apps/api/app/routers/knowledge.py \
        apps/web/src/components/library/source-row.tsx apps/web/messages/ apps/api/tests/test_ocr.py
git commit -m "feat(ocr): explicit, resumable re-OCR at 150dpi with provenance

110dpi is a Qwen ceiling, not a quality choice. Garbage transcriptions are marked
failed instead of entering the citation store as content."
```

---

### Task 10: Frontend verification — a book goes in through the UI

Not a formality. Chris: *"make sure that you test one of the books via the FE, and
not merely from the backend, so that we know our frontend will work, and we can do
it ourselves from UI and not by telling you (claude) to do it."* The app is his to
operate. **Any step that needs a curl to succeed is a bug in the UI, not a shortcut.**

**Files:**
- Create: `apps/web/e2e/ingest-a-book.spec.ts`

**Interfaces:**
- Consumes: everything above, through the browser only.

- [ ] **Step 1: Write the spec**

Powers first — 57 pages, the smallest, and the only book that exercises the
`digital` path *and* the image-region path.

```typescript
// apps/web/e2e/ingest-a-book.spec.ts
import { expect, test } from "@playwright/test";

test("the tutor uploads a book and gets trustworthy pages", async ({ page }) => {
  test.setTimeout(20 * 60 * 1000);              // 57 pages through claude -p

  await page.goto("/el/library");
  await page.getByTestId("add-source").click();
  await page.getByTestId("source-file").setInputFiles(
    "../../books/Guitar Exercises Made Simple (Maxwell Powers).pdf");
  await page.getByTestId("source-submit").click();

  const row = page.getByTestId("source-row").filter({ hasText: "Guitar Exercises" });
  await expect(row).toBeVisible({ timeout: 120_000 });

  // Progress must be visible — the tutor is never left staring at nothing.
  await expect(row.getByTestId("ocr-progress")).toBeVisible();
  await expect(row.getByTestId("source-status")).toHaveText(/έτοιμο|ready/i,
    { timeout: 18 * 60 * 1000 });

  // The 40 tab pages must have gone to vision, not been marked ready with the
  // tabs unread. That is the whole point of this book being the fixture.
  await row.click();
  const claudePages = page.getByTestId("page-provenance").filter({ hasText: /claude/i });
  await expect.poll(() => claudePages.count(), { timeout: 60_000 }).toBeGreaterThan(30);
});
```

- [ ] **Step 2: Run it**

Run: `cd apps/web && npx playwright test e2e/ingest-a-book.spec.ts --headed`
Expected: PASS.

- [ ] **Step 3: Confirm by eye, in the Reader**

Automation proves it ran; only reading the page proves it is right.

- [ ] Open a tab page in the Reader. Its text must describe **the actual exercise**
      (the notes, the strings, the shape), not just the caption above it.
- [ ] Confirm `text_source` shows `claude` on tab pages, `text_layer` on prose pages.
- [ ] Open a `text_layer` page and confirm the text was NOT re-OCR'd — Powers is
      real digital text and re-reading it would be waste.

- [ ] **Step 4: Commit**

```bash
git add apps/web/e2e/ingest-a-book.spec.ts
git commit -m "test(e2e): a book goes in through the UI, tabs included

The app is the tutor's to operate. A green backend test is not done."
```

---

## Self-Review

**Spec coverage** (`2026-07-17-library-scaling-design.md` Part A):

| Spec | Task | |
|---|---|---|
| A0 inherited-OCR routing | 1, 3 | ✅ |
| A1 digital pages w/ image regions | 3 | ⚠️ **gap** — Task 3 routes `digital` pages wholesale to `ready`. Powers' 40 tab pages need a per-page image-region check *within* the digital path. Task 10 asserts >30 pages reach `claude`, so it **will fail** until this is closed. Add as Task 3b. |
| A2 provenance | 2 | ✅ |
| A2b bridge `/v1/vision` | 4, 5 | ✅ |
| A3 `OCR_PROVIDER` | 6 | ✅ |
| A4 150dpi re-render | 6, 9 | ✅ |
| A5 `fits` gate | 7 | ✅ |
| A6 async upload | — | **deferred**, deliberately: Tauri/localhost, no CDN timeout observed. Tracked in the spec. |
| A7 named-song ordering | 8 | ✅ |
| Figure descriptions in OCR_PROMPT | 9 | ⚠️ **gap** — the spec's fusion decision requires `OCR_PROMPT` to also describe figures/diagrams. Task 9 changes DPI and provenance but not the prompt. Fold into Task 9 Step 3, and note it is a **prompt change**, so it must be registered in the prompt-transparency spec's registry. |

**Placeholder scan:** none.

**Type consistency:** `text_layer_kind` returns `"none"|"ocr"|"digital"` in Tasks 1/3 ✅. `text_source` values `text_layer|qwen|claude|failed` consistent across Tasks 2/9/10 ✅. `get_ocr_provider()` named identically in Tasks 6/9 ✅. `_resolve_media_path`/`_vision_argv` in Task 4 match their Task 5 caller ✅.

**Two gaps found and recorded rather than papered over.** Close both before Task 10, which is the test that catches them.
