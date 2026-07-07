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
| 3 | `curriculum-segmentation.md` | pending | Block tree · generate · auto-structure-from-doc · segmentation · card board |
| 4 | `artifact-engine.md` | pending | spec → validate → SVG (chords/tab/staff + signal-chain/amp/pedalboard/recipe) |
| 5 | `agent-tools-hitl.md` | pending | LangGraph loop · tools · streaming · HITL interrupt/approve · chat UI |
| 6 | `cockpit-integration.md` | pending | Today/Students/Curricula/Knowledge/Notes wired · Prep path · glanceability |
| 7 | `seed-content.md` | pending | Zero-to-Hero + Guitar Tone curricula · iconic-tone recipes · ingest book+links |
| 8 | `deploy.md` | pending | host nginx vhosts · DNS · certs · public compose · smoke on `guitar.cgrigoriadis.online` |
