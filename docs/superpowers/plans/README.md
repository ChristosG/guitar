# Guitar Tutor Copilot — Implementation Plans

- **Spec:** [`../specs/2026-07-06-guitar-tutor-copilot-design.md`](../specs/2026-07-06-guitar-tutor-copilot-design.md)
- **Build order & rationale:** spec §9.

The system is decomposed into **per-subsystem plans**. Each produces working, testable
software on its own and is executed + verified **before the next plan is written**, so
later plans can reference the real interfaces earlier plans produced (not guesses).

| # | Plan | Status | Delivers |
|---|------|--------|----------|
| 1 | `2026-07-06-foundations.md` | ✅ **DONE** | monorepo · Docker Compose · Postgres+pgvector · FastAPI skeleton · `LLMProvider`(Qwen) · core DB models+migrations · themed bilingual Next.js shell |
| 2 | `2026-07-07-knowledge-brain.md` | ✅ **DONE** | ingest (pdf/url/text) → chunk → embed → pgvector · retrieval · grounded bilingual /ask · Knowledge UI · SSRF-guarded |
| 3 | `2026-07-08-curriculum-segmentation.md` | ✅ **DONE** | guided-JSON generate (Brain-grounded) · deterministic segmentation · deep-clone assign · **cockpit shell** + Students + Curricula card board |
| 4 | `2026-07-08-artifact-engine.md` | ✅ **DONE** | spec → validate → SVG: chord diagrams · tab/staff (AlphaTab) · tone-recipe/signal-chain/amp-dial cards · LLM spec-gen · Artifacts gallery + attach-to-segment |
| 5 | `2026-07-10-agent-tools-hitl.md` | 🔄 **T1–T6 DONE, e2e-verified** (go-live T7 pending) | hand-rolled ReAct loop over `LLMProvider.chat_tools` (native Qwen tool-calling, NOT LangGraph/SSE — see plan's own "reverses stale spec decisions" note) · ~13-tool registry (6 reads + 7 mutations) · Postgres HITL (`ChatSession`/`Message`/`ApprovalRequest`, approve-before-execute) · chat router (messages/resolve/pending) + async-generate compose (reuses Plan 8's job/poll) · chat UI + approval card — verified live: real model tool-calls correctly at the full roster (read intent answers without hallucinating a mutation; mutation intent proposes the right tool + suspends) and a real browser e2e (grounded Q&A → cited-quality answer; curriculum-gen approval → job → tree; chord-diagram approval → artifact); remaining: T7 go-live (shared with Plan 8) |
| 6 | `cockpit-integration.md` | pending | Today/Students/Curricula/Knowledge/Notes wired · Prep path · glanceability |
| 7 | `seed-content.md` | pending | Zero-to-Hero + Guitar Tone curricula · iconic-tone recipes · ingest book+links |
| 8 | `2026-07-09-async-generation-and-deploy.md` | 🔄 **T1–T5 DONE, e2e-verified** (deploy T6–T7 pending) | async `generation_job` + `BackgroundTasks` runner · `POST /curricula/generate`→202 · `GET /jobs/{id}` poll · FE poll+render — verified live (real LLM, UI+curl); remaining: nginx orange-cloud vhosts · DNS · certs · live verify on `guitar.cgrigoriadis.online` |
