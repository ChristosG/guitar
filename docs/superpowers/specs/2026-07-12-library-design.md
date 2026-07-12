# The Library — Design (Plan 9)

**Status:** approved by Chris, 2026-07-12
**Sub-project A of a 3-part redesign.** A (Library) → B (Lesson Authoring) → C (Copilot rebuild).

---

## 1. Why this exists

The current cockpit is broad but shallow: it has pages for curricula, artifacts, students,
notes, and chat, and none of them serve the tutor's actual job. His job — his words — is to
**organize and author his lessons from HIS content**. Everything else is a bonus.

Today he cannot do that, because **his content is not in the app.**

Live state of the knowledge base at the time of writing:

```
ready       16141 chars  text   Guitar Tone & Gear — Course Spine   <- the ONLY real content
ready           0 chars  url    Humbucker (Wikipedia)               <- "ready". zero characters.
ready           0 chars  url    Distortion (music) (Wikipedia)      <- "ready". zero characters.
ready           0 chars  url    Guitar amplifier (Wikipedia)        <- "ready". zero characters.
ready         337 chars  text   Tone seed
ready         125 chars  text   Barre chord tip: SRV bite
ready          68 chars  text   Review-fix smoke
```

Three sources are marked `ready` with **zero characters** and render as healthy in the UI.
The tutor's book — `Getting Great Guitar Sounds.pdf`, 77 pages — is a raster scan with no
text layer and is not ingested at all. So the "RAG" agent has been retrieving against ~16k
characters of one hand-written text file.

**This is the root cause of the agent bluffing.** When asked for a G major scale tab, the model
typed a (wrong) ASCII tab into a code fence rather than calling `generate_artifact` — because
there was nothing in the library to ground against. Fixing the agent (sub-project C) before
fixing the library would just produce a well-engineered agent retrieving from an empty shelf.

**So: the Library is built first, and it is the home screen.**

## 2. Scope

**In scope**
- OCR the scanned book so its content is genuinely searchable and citable.
- A real reader: the scanned page next to its selectable text.
- Collections (one-level folders) so he can organize.
- Honest ingest health — a broken source is loud and fixable, never a green lie.
- The capture endpoint for "author a lesson from this selection" (the seam into sub-project B).

**Explicitly out of scope**
- The agent/chat. Forced retrieval, markdown rendering, streaming, and artifact discipline are
  **sub-project C**. Not touched here.
- The Lesson editor UI. **Sub-project B**. This project builds the *seam* to it and stubs the
  destination, so the wire is real and tested — but no lesson-authoring UI ships here.
- Today/Prep page. Still deferred.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Page-native model.** New `Page` table; `Chunk` gets a real `page_id` FK. | Makes a citation *verifiable* — a chunk knows its page, a page has its scan image, so "p.47" can always be **shown**, not merely claimed. Also enables per-page ingest health and isolated re-embedding. |
| D2 | **Non-paginated sources get exactly one `Page` row** (`image_path = NULL`). | Forces URL/text/note sources through the same shape as the book. Pays one cheap redundant row to delete a `source.type` branch from the reader, the chunker, the citation renderer, and the retry logic. |
| D3 | **OCR backend is swappable behind a new `LLMProvider.vision()` method.** Ships with **Qwen-VL (local, free)**. | The provider seam already abstracts `chat/embed/guided_json/chat_tools`; vision is the one genuine gap. Chris's call: Qwen-VL is the default and what we will use during the build. **Ship-time heads-up: we will probably swap to Claude** (better on scanned diagrams and amp panels; ~$1 one-time for the 77-page book, and its native PDF support returns real `page_location` citations). That must remain a config change, never a rewrite. |
| D4 | **OCR runs as `GenerationJob(kind="ocr")`** on the existing async job + poll infrastructure. | 77 vision calls is a background job. Reuses Plan 8 wholesale — no queue, no worker, no new infra. UI shows "OCR'ing page 34 of 77…". |
| D5 | **Per-page commit + automatic retry. No manual OCR editing.** | A failure on page 60 must not lose pages 1–59. Chris was explicit: *"why should he care with stuff like this — he will just inject his pdf and it has to be there."* The machine retries; the tutor is only ever told about a page we truly cannot read. There is **no "Fix OCR" text editor** in the reader. |
| D6 | **`char_count == 0` may never be `status="ready"`.** | This single lie hid three dead sources behind a green checkmark for two days. Zero extractable text ⇒ `status="empty"`, shown in red, with a retry. Pinned by a regression test. |
| D7 | **Collections are one-level folders**, a source lives in exactly one, default `Unfiled`. | A filing cabinet is the right mental model for a non-technical tutor. Tags were rejected: "which tags do I use?" is exactly the kind of open-ended box that makes the app feel like a chore. |

## 4. Data model

```
Page                                   (new)
  id, source_id -> KnowledgeSource (CASCADE)
  page_no     int            # 1-based, as printed
  image_path  str | None     # rendered scan on a volume; NULL for text/url sources
  text        str | None     # OCR'd or extracted text
  status      str            # pending | ocr_running | ready | failed | empty
  ocr_error   str | None

Chunk                                  (changed)
  + page_id  -> Page                   # replaces the unpopulated `page: int` guess

Collection                             (new)
  id, name

KnowledgeSource                        (changed)
  + collection_id -> Collection (SET NULL)   # NULL == "Unfiled"
```

`Chunk.page` (int, never properly populated) is dropped in favour of the FK.

## 5. Pipeline

`app/brain/ocr.py` (new):

1. **Render** — PyMuPDF rasterizes each page at 110dpi → JPEG (~500KB/page, measured on the
   real book) → written to a mounted volume. A `Page` row is created per page, `status=pending`.
2. **Transcribe** — each page image → `provider.vision()` → page text. One page at a time, each
   **committing independently** (D5). A failed page is retried automatically; on final failure it
   is marked `failed` and the job continues.
3. **Chunk + embed** — once a page is `ready`, the existing `chunk.py` → `ingest.py` path embeds
   its text, with `Chunk.page_id` set. **`chunk.py` and `retrieve.py` are otherwise untouched** —
   this bolts onto the tested pipeline rather than replacing it.

Also fixed here, because they are the same bug class:
- `ingest.py`'s status logic (D6).
- The Wikimedia **403** on URL ingest — a User-Agent block — so those three dead sources
  actually ingest.

## 6. UI

**The Library replaces the Knowledge page** and becomes the home screen.

```
┌─ LIBRARY ───────────────────────────── [+ Add source] ─┐
│ 📁 Tone & Gear                                          │
│    📕 Getting Great Guitar Sounds     77 pp   ✅ ready  │
│    🌐 Humbucker (Wikipedia)            —      ⚠️ empty  │
│                                          [Retry ingest] │
│ 📁 Beginner Method                                      │
│    📄 Course Spine                   16,141 ch ✅ ready │
│ 📁 Unfiled                                              │
└─────────────────────────────────────────────────────────┘
```

Every source shows honest state: `ready` / `empty` / `failed` / `ocr_running (34/77)`.

**The Reader** — the screen that matters:

```
┌─ Getting Great Guitar Sounds · p.47 ────────────────────┐
│ ┌──────────────┐ │ The Tube Screamer is not really a    │
│ │  [scanned    │ │ distortion box so much as a mid-     │
│ │   page 47]   │ │ range hump with clipping...          │
│ │              │ │ ███████████████████ ← selection      │
│ └──────────────┘ │                                      │
│   ◀ 46   48 ▶    │ ✨ Author a lesson from this          │
└─────────────────────────────────────────────────────────┘
```

Left: the real scan. Right: the selectable OCR text. The image half is **not decoration** — for a
gear book full of amp photos and knob positions it is half the content, and seeing it next to the
text is how the tutor knows the transcription is faithful.

**✨ Author a lesson from this** packages `{text, source_id, page_no}` and hands it to sub-project
B. Here it hits a real, tested endpoint with a stubbed destination.

## 7. API

All additive; no existing route changes shape.

| Route | Purpose |
|---|---|
| `GET/POST/PATCH/DELETE /library/collections` | Folders |
| `PATCH /knowledge/sources/{id}` | Move to collection, rename |
| `POST /knowledge/sources/{id}/ocr` | Start OCR → `202 {job_id}` |
| `GET /knowledge/sources/{id}/pages` | Reader: page list + statuses |
| `GET /knowledge/sources/{id}/pages/{n}` | One page: text + image URL |
| `POST /knowledge/sources/{id}/retry` | Re-run a failed/empty ingest |
| `GET /media/pages/{id}.jpg` | Serve a page image off the volume |
| `POST /lessons/from-selection` | Capture a selection (seam to sub-project B; stub destination) |

## 8. Error handling

Every page is independent. A `vision()` timeout on page 60 marks that page failed, retries it,
and keeps going — 59 good pages are never lost to one bad one. The job records per-page failures
and the UI reports `71 of 77 pages read · 6 failed [Retry failed pages]`. A source is `ready`
only when at least one page carries real text (D6).

## 9. Testing

- **TDD throughout.** `vision()` is faked in unit tests exactly as `chat`/`embed` already are, so
  the whole pipeline is testable without touching a GPU.
- **Regression test pinning D6:** a zero-character ingest must never come out `ready`.
- **Real verification, not just green tests:** OCR the actual 77-page book against live Qwen-VL and
  read the output to confirm it is genuinely faithful.

**Acceptance test — the thing that has never worked:**

> Ask a question that can *only* be answered from the book. Get back a grounded answer citing a
> real page — with the scan of that page one click away.

If that does not work, this project is not done.
