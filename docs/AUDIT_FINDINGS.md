# Audit findings — full verified list (2026-07-16)

74 findings survived two-round adversarial verification (multi-agent
finders + independent verifiers reading the actual code).
Status: FIXED tonight, or OPEN (the notable ones are argued in
`CORE_DECISIONS.md`).


## P1

- OPEN — `apps/api/app/jobs/curriculum_draft.py:100` — No in-flight guard on draft Resume/Deepen + non-atomic lesson claim: concurrent fan-outs double-bill the same lessons
- **FIXED** — `apps/api/app/jobs/curriculum_draft.py:84` — Resume never retries failed lessons — the exact action the job error, the docstring, and the UI all tell the tutor to take
- OPEN — `apps/api/app/jobs/runner.py:159` — Job runners record Claude LLMError kinds (429 / invalid key / timeout / 529) as error_kind='internal' — 'our bug, try again'
- OPEN — `apps/api/app/agent/loop.py:640` — Chat REST turn: LLM failure surfaces as raw 500 and the whole multi-call billed turn is discarded
- **FIXED** — `apps/api/app/brain/ocr.py:207` — OCR burns the permanent per-page attempt budget on auth/rate-limit failures and never aborts the run
- OPEN — `apps/api/app/jobs/curriculum_draft.py:295` — Draft fan-out coerces empty course source_ids to whole library (`source_ids or None`)
- **FIXED** — `apps/api/app/agent/guards.py:315` — Named-song guard over-declines: ordinary theory/practice questions and legitimate generic artifact requests are refused before the model is ever called
- **FIXED** — `apps/api/app/routers/chat.py:434` — Chat context assembly is unbounded: every turn resends the entire session history, eventually overflowing the context window and permanently bricking the session
- **FIXED** — `apps/api/app/brain/retrieve.py:246` — Query-translation circuit breaker latches permanently on one transient failure, collapsing Greek retrieval quality for the rest of the process lifetime
- **FIXED** — `apps/api/app/jobs/runner.py:144` — Job runners never catch LLMError — under the Claude provider every 429/auth/timeout is recorded as error_kind="internal"
- **FIXED** — `apps/api/app/jobs/runner.py:387` — run_lesson_job records Claude auth/rate-limit/timeout failures as error_kind='internal'
- **FIXED** — `apps/web/src/components/chat/chat-panel.tsx:281` — Chat stream network error auto-resends the message via REST — double LLM billing and possible duplicate turn
- **FIXED** — `apps/api/Dockerfile:41` — No automatic `alembic upgrade head` — every app update boots against a stale schema until someone runs migrations by hand
- **FIXED** — `apps/web/src/components/chat/chat-panel.tsx:270` — Stream 'done' with zero deltas leaves a permanent empty assistant bubble
- **FIXED** — `apps/web/src/components/curriculum/interview-dialog.tsx:315` — Regenerated outline is not shown: outline editor key collides, tutor pays for a new outline and sees the old one
- OPEN — `apps/api/app/agent/loop.py:880` — Streaming endpoint double-bills every tool-calling turn: the streamed model call is discarded on 'fallback' and the REST retry re-runs it
- **FIXED** — `apps/api/app/jobs/curriculum_draft.py:88` — Lesson claim in the draft fan-out is check-then-set with no lock, and Resume/deepen enqueue has no in-flight guard — double Claude billing
- **FIXED** — `apps/web/src/components/curriculum/interview-dialog.tsx:83` — Outline-job poll timeout surfaces as an invisible empty error and desyncs the dialog from the server's step

## P2

- OPEN — `apps/api/app/routers/chat.py:501` — SSE chat stream persists nothing on client disconnect — a fully-billed generation is silently discarded
- OPEN — `tools/claude_bridge/bridge.py:199` — Bridge classifies Anthropic 'overloaded'/529 as 'upstream', so transient overload kills lessons dead instead of requeueing
- OPEN — `apps/api/app/routers/curriculum.py:226` — Interview outline regeneration has no in-flight guard — duplicate full-library calls
- OPEN — `apps/api/app/routers/students.py:70` — POST /students silently drops the `goals` field on create
- OPEN — `apps/api/app/lessons/draft.py:169` — LLM-emitted titles are persisted unclamped into Block.title varchar(300) — a long title fails the job AFTER the paid model call
- OPEN — `apps/api/app/schemas/curriculum.py:66` — Multiple write schemas have no max_length while their columns are bounded varchars — tutor-typed input over the limit becomes a raw 500 (DataError) instead of a 422
- OPEN — `apps/api/app/routers/health.py:36` — GET /health/ready returns 409 instead of its documented {db,llm,embed} body when the Anthropic key is not configured
- OPEN — `apps/api/app/curriculum/generate.py:121` — Explicit 'no sources' selection ([]) silently coerced to 'whole library' in generate_curriculum
- OPEN — `apps/api/app/curriculum/draft.py:140` — Oversized-library fallback still embeds the full library in every prompt — breaks entirely past the model window
- OPEN — `apps/api/app/curriculum/interview.py:458` — Outline step has no interlock with its background job: duplicate paid regenerations, and a late job overwrites the tutor's accepted outline
- OPEN — `apps/api/app/curriculum/interview.py:438` — Accepted edited outline is barely validated — confirm can 500 irrecoverably, stranding the interview
- **FIXED** — `apps/api/app/curriculum/outline.py:279` — English prose persisted into a Greek tutor's curriculum: GAP_BODY and enforce_shape placeholders
- **FIXED** — `apps/api/app/brain/retrieve.py:493` — explain_concept / answer() with zero retrieval hits instructs the model to answer 'strictly from the provided context' with an EMPTY context — a guaranteed refusal instead of a labelled general-knowledge answer
- OPEN — `apps/api/app/agent/loop.py:247` — Entity-noun exclusion in _is_content_bearing suppresses forced grounding for genuine library questions mentioning scale/tab/diagram/lesson nouns — in both languages
- OPEN — `apps/api/app/agent/loop.py:656` — Internal corrective prompts are persisted as role='user' and displayed in chat history as if the tutor typed them
- OPEN — `apps/api/app/agent/loop.py:122` — User-facing fallback/decline strings are hardcoded English and ignore the session locale
- **FIXED** — `apps/api/app/lessons/draft.py:253` — A schema-valid but empty lesson ({title, sessions: []}) is persisted as a successful job — paid call, nothing to teach
- **FIXED** — `apps/api/app/curriculum/assign.py:59` — clone_content_subtree drops Block.meta — assigned student copies lose citations, tiers, sections, and draft_status
- OPEN — `apps/api/app/routers/artifacts.py:134` — Artifact generation under the Claude provider surfaces 429/timeout/auth as an unhandled 500
- **FIXED** — `apps/api/app/brain/retrieve.py:512` — answer() pays a chat call even when zero hits survive the floor — a guaranteed 'not in your library' answer billed to the tutor
- **FIXED** — `apps/web/src/components/artifacts/kinds.ts:24` — gear_card is offered in both generate pickers but has no renderer — a paid LLM call that can only ever display 'unsupported kind'
- **FIXED** — `apps/api/app/settings_store.py:103` — app_setting singleton creation race: concurrent first reads raise unhandled IntegrityError (500)
- **FIXED** — `apps/api/app/brain/ocr.py:452` — OCR treats rate-limit errors as page failures and burns the permanent 3-attempt budget — pages can become forever-unreadable
- **FIXED** — `docker-compose.yml:117` — No backup story at all — one `docker compose down -v` or disk fault erases every curriculum, chat, note, and paid-for OCR text
- **FIXED** — `apps/api/app/brain/repair.py:198` — Opening a legacy pageless URL source in the Reader deletes its chunks before knowing the re-fetch succeeds — permanent content loss on a GET
- **FIXED** — `apps/web/src/components/lessons/session-card.tsx:82` — Stale draftMinutes after 'merge down' writes the pre-merge value back over the summed est_minutes
- OPEN — `apps/web/src/components/library/selection-action.tsx:109` — Cross-page selection sends page-marker chrome ('ΣΕΛΙΔΑ N') embedded inside the passage used to draft the lesson
- **FIXED** — `apps/api/app/agent/guards.py:288` — Named-song guard clause splitter is English-only: Greek compound requests falsely declined without ever calling the model
- OPEN — `apps/api/app/routers/chat.py:424` — No concurrency guard on post_message: a double-send races past the pending-approval check and can interleave two agent turns
- **FIXED** — `apps/web/src/components/lessons/session-card.tsx:88` — Clearing a session's minutes in the lesson editor silently no-ops and the old value snaps back
- **FIXED** — `apps/web/src/components/curriculum/draft-progress-bar.tsx:66` — One failed tree refetch after the last lesson drafts leaves the board permanently stale under a 'complete' progress bar
- **FIXED** — `apps/api/app/main.py:68` — Embedding model (ONNX) is never warmed at boot — a missing/corrupt model file surfaces as runtime 500s, contrary to the documented startup check
- **FIXED** — `apps/web/src/components/library/selection-action.tsx:144` — Lesson-draft poll keeps running after leaving the Reader and yanks the tutor to the lesson page minutes later
- **FIXED** — `apps/api/app/jobs/runner.py:115` — GenerationJob error messages are hardcoded English and rendered verbatim in the Greek UI; error_kind exists but is never used

## P3

- OPEN — `apps/api/app/jobs/curriculum_draft.py:343` — Fan-out job finalizes 'succeeded' with no error when every lesson was rate-limit-requeued
- OPEN — `apps/api/app/routers/jobs.py:25` — GenerationJob rows (including full selection text in params) are never deleted
- OPEN — `apps/api/app/routers/curriculum.py:550` — POST /blocks/{id}/segment with an unknown student_id hits the FK as a raw 500 instead of a 404
- OPEN — `apps/api/app/curriculum/corpus.py:230` — _count_tokens fallback estimate (len//3) is optimistic for Greek, not pessimistic
- OPEN — `apps/web/src/app/[locale]/(cockpit)/lessons/[lessonId]/page.tsx:56` — Concurrent rename/delete refreshes can apply an older lesson tree last (stale UI until next action)
- OPEN — `apps/api/app/config.py:87` — Session cookie expires hard at 14 days with no rolling renewal — periodic surprise logout, in-flight UI state lost
- OPEN — `apps/api/app/routers/library.py:298` — No in-flight guard for url reingest retries — double-click/two tabs enqueue racing reingest jobs on the same source
- OPEN — `apps/web/src/components/library/library-search.tsx:32` — Library-search snippet centering never matches Greek queries (accents + server-side translation), hiding the match evidence
- **FIXED** — `apps/api/app/routers/chat.py:283` — Orphaned empty chat_session rows and stale curriculum_interview rows accumulate forever with no GC
- **FIXED** — `apps/api/app/agent/transcript.py:108` — Citations are silently dropped when a turn suspends on a mutation preceded by read tool-calls
- **FIXED** — `apps/api/app/models/generation_job.py:41` — generation_job table grows forever with no retention and no status index
- OPEN — `apps/api/app/brain/chunk.py:14` — Chunker's sentence-boundary detection is English-only: Greek question mark ';' and ano teleia '·' are not sentence enders
- OPEN — `apps/web/src/app/[locale]/(cockpit)/library/[sourceId]/page.tsx:143` — Reader deep-link (?page=N) scroll drifts off-target: single 400ms retry expires before rows above the target finish loading
- **FIXED** — `apps/web/src/components/lessons/provenance-chip.tsx:27` — ProvenanceChip caches a rejected title fetch forever — one transient error blanks that book's title until full reload
- OPEN — `apps/web/src/lib/api.ts:1332` — Chat streaming has no AbortController: navigating away mid-stream leaks the connection, and job polls run 5 minutes after unmount
- OPEN — `apps/web/src/components/notes/note-form.tsx:74` — Note edit form offers 'no student' but unlinking silently does nothing
- **FIXED** — `apps/web/src/components/students/lesson-log-list.tsx:43` — Lesson log dates render as raw ISO strings, not Greek-formatted dates
- **FIXED** — `apps/web/src/app/[locale]/(cockpit)/chat/page.tsx:46` — Every visit to /chat before the first-ever message creates another orphan session row
- OPEN — `apps/api/app/curriculum/draft.py:430` — student.preferred_language is unvalidated free text and flows un-normalized into lesson language; SECTION_LABELS falls back to English
- OPEN — `apps/api/app/curriculum/interview.py:286` — Interview validation (_reask) error strings are English and rendered verbatim in the Greek interview dialog
- OPEN — `apps/web/src/components/library/reader-page-row.tsx:39` — Cross-page selection embeds the Reader's page-marker label text into the passage sent to the LLM
- **FIXED** — `apps/web/src/app/[locale]/(cockpit)/library/page.tsx:258` — Transient refresh failure during OCR hides the entire library behind the error line
