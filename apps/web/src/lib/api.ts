/**
 * Typed fetch helpers for the Knowledge Brain API (`/knowledge/*`).
 *
 * The browser calls the API directly — it does not go through a Next.js
 * proxy/route handler (see `app/main.py`'s CORS middleware, which exists
 * specifically because the browser is the caller).
 *
 * THE API BASE IS RESOLVED IN THE BROWSER, NOT BAKED AT BUILD TIME.
 *
 * It used to be `process.env.NEXT_PUBLIC_API_BASE`, which Next.js inlines into
 * the client bundle at BUILD time. That is a single constant — and there is only
 * ONE `web` container, serving BOTH `localhost:8790` and (via nginx)
 * `guitar.cgrigoriadis.online`. So whichever value was baked, the other
 * environment broke, and the two failures were hours apart and looked unrelated:
 *
 *   baked `http://localhost:8791` -> the DEPLOYED site tells every visitor's
 *     browser to call the visitor's OWN localhost. Nothing errors server-side;
 *     the API log is empty because nothing arrives. The user sees "Couldn't
 *     reach the server."
 *   baked `https://guitar-api.…`  -> LOCALHOST now calls the public API
 *     cross-SITE (different eTLD+1), so the SameSite=Lax session cookie is not
 *     sent on XHR at all. Login succeeds, the app flashes onto /today, and the
 *     next request is unauthenticated.
 *
 * A build-time constant cannot describe a runtime fact. So we read the fact:
 * the origin the page was actually served from. Localhost talks to localhost;
 * the deployed host talks to its own API subdomain. One image, both
 * environments, and no build arg to forget.
 *
 * `NEXT_PUBLIC_API_BASE` still wins if set — it is the escape hatch for a
 * deployment whose API is not at `guitar-api.<domain>`.
 *
 * AND THE PORT IS A RUNTIME FACT TOO — that is what the DESKTOP build added.
 *
 * The Tauri shell ships this same bundle, but it cannot ship the same PORTS:
 * :8790/:8791 belong to whoever grabbed them first on the tutor's laptop, so
 * the shell picks two FREE ports at launch and only then knows where its own
 * API child is listening. The origin rule below would answer `:8791` with
 * total confidence — a port with nothing behind it, or worse, someone else's
 * server. Same lesson as above, one step further: don't guess a fact, read it.
 * The shell states it as `window.__GT_API_BASE__ = "http://localhost:<port>"`
 * via `initialization_script`, which runs BEFORE any page script — which is
 * also why reading it once into the module-level `API_BASE` below is correct
 * rather than lucky.
 *
 * Three rules, highest precedence first:
 *   1. `window.__GT_API_BASE__` — desktop only; the shell is the only thing
 *      that can know which ports it won. ONLY HONOURED ON A LOCAL ORIGIN
 *      (see `isLoopbackHost`).
 *   2. `NEXT_PUBLIC_API_BASE`   — the escape hatch above, baked into the client
 *      bundle at build time (on the SERVER the same read is a runtime one —
 *      `envApiBase` explains where that difference bites).
 *   3. the page's own origin    — localhost -> :8791, `guitar.X` ->
 *      `guitar-api.X`. Unchanged; this is still what the webapp uses.
 *
 * A malformed injection (absent, empty, not a string) falls THROUGH to 2/3
 * instead of being trusted: degrading to today's behaviour produces a
 * comprehensible bug report, whereas `undefined/knowledge/sources` produces a
 * mystery.
 *
 * NOTHING HERE MAY BE RENDERED INTO SERVER HTML. All three rules are runtime
 * facts, and two of them need a `window` the server does not have — so a server
 * render can only ever produce rule 2's value or the `:8791` default. React does
 * NOT repair a mismatched attribute during hydration (it says so itself: "some
 * attributes of the server rendered HTML didn't match the client properties.
 * This won't be patched up."), so an `API_BASE`-derived URL baked into JSX keeps
 * the SERVER's guess forever. That is not hypothetical: `backup-card.tsx`'s
 * export anchor did exactly this, which pointed the deployed site's only backup
 * button at each VISITOR's own `http://localhost:8791` and the desktop app's at
 * a port it does not own. See `serverBackupExportUrl` for the shape a rendered
 * URL has to take instead.
 */

/** Trailing slashes off, because every caller builds `${API_BASE}${path}` and
 * `//knowledge/sources` is a different, 404-ing path to FastAPI. Applied to
 * rule 1 AND rule 2 — a hand-typed `NEXT_PUBLIC_API_BASE=https://host/` is at
 * least as likely to carry one as the shell's injection is. */
function normalizeBase(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

/** Rule 2 alone, normalized; `null` when unset or blank so callers can fall
 * through to rule 3.
 *
 * The two halves of the app read this from two different places, which matters
 * exactly once (see `serverBackupExportUrl`). In the BROWSER there is no
 * `process.env` to read at all — Next bakes the value into the client bundle at
 * BUILD time, which is why changing it there means a rebuild. On the SERVER the
 * same expression stays a live `process.env` lookup, answered per request out of
 * the node process's own environment: a production `next start` whose env
 * exports `NEXT_PUBLIC_API_BASE`, on a bundle built WITHOUT it, renders that
 * runtime value into the SSR HTML.
 *
 * They agree in every shape this repo ships, but by arrangement rather than by
 * language rule: `docker-compose.yml` feeds both halves from the same root
 * `.env` (build ARG for the client bundle, `env_file` for the server), and
 * `desktop/scripts/stage-web.sh` unsets it deliberately so neither half has one.
 * Editing `.env` and restarting WITHOUT rebuilding `web` is what pulls them
 * apart — the same caveat docker-compose.yml already spells out. */
function envApiBase(): string | null {
  const raw = process.env.NEXT_PUBLIC_API_BASE;
  if (typeof raw !== "string") return null;
  return normalizeBase(raw) || null;
}

/** Is this page served from the tutor's own machine? The desktop shell always
 * loads `http://localhost:<web port>` — literal `localhost`, which
 * `desktop/src-tauri/src/main.rs` calls out as load-bearing for the session
 * cookie — so this gate never costs the desktop anything.
 *
 * It is the same test rule 3 uses to pick `:8791`, deliberately: one definition
 * of "local", so the two rules can never disagree about where they are. */
function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1";
}

/** The base a SERVER render can know: no `window`, so rules 1 and 3 are both
 * unavailable and only the `NEXT_PUBLIC_API_BASE` escape hatch — read from the
 * server's own environment at request time, see `envApiBase` — plus the dev
 * default remains. Used by the SSR branch below AND by `serverBackupExportUrl`,
 * so the two are the same string BY CONSTRUCTION rather than by a comment asking
 * someone to keep them in sync. */
function serverApiBase(): string {
  return envApiBase() ?? "http://localhost:8791";
}

function resolveApiBase(): string {
  // SSR/build: there is no `window`. next-intl renders these pages on the
  // server, so this branch is real, not defensive.
  if (typeof window === "undefined") {
    return serverApiBase();
  }

  // Rule 1 — the desktop shell's runtime truth, GATED ON A LOCAL ORIGIN.
  //
  // The gate is the security half of this rule. One bundle ships to both the
  // Tauri shell and `guitar.cgrigoriadis.online`, and `API_BASE` is where every
  // credentialed request goes — including the login POST. Ungated, any script
  // that ran for a moment on the public site (an injected tag, a bad dependency,
  // a stored-XSS sink) could set one global before this module evaluates and
  // silently redirect the tutor's password and session to its own host, with no
  // navigation and nothing visible to see. A local origin is the only place the
  // global can legitimately come from — the shell is the thing that serves the
  // page there — so honouring it anywhere else buys nothing and costs that.
  const injected = window.__GT_API_BASE__;
  if (typeof injected === "string" && isLoopbackHost(window.location.hostname)) {
    const base = normalizeBase(injected);
    if (base !== "") return base;
  }

  // Rule 2 — the build-time escape hatch.
  const fromEnv = envApiBase();
  if (fromEnv) return fromEnv;

  // Rule 3 — the page's own origin.
  const { protocol, hostname } = window.location;
  if (isLoopbackHost(hostname)) {
    // Cookies ignore the PORT, so a host-only cookie set by the API on :8791 is
    // sent to the app on :8790. Same site, no CORS credential problem.
    return `${protocol}//${hostname}:8791`;
  }
  // `guitar.cgrigoriadis.online` -> `guitar-api.cgrigoriadis.online`. Same
  // registrable site, so the `.cgrigoriadis.online` cookie covers both.
  return `${protocol}//guitar-api.${hostname.replace(/^guitar\./, "")}`;
}

const API_BASE = resolveApiBase();

export type SourceKind = "text" | "url";
/** Mirrors `app.models.knowledge.SOURCE_STATUSES` (`ingest.py`'s module
 * docstring + `ocr.py`'s `_rollup_source_status`): "ingesting" (transient,
 * committed before the pipeline runs) -> "ready" (readable text, no failed
 * pages) | "partial" (readable text, SOME pages unreadable) | "empty" (the
 * pipeline succeeded but extracted nothing — SPEC D6: this is NOT "ready") |
 * "failed" (the pipeline raised).
 *
 * "partial" is Stage 7.2 and it is the honest one: a 74-of-77-page book used to
 * roll up to a green "Ready" with no retry path, forever. A partial source is
 * fully readable and fully citable — treat it as USABLE everywhere (link into
 * the Reader, cite it, search it); it just also offers "retry the failed pages".
 *
 * There is still no "ocr_running" here — that only ever exists on `Page.status`.
 * Whether a book is being read RIGHT NOW is `SourceOut.ocr_active`, which is a
 * server fact (an in-flight `GenerationJob`), not a React state. */
export type SourceStatus = "ingesting" | "ready" | "partial" | "empty" | "failed";

/** One book's concept-canon compile state (Part B, C7) — mirrors
 * `app.schemas.knowledge.CompileStatusOut`. `SourceOut.compile` is `null`/absent
 * when the book has NEVER been read into the canon (the honest "not compiled yet,
 * here is the button" state). A present object with `status: "running"` means it
 * is being read into the canon right now; `"ready"` carries `concept_count`;
 * `"failed"` means a compile raised (`error` is server prose the tutor never
 * reads — the UI turns `status` into one Greek sentence). */
export interface CompileStatus {
  status: "running" | "ready" | "failed" | (string & {});
  concept_count: number | null;
  compiled_at: string | null;
  model: string | null;
  error: string | null;
}

export interface SourceOut {
  id: string;
  title: string;
  type: string;
  status: SourceStatus | (string & {});
  domain: string | null;
  language: string | null;
  char_count: number | null;
  error: string | null;
  created_at: string;
  /** Which `Collection` this source is filed under, or `null` for the
   * synthetic "Unfiled" bucket the Library page renders client-side (there
   * is no `Collection` row for "Unfiled" — it's just every source with a
   * null FK). Added alongside `routers/library.py` (Plan 9 Task 6). */
  collection_id: string | null;

  /** Page-level truth, derived server-side per request (Stage 7.2), not columns.
   * `ocr_active` is what makes a HARD RELOAD during the 9-minute OCR honest: the
   * row can say "reading page 30 of 77" the instant the list lands, with no job
   * id and no prior React state to remember. Before this, a reload mid-OCR showed
   * the book RED ("nothing was read") with a Retry button that started a second
   * racing job. Absent (0/false) on any API old enough not to send them. */
  pages_total?: number;
  pages_ready?: number;
  pages_failed?: number;
  pages_pending?: number;
  ocr_active?: boolean;

  /** Concept-canon compile state (Part B, C7), decorated server-side per request
   * from `book_compile` (not a column — same posture as the page counts above).
   * `null` = never compiled into the canon; absent on any API old enough not to
   * send it (handled the same as `null`). */
  compile?: CompileStatus | null;
  /** An in-flight canon-compile JOB — `ocr_active`'s direct analogue, and the
   * flag that closes the compile's blind window: `book_compile.status` only
   * flips to "running" after the worker has read the whole book's context
   * (seconds on a big book), so the row and the list's poll key on THIS from
   * the moment the 202 lands. */
  compile_active?: boolean;
  /** The LATEST ocr job's failure, present only while the latest one FAILED
   * (a later successful run clears it). `error_kind` keys the localized
   * `jobErrors` copy — this is how an auth-parked read stops looking like a
   * healthy "Continue reading". */
  last_ocr_error_kind?: string | null;
  last_ocr_error?: string | null;
}

export interface ChunkPreviewOut {
  id: string;
  text: string;
  section_path: string | null;
  page: number | null;
}

export interface SourceDetailOut extends SourceOut {
  chunks: ChunkPreviewOut[];
}

export interface HitOut {
  chunk_id: string;
  source_id: string;
  source_title: string;
  text: string;
  section_path: string | null;
  page: number | null;
  /** RRF FUSION score — an ordering key only. It tops out around 0.033 and means
   *  nothing on its own; never render it as a relevance percentage. */
  score: number;
  /** Cosine similarity in [0,1] — the one number that answers "how close a match
   *  is this?". Present since the hybrid-retrieval swap (Plan 13, Stage 4). */
  vector_score?: number;
  /** Okapi BM25. 0 when the chunk was found by the dense arm alone. */
  lexical_score?: number;
}

export interface SearchResponse {
  hits: HitOut[];
}

export interface AskResponse {
  text: string;
  citations: HitOut[];
}

export interface CreateSourceInput {
  kind: SourceKind;
  title: string;
  domain?: string | null;
  language?: string | null;
  text?: string | null;
  url?: string | null;
  /** kind="url" only: >1 follows same-site links breadth-first from the URL
   * (multi-page guides — justinguitar-style course indexes) and stores one
   * Page per crawled page. Server caps at 50; omit/1 = single-page fetch. */
  crawl_pages?: number;
}

export interface UploadSourceInput {
  title: string;
  domain?: string | null;
  language?: string | null;
  file: File;
}

/** `domain`/`language` are GONE from the API (Plan 13, Stage 4.4) — both were
 * filters on columns that most real sources leave NULL, and `language` could
 * filter the tutor's English book to zero in a Greek session. Scoping a search
 * is `source_ids`, which HE chooses. */
export interface SearchInput {
  query: string;
  k?: number;
  source_ids?: string[];
}

export interface AskInput {
  query: string;
  locale: string;
  k?: number;
}

/** Thrown for any non-2xx response. `detail` is the server's message when parseable
 * (FastAPI's `{"detail": ...}` shape), else the raw response text/status.
 *
 * `code` and `body` were added with the password gate (Plan 13 Task 3.2). Some
 * errors are now *actionable* rather than merely reportable — `llm_not_configured`
 * (409) means "open Settings and paste your key", and the UI has a Greek sentence
 * and a button for that. It cannot key off `detail`, because `detail` is prose
 * written on the server, in one language, that the tutor must never actually read.
 * So the server sends `{"detail": {"code": "...", "message": "..."}}` and the UI
 * branches on `code`.
 */
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    /** Machine-readable, when the server sent one. Branch on this, never on `detail`. */
    public code?: string,
    /** The parsed response body, for the rare caller that needs more than a code. */
    public body?: unknown,
  ) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

interface ParsedError {
  detail: string;
  code?: string;
  body?: unknown;
}

async function parseError(res: Response): Promise<ParsedError> {
  const raw = await res.text().catch(() => "");
  try {
    const body = JSON.parse(raw);
    const d = body?.detail;
    if (typeof d === "string") return { detail: d, body };
    if (Array.isArray(d)) {
      return {
        detail: d.map((x: { msg?: string }) => x.msg ?? JSON.stringify(x)).join("; "),
        body,
      };
    }
    // `{"detail": {"code": ..., "message": ...}}` — the shape the auth/settings
    // routers and `main.py`'s LLMNotConfigured handler emit.
    if (d && typeof d === "object") {
      return { detail: String(d.message ?? raw), code: d.code, body };
    }
    return { detail: raw || res.statusText, body };
  } catch {
    return { detail: raw || res.statusText };
  }
}

const LOCALES = ["el", "en"];
const DEFAULT_LOCALE = "el";

/** The header that finally tells the API what language the tutor is looking at
 * (Plan 13, Stage 5.1 — `app/i18n.py`'s `LOCALE_HEADER`/`locale_dep`).
 *
 * Until now the UI locale never left the browser: `useLocale()` is read by eight
 * components and every one of them used it only to build an href. So the MODEL
 * picked the output language — a Greek tutor, on a Greek page, got English
 * artifacts and an English lesson draft. Every call in this file now carries it,
 * and the API defaults it to Greek if it is ever missing.
 */
const LOCALE_HEADER = "X-App-Locale";

/** The locale the tutor is CURRENTLY LOOKING AT, read off the URL rather than
 * threaded through 30 call sites.
 *
 * next-intl's routing (`src/i18n/routing.ts`) prefixes every route with the
 * locale — `/el/chat/…`, `/en/library` — so the first path segment IS the
 * locale, and it is the same segment `handleUnauthorized` already trusts to
 * build its login bounce. Deliberately a plain module function, NOT a hook:
 * this file is called from event handlers, polling loops and `useEffect`s, and
 * a hook would force every one of those call sites to pass a locale down.
 *
 * No `window` (SSR/prerender) => the app default, Greek — matching
 * `routing.ts`'s `defaultLocale` and `app/i18n.py`'s `DEFAULT_LOCALE`.
 */
function uiLocale(): string {
  if (typeof window === "undefined") return DEFAULT_LOCALE;
  const seg = window.location.pathname.split("/")[1];
  return LOCALES.includes(seg) ? seg : DEFAULT_LOCALE;
}

/** Set once we've committed to a bounce, so ten parallel 401s (the cockpit
 * pages fan out several fetches on mount) don't each assign `location.href`. */
let redirectingToLogin = false;

/** A 401 means the session cookie is gone or expired. The user is not going to
 * fix that by reading an error toast, so we take him to the login page.
 *
 * `/auth/*` IS EXEMPT, and that exemption is load-bearing: `POST /auth/login`
 * answers a WRONG PASSWORD with a 401. If the interceptor fired on it, the login
 * page would navigate to itself the instant the tutor typed the wrong password —
 * the calm Greek "wrong password" sentence would never render, and a stale-cookie
 * bounce would look like an infinite redirect loop.
 */
function handleUnauthorized(path: string): void {
  if (typeof window === "undefined") return;
  if (path.startsWith("/auth/")) return;
  if (redirectingToLogin) return;

  const segments = window.location.pathname.split("/");
  const locale = LOCALES.includes(segments[1]) ? segments[1] : "el";
  if (window.location.pathname === `/${locale}/login`) return;

  redirectingToLogin = true;
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.href = `/${locale}/login?next=${encodeURIComponent(next)}`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  const headers: HeadersInit = {
    ...(init.body != null && !isFormData ? { "Content-Type": "application/json" } : {}),
    // Before `...init.headers`, so a caller could override it; after nothing,
    // because no caller ever should.
    [LOCALE_HEADER]: uiLocale(),
    ...(init.headers ?? {}),
  };
  // `credentials: "include"` — the API is a DIFFERENT ORIGIN (see this file's top
  // docstring: the browser calls it directly), and fetch's default of
  // `same-origin` means the `gt_session` cookie is simply not attached. Every
  // request would 401, forever, with a perfectly valid cookie sitting in the jar.
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  if (!res.ok) {
    if (res.status === 401) handleUnauthorized(path);
    const { detail, code, body } = await parseError(res);
    throw new ApiError(res.status, detail, code, body);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function listSources(): Promise<SourceOut[]> {
  return request<SourceOut[]>("/knowledge/sources");
}

export function createSource(input: CreateSourceInput): Promise<SourceOut> {
  return request<SourceOut>("/knowledge/sources", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function uploadSource(input: UploadSourceInput): Promise<SourceOut> {
  const form = new FormData();
  form.append("title", input.title);
  if (input.domain) form.append("domain", input.domain);
  if (input.language) form.append("language", input.language);
  form.append("file", input.file);
  return request<SourceOut>("/knowledge/sources/upload", {
    method: "POST",
    body: form,
  });
}

export function getSource(id: string): Promise<SourceDetailOut> {
  return request<SourceDetailOut>(`/knowledge/sources/${id}`);
}

export function deleteSource(id: string): Promise<void> {
  return request<void>(`/knowledge/sources/${id}`, { method: "DELETE" });
}

export function searchKnowledge(input: SearchInput): Promise<SearchResponse> {
  return request<SearchResponse>("/knowledge/search", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function askKnowledge(input: AskInput): Promise<AskResponse> {
  return request<AskResponse>("/knowledge/ask", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * THE CONCEPT CANON (Part B) — the tutor's whole library distilled into concepts,
 * each carrying what EVERY book says about it, and — the entire point — where the
 * books DISAGREE. Two entry points, one shape:
 *
 *   - BROWSE (`GET /canon/concepts`, C7) — the canon's front page: every compiled
 *     concept, most-divergent first, with the counts a beginner needs to read it.
 *   - SEARCH (`POST /knowledge/concepts/search`, C8) — "what do my books say about
 *     X?", BM25 over the same concepts.
 *
 * Both return the same `ConceptHit`, so one component renders either. A concept
 * with `divergence: true` MUST render its positions against each other (Hunter vs
 * Gallagher), never averaged into one line — that divergence is the product.
 */

/** One pointer into a real book. `source_id` + a page from `pages` is exactly the
 * Reader deep-link (`/library/{source_id}?page={page}`), the same contract the
 * lesson provenance chip uses. `pages_label` is the pre-rendered, gap-aware
 * display string ("p.113" / "pp.57-58" / "pp.110-112, 118") — the server does not
 * bridge gaps, so trust it over re-deriving one from `pages`. `grounding:
 * "figure"` means the citation is OUR description of a picture/diagram — cite it,
 * never quote it as the author's words (the [FIGURE] contract). */
export interface ConceptCitation {
  source_id: string;
  source_title: string;
  pages: number[];
  pages_label: string;
  grounding: "author" | "figure" | (string & {});
}

/** One position on a concept. `kind`:
 *   - "consensus"  — two or more DIFFERENT books, the same position verbatim.
 *   - "divergence" — a book departing from what the others said. THE PRODUCT.
 *   - "only_in"    — one book covers this concept; a unique take, nobody to
 *                    disagree with. Honest under its own label, never dropped. */
export interface ConceptPosition {
  kind: "consensus" | "divergence" | "only_in" | (string & {});
  position: string;
  books: string[];
  citations: ConceptCitation[];
}

/** One concept and its full cross-book picture — mirrors
 * `app.schemas.knowledge.ConceptHitOut`. `score` is BM25 (search only, ordering);
 * on a browse hit it is 0 and meaningless. `coverage` is how many distinct books
 * treat this concept; `divergence` is whether they disagree. */
export interface ConceptHit {
  concept_id: string;
  key: string;
  label_en: string;
  label_el: string | null;
  score: number;
  coverage: number;
  divergence: boolean;
  positions: ConceptPosition[];
}

/** `GET /canon/concepts` — mirrors `app.schemas.canon.CanonOverviewOut`. The
 * counts are the honest header a beginner sees before the concepts: how many
 * books were read into the canon, how many are still reading, how many concepts
 * came out, and — the headline — on how many the books DISAGREE. */
export interface CanonOverview {
  concepts: ConceptHit[];
  total_concepts: number;
  divergence_count: number;
  books_compiled: number;
  books_compiling: number;
}

/** `ConceptSearchResponse` — `POST /knowledge/concepts/search` (C8). Same
 * `ConceptHit` as the browse view. */
export interface ConceptSearchResponse {
  hits: ConceptHit[];
}

/** Browse the whole compiled canon. 404 (endpoint not yet deployed) surfaces as
 * an `ApiError` the caller can special-case into the "still being built" empty
 * state, since the canon route ships slightly ahead of the API that serves it. */
export function browseCanon(): Promise<CanonOverview> {
  return request<CanonOverview>("/canon/concepts");
}

/** Search the concept canon (C8). No `source_ids` — a concept spans books, so
 * scoping to one would throw away the cross-book picture (the whole point). */
export function searchConcepts(query: string, k = 8): Promise<ConceptSearchResponse> {
  return request<ConceptSearchResponse>("/knowledge/concepts/search", {
    method: "POST",
    body: JSON.stringify({ query, k }),
  });
}

/** Read a book INTO the concept canon (`POST /knowledge/sources/{id}/compile`,
 * C6). THE MONEY GUARD lives server-side: pressing this on an already-compiled
 * book starts no job (`already_compiled: true`); pressing it twice on an
 * un-compiled one returns the SAME job. Reading a book costs real model time
 * against the tutor's subscription, so callers confirm first and say so. Returns
 * a 202 job envelope (poll `getJob`) or `{already_compiled: true}`.
 *
 * `opts.force` is the explicit RECOMPILE override (`?force=true`): it bypasses the
 * money guard and re-reads the whole book, REPLACING that book's canon concepts.
 * It always starts a job on a `ready` book — never `already_compiled` — so the
 * caller must confirm the real spend first. */
export interface CompileResponse {
  job_id?: string;
  already_running?: boolean;
  already_compiled?: boolean;
}

export function compileSource(id: string, opts?: { force?: boolean }): Promise<CompileResponse> {
  const query = opts?.force ? "?force=true" : "";
  return request<CompileResponse>(`/knowledge/sources/${id}/compile${query}`, { method: "POST" });
}

/**
 * Typed fetch helpers for the Curriculum APIs (`/curricula`, `/blocks`).
 * Same direct-from-browser convention as the Knowledge helpers above (see
 * this file's top docstring) — the cockpit pages call these straight from
 * client components so Playwright's `page.route` interception sees every
 * request.
 */

/** Recursive Block tree shape returned by every `/curricula/*` and
 * `/blocks/*` route (`routers.curriculum.block_to_tree` on the API side):
 * course -> module -> lesson -> segment for curriculum content, or
 * delivery_root -> session for a segmented delivery plan. `kind` is a soft,
 * relabelable string on the API — deliberately typed as `string` here, not
 * a union, so an unrecognized future kind still renders instead of failing
 * a type check. */
export interface BlockNode {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  est_minutes: number | null;
  order: number;
  language: string;
  plane: string;
  student_id: string | null;
  /** THE FIELD THE BOARD WAS MISSING. `block_to_tree` used to serialize nine
   * fields and `meta` was not one of them, so the tier badges, the gap flags
   * and the page citations were written to the database by the generator and
   * dropped at the API boundary — the board could not have rendered them if
   * it had tried. Optional here because a Block written before Stage 6 has
   * `null` meta, and every reader below treats that as "nothing to show"
   * rather than as an error. */
  meta: BlockMeta | null;
  /** EMBEDDED, not fetched. Every segment leaf used to fire its own `GET
   * /artifacts?block_id=` — ~120 in parallel on one board render, which IS
   * the "Could not load attached artifacts" error the tutor kept seeing. The
   * whole tree's artifacts now arrive from one `WHERE block_id IN (...)`. */
  artifacts: ArtifactOut[];
  children: BlockNode[];
}

/** Where a module's material actually comes from, decided by the model AFTER
 * READING THE WHOLE LIBRARY — not by a cosine score clearing a constant (that
 * constant's separation margin on this corpus was measured at 0.021).
 *
 * `gap` is not a fourth kind of source; it is the honest absence of one, and it
 * only happens under the `library_only` gap policy. It is a first-class value
 * precisely because "your library doesn't cover this" is a TRUE and useful thing
 * to tell a tutor, and unlabelled general knowledge is the bug he reported. */
export type Tier = "library" | "general_knowledge" | "web" | "gap";

/** One validated (source, page) pair a segment was actually written from.
 * `source_id` is the real `KnowledgeSource.id`, so the chip deep-links straight
 * into the Reader at the cited page — and every one of these was checked against
 * the pages the model was actually shown before it was persisted
 * (`curriculum/draft.py`: a citation the tutor clicks and finds nothing on is
 * worse than no citation, because it is one he will trust). */
export interface Citation {
  source_id: string;
  source_ref: string;
  source_title: string | null;
  page: number;
}

export type DraftStatus = "queued" | "drafting" | "ready" | "failed";

/** `Block.meta.segment_status` — a segment `apply_revision`'s `add_segment`/
 * `edit_segment` created or marked goes `queued` -> `done` once
 * `generate_segment` fills it in (the revise job's chain, or the resume
 * endpoint's segment drain — either can pick up a `queued` one), or `failed`
 * if that provider call raised. Never `drafting`: unlike a lesson, one
 * segment's generation is a single call with no in-between state worth
 * showing. A `done` segment (or a legacy one with no `segment_status` at
 * all) renders no chip — its body speaks for itself. */
export type SegmentStatus = "queued" | "done" | "failed";

/** `Block.meta` — a different shape per `kind`, all of it optional, because
 * `meta` is one plain JSON column on one table and a course, a module, a lesson
 * and a segment each keep different things in it. Kept as one flat interface
 * rather than a discriminated union: nothing here is required, every reader
 * already checks the fields it cares about, and a union keyed off `kind` (a
 * deliberately soft string on the API) would buy type-safety it cannot actually
 * guarantee. */
export interface BlockMeta {
  // module
  tier?: Tier;
  tier_requested?: string | null;
  coverage_note?: string;
  objective?: string;
  // lesson
  draft_status?: DraftStatus;
  word_count?: number;
  target_words?: number;
  floor_words?: number;
  meets_floor?: boolean;
  error?: string | null;
  // lesson + segment
  citations?: Citation[];
  section?: string;
  // segment
  segment_status?: SegmentStatus;
  segment_error?: string | null;
  // any block that has been through Extend-with-chat
  prev_body?: string;
  refined?: boolean;
  // course
  brief?: string | null;
  gap_policy?: string;
  library?: {
    token_count?: number;
    fits?: boolean;
    /** FALSE MEANS THE LIBRARY DID NOT FIT and the lessons were drafted from
     * per-module retrieval instead. The board says so, in words. A silent
     * downgrade to retrieval is the exact failure Stage 6 exists to remove. */
    full_context?: boolean;
    sources?: { ref: string; id: string; title: string; pages?: number }[];
  };
  shape?: {
    lessons_total?: number;
    modules?: number;
    minutes_per_lesson?: number;
    target_words_per_lesson?: number;
    floor_words_per_lesson?: number;
  };
  [key: string]: unknown;
}

/** `GET /curricula` row shape — template roots only (no `children`). */
export interface CurriculumListItem {
  id: string;
  title: string;
  language: string;
  target_profile: Record<string, unknown> | null;
  created_at: string;
}

export interface GenerateCurriculumInput {
  title: string;
  language: string;
  profile: Record<string, unknown>;
  domain?: string | null;
  target_minutes_total?: number | null;
}

/** `GET /jobs/{id}` response shape — mirrors the API's `schemas/jobs.py`'s
 * `JobOut` exactly. The poll target for async generation jobs (curriculum
 * generation today — see `startCurriculumGeneration` below). `status` is
 * `pending | running | succeeded | failed` on the API, but kept as a plain
 * `string` here for the same reason `BlockNode.kind`/`ArtifactOut.kind` are:
 * an unrecognized future status should still round-trip instead of failing
 * a type check. */
export interface JobOut {
  id: string;
  kind: string;
  status: string;
  result_root_id: string | null;
  error: string | null;
  error_kind: string | null;
  /** Free-form runner progress — e.g. the draft fan-out's `{phase: "drafting"}`,
   * or add-module's `{phase: "drafting", module_id: "..."}` (the id of the module
   * it just planted, so the board can highlight it). `draft_job_id` is the revise
   * chain's chained `curriculum_draft` row: the revise job succeeds the moment the
   * tree is right, and the chat polls this to surface a drafting failure. */
  progress: { phase?: string; module_id?: string; draft_job_id?: string } | null;
  created_at: string;
  updated_at: string;
}

/** `POST /curricula/generate`'s 202 response — mirrors `schemas/jobs.py`'s
 * `JobAccepted`, just enough for the caller to start polling `getJob`.
 *
 * `root_id` is set ONLY by the interview's confirm step, and it changes what the
 * UI does with this response completely: the tree ALREADY EXISTS (the outline is
 * materialized at confirm, every lesson `queued`), so the board opens instantly on
 * a real curriculum with a progress bar instead of sitting on a spinner for four
 * minutes waiting for a job to produce something to look at. */
export interface JobAccepted {
  job_id: string;
  status: string;
  root_id?: string | null;
}

/** `GET /curricula/{root}/progress` — a GROUP BY over the lesson blocks, computed
 * fresh on every poll (never a counter on the job row: the blocks are what the
 * tutor is looking at, so the blocks are what we count). This is what lets him
 * READ MODULE 1 WHILE MODULE 5 IS STILL BEING WRITTEN. */
export interface DraftProgress {
  root_id: string;
  total: number;
  queued: number;
  drafting: number;
  ready: number;
  failed: number;
  done: boolean;
  /** The reason the most recent draft RUN for this curriculum failed (the
   * `curriculum_draft` job's own `error`), or null. Set when lessons are stuck
   * `queued` because that run died — so the board shows why instead of a silent
   * "processing…". Cleared once a later run (a Resume) succeeds. */
  draft_error?: string | null;
}

export function getCurriculumProgress(rootId: string): Promise<DraftProgress> {
  return request<DraftProgress>(`/curricula/${rootId}/progress`);
}

/** RESUME. Drafts every lesson still `queued` (or newly added, or rate-limited
 * back to `queued`) under this curriculum. It is a REQUEST because a
 * `BackgroundTask` is the only thing this app can schedule and only a request can
 * schedule one — there is no worker process. That is the entire recovery story. */
export function resumeCurriculumDraft(rootId: string): Promise<JobAccepted> {
  return request<JobAccepted>(`/curricula/${rootId}/draft`, { method: "POST" });
}

/** DEEPEN one lesson: back to `queued` with a raised word target, and the ordinary
 * fan-out redrafts it. Same job, same cached library prefix — no second pipeline. */
export function deepenLesson(lessonId: string): Promise<JobAccepted> {
  return request<JobAccepted>(`/blocks/${lessonId}/deepen`, { method: "POST" });
}

/** RE-DRAFT UNDER THE CURRENT STRUCTURE (Plan C, Task 8) — the ONLY path that
 * rewrites lessons that already drafted. Unlike `resumeCurriculumDraft` above
 * (which leaves a `ready` lesson alone), this flips EVERY non-gap lesson back
 * to `queued`, `ready` ones included, and reschedules the same fan-out — which
 * reads the course's OWN blueprint fresh, so this is what actually rebuilds
 * every lesson under a blueprint the tutor just changed. It NEVER fires on its
 * own: a blueprint save never calls this, only an explicit, confirm-gated
 * button press does. */
export function redraftCurriculum(rootId: string): Promise<JobAccepted> {
  return request<JobAccepted>(`/curricula/${rootId}/redraft`, { method: "POST" });
}

export interface AddModuleInput {
  title: string;
  objective?: string;
  tier?: Tier;
  /** Insert after this module. Omitted = append. */
  after?: string | null;
}

export interface AddLessonInput {
  title: string;
  objective?: string;
  after?: string | null;
}

export function addModule(rootId: string, input: AddModuleInput): Promise<BlockNode> {
  return request<BlockNode>(`/curricula/${rootId}/modules`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export interface GenerateModuleInput {
  /** The tutor's optional steer ("πετάλια και εφέ"). Empty/null = let the model
   * pick the module the course is most obviously missing. */
  topic?: string | null;
}

/** AI ADD-MODULE. 202 + a job to poll: the planning call reads the whole library
 * (20-60s), lands ONE module that fits the existing course with its lessons
 * `queued`, then chains the ordinary draft fan-out over them. By the time the job
 * reports `succeeded` the module exists; the lessons fill in behind the same
 * progress bar the original generation used. */
export function generateModule(rootId: string, input: GenerateModuleInput): Promise<JobAccepted> {
  return request<JobAccepted>(`/curricula/${rootId}/modules/generate`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function addLesson(moduleId: string, input: AddLessonInput): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${moduleId}/lessons`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export interface LessonFromChatInput {
  title: string;
  /** The assistant answer, verbatim — stored as the lesson's one segment, no
   * LLM call anywhere on this path. */
  content: string;
  citations?: ChatCitation[] | null;
  chat_session_id?: string | null;
}

/** THE CHAT→CURRICULUM BRIDGE: land a chat answer as a real (ready) lesson
 * under a module the tutor picked from the database. Deepen on the board is
 * the later "write it out to full length" upgrade path. */
export function addLessonFromChat(moduleId: string, input: LessonFromChatInput): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${moduleId}/lessons/from-chat`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Up/down, not drag-and-drop — the tutor is reordering five modules, not a
 * thousand rows, and two buttons beat a drag library with touch targets and an
 * autoscroll. Moving past the end is a no-op on the API, not an error. */
export function reorderBlock(blockId: string, direction: "up" | "down"): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${blockId}/reorder`, {
    method: "POST",
    body: JSON.stringify({ direction }),
  });
}

/** EXTEND WITH CHAT. Chris: "a button to Extend with chat where the user writes
 * e.g. change this and give more detail about the Amp — and it actually follows
 * his instruction." Synchronous (one block, a few thousand tokens, and he is
 * sitting there watching), and the old body is stashed server-side so `undoRefine`
 * can put it back. */
export function refineBlock(blockId: string, instruction: string): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${blockId}/refine`, {
    method: "POST",
    body: JSON.stringify({ instruction }),
  });
}

export function undoRefine(blockId: string): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${blockId}/undo`, { method: "POST" });
}

export interface BlockUpdateInput {
  title?: string;
  body?: string | null;
  est_minutes?: number | null;
  order?: number;
  kind?: string;
}

export interface SegmentBlockInput {
  session_minutes: number;
  cadence_per_week?: number;
  student_id?: string | null;
}

export function listCurricula(): Promise<CurriculumListItem[]> {
  return request<CurriculumListItem[]>("/curricula");
}

export function renameCurriculum(rootId: string, title: string): Promise<CurriculumListItem> {
  return request<CurriculumListItem>(`/curricula/${rootId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export function deleteCurriculum(rootId: string): Promise<void> {
  return request<void>(`/curricula/${rootId}`, { method: "DELETE" });
}

/** Fetches the clean DOCX export and hands it to the browser as a download.
 * Not routed through `request()`: that helper always parses the body as JSON
 * (or 204s), and this response is a binary stream with its filename living in
 * `Content-Disposition`, not the JSON envelope `parseError` expects. So this
 * builds the same `${API_BASE}${path}` URL, `X-App-Locale` header, and
 * `credentials: "include"` (the cross-origin cookie requirement documented at
 * this file's top) by hand, matching `request()`'s convention without going
 * through it. A plain `<a href>` would drop that cookie behavior entirely, so
 * this goes through `fetch` + an object URL like any other authenticated
 * call. */
export async function downloadCurriculumDocx(rootId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/curricula/${rootId}/export.docx`, {
    headers: { [LOCALE_HEADER]: uiLocale() },
    credentials: "include",
  });
  if (!res.ok) {
    if (res.status === 401) handleUnauthorized(`/curricula/${rootId}/export.docx`);
    const { detail, code, body } = await parseError(res);
    throw new ApiError(res.status, detail, code, body);
  }
  const blob = await res.blob();
  const disposition = res.headers.get("content-disposition") ?? "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/);
  const filename = utf8Match ? decodeURIComponent(utf8Match[1]) : "curriculum.docx";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  // IN THE DOCUMENT BEFORE THE CLICK. A detached anchor's `click()` dispatches
  // the event, but WebKit does not start a download from it — the export
  // therefore did nothing at all inside the desktop shell: the API served the
  // .docx (200, right bytes), the blob was built, `click()` returned, and no
  // file was ever written and no error was shown. Chromium is more forgiving,
  // which is why this survived in the browser deployment.
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Deferred, not synchronous: revoking the object URL right after `.click()`
  // races the browser's own handling of that click on some engines (the
  // download can start reading the blob URL after it's already been
  // revoked) — a `setTimeout(0)` push lets the click's own navigation/save
  // step happen first.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Typed fetch helpers for the guided curriculum-authoring interview
 * (`routers/curriculum.py`'s `/curricula/interview...` routes, Plan 12 Task
 * 3 / G2) — mirrors `apps/api/app/schemas/interview.py` field-for-field.
 * Chris: "maybe llm can act as an assistant there bro, guiding him, and
 * asking him questions or corrections throughout the process." The state
 * machine itself lives entirely server-side (`app.curriculum.interview`'s
 * own docstring: the model never tracks `step`); this client is just a thin
 * typed wrapper around start/answer/get, same direct-from-browser
 * convention as every other section of this file.
 */

/** One `options[]` entry on the "scope"/"sources" steps. `type`/`char_count`/
 * `default_selected` are only ever populated for "sources" (every source's
 * title/type/char_count so the tutor can tell real material from a
 * synthetic filler source apart — see `InterviewDialog`'s own docstring).
 * The "who" step used to have options too (the student roster) — it now
 * returns `options: null` and asks level + language via `findings`. All
 * optional here (not a discriminated union) since every step's options share
 * this one shape on the wire. */
export interface InterviewOption {
  value: string;
  label: string;
  type?: string;
  char_count?: number | null;
  default_selected?: boolean;
  /** Canon compile status — sources-step only. `true` = a ready BookCompile
   * exists, so above the whole-read threshold this source arrives as canon;
   * `false` = it rides along verbatim (or via retrieval). Absent on the wire
   * for steps whose options are not sources. */
  compiled?: boolean;
}

/** One lesson of the outline, BEFORE anything has been drafted. Titles,
 * objectives and minutes — this is the cheap moment, and editing it here costs
 * nothing while editing it after the draft costs another twenty model calls. */
export interface OutlineLesson {
  title: string;
  objective: string;
  est_minutes: number;
}

/** One module of the outline. `tier` was assigned BY THE MODEL, HAVING READ THE
 * WHOLE LIBRARY — and the tutor can override it per module ("for this one, go
 * search the web"). `coverage_note` is the model's own one-sentence answer to
 * "what in his library covers this, or what is missing from it" — which is the
 * honest replacement for a cosine score with a 0.021 separation margin. */
export interface OutlineModule {
  title: string;
  objective: string;
  tier: Tier;
  coverage_note: string;
  /** Set when the tutor's gap policy CLAMPED what the model asked for (it wanted
   * `general_knowledge` under a `library_only` policy, so it became a `gap`). */
  tier_requested?: string | null;
  lessons: OutlineLesson[];
}

/** `interview.outline` on the wire — and the thing the tutor EDITS. Whatever he
 * sends back is what gets materialized; the model's original is not kept anywhere
 * and is not supposed to be. */
export interface Outline {
  title: string;
  modules: OutlineModule[];
}

/** The derived counts for one curriculum. NUMBERS, not a formatted sentence:
 * the API does not know the tutor's locale, and it used to send an English
 * string that the (fully Greek) interview then rendered verbatim. Formatting a
 * human-facing sentence server-side is a Greek bug waiting to happen — see
 * `curricula.interview.steps.sources.shape` in `messages/{en,el}.json`. */
export interface InterviewShape {
  lessons_total: number;
  modules: number;
  lessons_per_module: number[];
  target_words_per_lesson: number;
  teaching_minutes: number;
  qa_minutes: number;
}

/** The `findings` field of `InterviewStateOut` — a different payload per step,
 * all optional. "who" carries the level list, "sources" the derived shape echo,
 * "structure" the pre-filled blueprint, and "outline"/"confirm" carry the
 * outline itself. */
export interface InterviewFindings {
  levels?: string[];
  /** "who" step only — the course languages the API accepts ("el"/"en").
   * Asked explicitly since the student roster left the product: the tutor
   * runs a Greek cockpit and writes English courses, so the wizard must ask
   * rather than inherit the UI locale. */
  languages?: string[];
  shape?: InterviewShape;
  title?: string;
  modules?: OutlineModule[];
  /** "structure" step only — `resolve_default_blueprint(db)`, the SETTINGS
   * default pre-filled into the (optional, skippable) editor. Skipping leaves no
   * blueprint in `interview.answers`, and confirm falls back to this same
   * settings default (spec invariant #7). */
  blueprint?: BlueprintShape;
  /** "structure" step only — the lesson length the tutor booked two steps
   * earlier, so the blueprint editor can show each section's share as real
   * minutes of HIS lesson instead of a bare percentage. */
  minutes_per_lesson?: number | null;
}

/** The `{interview_id, step, question, options?, findings?, error?}` envelope
 * every interview route returns (mirrors `schemas/interview.py`'s
 * `InterviewStateOut`). `step` is one of `STEP_ORDER` ("who" | "duration" |
 * "scope" | "structure" | "sources" | "outline" | "confirm") or the terminal
 * "done" — kept as a plain `string` here (not a union), same "an unrecognized
 * future step still round-trips" reasoning as `BlockNode.kind`/`JobOut.status`
 * elsewhere in this file. `error` is set only when the previous answer was
 * invalid and this response is re-asking the same step's question. */
export interface InterviewStateOut {
  interview_id: string;
  step: string;
  question: string;
  options: InterviewOption[] | null;
  findings: InterviewFindings | null;
  error: string | null;
  /** The answer this step already has — set when navigating BACK, so the
   * revisited step re-renders the tutor's earlier choice, never blank
   * defaults. Shape depends on the step (same shape that step submits). */
  prior?: unknown;
  /** Set once "confirm" has materialized the tree. */
  root_id?: string | null;
  job_id?: string | null;
}

export interface InterviewStartInput {
  title: string;
}

/** Starts a brand-new interview at its first ("who") step. */
export function startInterview(input: InterviewStartInput): Promise<InterviewStateOut> {
  return request<InterviewStateOut>("/curricula/interview", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Current state of an in-progress interview — refresh-safe (`GET
 * .../{id}` never recomputes the cached "preview" findings, per
 * `app.curriculum.interview.render_state`'s own docstring). This is what the
 * resume path hydrates the dialog from. */
export function getInterview(interviewId: string): Promise<InterviewStateOut> {
  return request<InterviewStateOut>(`/curricula/interview/${interviewId}`);
}

/** One step BACK (2026-07-23: forward-only wizard). Answers already given are
 * kept server-side; the response's `prior` is what the revisited step
 * re-renders them from, so Continue re-submits the tutor's earlier choice. */
export function backInterview(interviewId: string): Promise<InterviewStateOut> {
  return request<InterviewStateOut>(`/curricula/interview/${interviewId}/back`, {
    method: "POST",
  });
}

/** The newest resumable interview (step short of "done", touched within 48h),
 * or null. The state machine was always server-side and refresh-safe; a crashed
 * browser only ever lost the POINTER to it — this is how the curricula page
 * finds it again (2026-07-23: a desktop OOM kill closed the wizard mid-flight). */
export interface OpenInterviewOut {
  interview_id: string;
  title: string;
  step: string;
  updated_at: string;
}

export function getOpenInterview(): Promise<OpenInterviewOut | null> {
  return request<OpenInterviewOut | null>("/curricula/interview/open");
}

/** Advances one step of the interview. The response is EITHER the next
 * `InterviewStateOut` (a non-final step, or a re-ask with `.error` set on an
 * invalid answer) OR, on the final "confirm" step once approved, a 202
 * `JobAccepted` — mirrors `answer_curriculum_interview`'s own two-shape
 * response on the API side exactly (see that route's docstring: "or — on
 * the final 'confirm' step, once approved — enqueue the REAL grounded
 * generation job and return 202 {job_id, status}"). Callers MUST
 * discriminate the result themselves (`isJobAccepted` below) rather than
 * assuming one shape — this is not a discriminated union on a shared `kind`
 * field, since neither shape carries one. */
export function answerInterview(
  interviewId: string,
  answer: unknown,
): Promise<InterviewStateOut | JobAccepted> {
  return request<InterviewStateOut | JobAccepted>(`/curricula/interview/${interviewId}/answer`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

/** Discriminates `answerInterview`'s two possible response shapes — a
 * `JobAccepted` always has `job_id`, an `InterviewStateOut` never does. */
export function isJobAccepted(
  result: InterviewStateOut | JobAccepted,
): result is JobAccepted {
  return typeof (result as JobAccepted).job_id === "string";
}

/** Enqueues curriculum generation (Plan 8 Task 3/4): `generate_curriculum`
 * itself is still the same SLOW guided-JSON LLM call measured at
 * 49-179s/call, but that work now runs off the request path — this call
 * returns a 202 `JobAccepted` almost immediately. Callers MUST poll
 * `getJob(job_id)` until `status` is `"succeeded"` (then fetch the tree via
 * `getCurriculum(result_root_id)`) or `"failed"`, and MUST show a clear,
 * non-frozen loading state for the whole wait (see
 * `components/curriculum/generate-dialog.tsx`'s poll loop). */
export function startCurriculumGeneration(input: GenerateCurriculumInput): Promise<JobAccepted> {
  return request<JobAccepted>("/curricula/generate", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Poll target for `startCurriculumGeneration` (and any future async job
 * kind — `JobOut.kind` distinguishes them). */
export function getJob(jobId: string): Promise<JobOut> {
  return request<JobOut>(`/jobs/${jobId}`);
}

export function getCurriculum(rootId: string): Promise<BlockNode> {
  return request<BlockNode>(`/curricula/${rootId}`);
}

export function getBlock(id: string): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${id}`);
}

export function updateBlock(id: string, input: BlockUpdateInput): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

export function deleteBlock(id: string): Promise<void> {
  return request<void>(`/blocks/${id}`, { method: "DELETE" });
}

export function segmentBlock(id: string, input: SegmentBlockInput): Promise<BlockNode> {
  return request<BlockNode>(`/blocks/${id}/segment`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Typed fetch helpers for the Artifacts API (`/artifacts/*`). Same direct-
 * from-browser convention as the Knowledge/Curriculum helpers above (see this
 * file's top docstring) — the Artifacts gallery page and the curriculum
 * board's per-segment "attach artifact" dialog both call these straight from
 * client components, so `page.route` sees every request.
 *
 * `kind` is deliberately typed as a plain `string` here, not the 7-member
 * `ArtifactKind` union `components/artifacts/types.ts` defines — same
 * reasoning as `BlockNode.kind` above: a kind this layer doesn't recognize
 * should still round-trip instead of failing a type check. Callers that need
 * the fixed 7-kind picker list import `ArtifactKind`/`ARTIFACT_KINDS` from
 * `components/artifacts/kinds.ts` instead.
 *
 * `blockId` (on `ListArtifactsInput`/`CreateArtifactInput`/
 * `GenerateArtifactInput`) is camelCase at the call site — unlike e.g.
 * `SegmentBlockInput.session_minutes`/`GenerateCurriculumInput.
 * target_minutes_total` above, which mirror their POST body's snake_case
 * field names 1:1 and are passed straight through via `JSON.stringify
 * (input)`. Here each function below builds the snake_case `block_id` wire
 * field itself instead, since callers (the gallery form, the block-card
 * attach dialog) read far more naturally passing/destructuring `blockId`.
 * `ArtifactOut` is unaffected by this — it mirrors the response body
 * verbatim (snake_case `block_id`/`created_at`/...), consistent with every
 * other `*Out` interface in this file; only these *input* shapes get the
 * camelCase treatment.
 */

export interface ArtifactOut {
  id: string;
  kind: string;
  spec: Record<string, unknown>;
  title: string;
  tags: string[];
  source: string;
  block_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateArtifactInput {
  kind: string;
  spec: Record<string, unknown>;
  title?: string | null;
  tags?: string[];
  blockId?: string | null;
}

export interface GenerateArtifactInput {
  kind: string;
  prompt: string;
  blockId?: string | null;
  ground?: boolean;
}

export interface ListArtifactsInput {
  blockId?: string | null;
  kind?: string | null;
}

export function listArtifacts({ blockId, kind }: ListArtifactsInput = {}): Promise<ArtifactOut[]> {
  const params = new URLSearchParams();
  if (blockId) params.set("block_id", blockId);
  if (kind) params.set("kind", kind);
  const qs = params.toString();
  return request<ArtifactOut[]>(`/artifacts${qs ? `?${qs}` : ""}`);
}

export function getArtifact(id: string): Promise<ArtifactOut> {
  return request<ArtifactOut>(`/artifacts/${id}`);
}

export function createArtifact(input: CreateArtifactInput): Promise<ArtifactOut> {
  return request<ArtifactOut>("/artifacts", {
    method: "POST",
    body: JSON.stringify({
      kind: input.kind,
      spec: input.spec,
      title: input.title ?? null,
      tags: input.tags ?? [],
      block_id: input.blockId ?? null,
    }),
  });
}

/** Fast relative to `startCurriculumGeneration` above — one spec, not a
 * nested tree (a chord diagram or tone recipe is a single small
 * `guided_json` call, seconds not minutes — see deploy-recon notes) — but
 * still genuinely async, so callers still need *a* loading state, just not
 * the curriculum dialog's poll-loop/blocking-dialog treatment (see
 * `components/artifacts/generate-form.tsx`). */
export function generateArtifact(input: GenerateArtifactInput): Promise<ArtifactOut> {
  return request<ArtifactOut>("/artifacts/generate", {
    method: "POST",
    body: JSON.stringify({
      kind: input.kind,
      prompt: input.prompt,
      block_id: input.blockId ?? null,
      ground: input.ground ?? false,
    }),
  });
}

export function deleteArtifact(id: string): Promise<void> {
  return request<void>(`/artifacts/${id}`, { method: "DELETE" });
}

/**
 * Typed fetch helpers for the Chat API (`/chat/*`) — Plan 5's HITL agent
 * copilot: session creation, turn-taking, and the approve/edit/reject flow
 * that gates every mutation tool call. Same direct-from-browser convention as
 * every other section of this file (see this file's top docstring); mirrors
 * `apps/api/app/schemas/chat.py` field-for-field, including that module's own
 * "one flat response shape, fields vary by `status`" choice for `ChatTurnOut`
 * (see its docstring) rather than a discriminated union per status.
 *
 * `POST /chat/{id}/messages` answers 409 if an approval is already open on
 * that session (`routers/chat.py`'s `post_message` guard, checked BEFORE the
 * new user message is even persisted). This app's UI
 * (`components/chat/chat-panel.tsx`) is what's responsible for never
 * triggering that: it disables the composer for the entire time a
 * `ChatTurnOut` with `status: "awaiting_approval"` is unresolved, so a 409
 * here would mean this client's own guard has a bug, not an expected
 * response to design around.
 */

export type ChatTurnStatus = "answer" | "awaiting_approval" | "job_pending";

/** One entry of a grounded turn's `citations` (Plan 11 Task 1, C1/C2, mirrors
 * `app.agent.loop._to_citation`'s exact shape) — what lets the chat UI render
 * a chip that deep-links into the Reader (Plan 9) at the real scanned page a
 * grounded answer actually came from. `page_no`/`page_id` are `null` when the
 * underlying chunk predates page-addressable ingest; `message-list.tsx` only
 * renders a chip when `page_no` is set (a chip with nowhere real to link
 * would be worse than no chip). */
export interface ChatCitation {
  source_id: string;
  source_title: string;
  page_no: number | null;
  page_id: string | null;
  snippet: string;
}

/** Mirrors `schemas/chat.py`'s `ChatTurnOut` — the response shape for both
 * `sendChatMessage` and `resolveApproval`. Which fields are populated
 * depends on `status`: "answer" -> `content`; "awaiting_approval" ->
 * `approval_id`/`tool_name`/`tool_args`/`description`; "job_pending"
 * (resolve only) -> `job_id`. `status` keeps the same "soft union" shape as
 * `SourceOut.status`/`JobOut.status` above (a plain API-side `str`, not a
 * `Literal`) — an unrecognized future status should still round-trip
 * instead of failing a type check. `citations` (Plan 11 Task 1/3) is set on
 * an "answer"/"awaiting_approval" turn that had something to cite, `null`/
 * omitted otherwise. */
export interface ChatTurnOut {
  status: ChatTurnStatus | (string & {});
  content?: string | null;
  approval_id?: string | null;
  tool_name?: string | null;
  tool_args?: Record<string, unknown> | null;
  description?: string | null;
  job_id?: string | null;
  citations?: ChatCitation[] | null;
}

/** One row of `getChatHistory`'s response — mirrors `schemas/chat.py`'s
 * `MessageOut` (user/assistant rows only; the API omits tool/system rows —
 * internal plumbing the chat UI never needs to render). */
export interface ChatMessageOut {
  id: string;
  role: string;
  content: string | null;
  created_at: string;
  citations?: ChatCitation[] | null;
}

/** Mirrors `schemas/chat.py`'s `PendingApprovalOut` — `GET
 * /chat/{id}/pending`'s response when a mutation is awaiting a decision, or
 * `null` when the session has none open. */
export interface PendingApprovalOut {
  id: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  status: string;
  created_at: string;
}

/** One row of a `RevisionPlan.ops` — mirrors `curriculum/revise.py`'s FLAT,
 * tagged op schema field-for-field (constraint #9 there: not `oneOf`, one
 * object per op with `op` as the discriminator and every per-op field
 * optional at this level). Rendered by `RevisionPlanCard`, never
 * reconstructed client-side: the approval card shows exactly the plan the
 * backend already validated (`approved == applied`). */
export interface RevisionPlanOp {
  op: string;
  reason: string;
  module_id?: string;
  after_lesson_id?: string;
  after_module_id?: string;
  to_module_id?: string;
  lesson_id?: string;
  /** `edit_segment`/`remove_segment`: the target segment id. `add_segment`
   * carries no `segment_id` of its own — it's a NEW segment, filed under
   * `lesson_id` (+ optional `section_key`) instead. */
  segment_id?: string;
  /** `add_segment` only: files the new segment under this ENABLED blueprint
   * section (`curriculum/revise.py`'s `_ENABLED_SECTION_KEYS` check drops the
   * op server-side if it names a disabled one). Omitted lets the backend
   * place it wherever `apply_revision`'s default does.
   *
   * `set_section_enabled` (2026-07-20 hotfix) also carries this field: the
   * section it flips — which must already exist in the blueprint, enabled or
   * not (it never invents a section; that is a full `update_blueprint`). */
  section_key?: string;
  /** `set_section_enabled` only: true to enable, false to disable. Omitted
   * defaults to true server-side (the common case: enabling a section). */
  enabled?: boolean;
  /** `set_section_enabled` only: an optional new label for this section, in
   * the course's own language — the other language's label is left as-is. */
  label?: string;
  title?: string;
  objective?: string;
  instruction?: string;
  tier?: string;
  lessons?: { title: string; objective: string }[];
  blueprint?: Record<string, unknown>;
}

/** Pure, server-computed blast-radius summary attached to every plan by
 * `_validate_pending_revision` (`compute_impact` in `curriculum/revise.py`,
 * 2026-07-20, Spec A) — counted from the VALIDATED op shapes, never from the
 * model's own `reason` claims, so `RevisionPlanCard`'s banner can show the
 * tutor honest numbers the model cannot spin. `destructive` is true iff the
 * plan REWRITES or REMOVES something that already exists (`rewrites`,
 * `lesson_removals`, `segment_removals`); pure additions and a bare
 * blueprint reshape are not, by themselves, destructive. */
export interface RevisionImpact {
  rewrites: number;
  segment_additions: number;
  segment_edits: number;
  segment_removals: number;
  lesson_removals: number;
  lessons_added: number;
  blueprint_changed: boolean;
  destructive: boolean;
}

/** One entry of `RevisionPlan.dropped` — an op `validate_ops` rejected before
 * it ever reached this plan, and why (`curriculum/revise.py`, 2026-07-20
 * hotfix). `op` is the RAW op the model proposed (not a `RevisionPlanOp`: it
 * failed validation, so it may be missing required fields or carry a
 * shorthand id — never assume its shape beyond `op.op`). */
export interface RevisionPlanDroppedOp {
  op: { op?: string; [key: string]: unknown };
  reason: string;
}

/** `propose_curriculum_revision`'s return value / `apply_curriculum_
 * revision`'s `plan` argument — mirrors `curriculum/revise.py`'s
 * `{summary, ops}` shape, id-validated server-side before it ever reaches
 * an `ApprovalRequest` (see `routers/chat.py`'s `_validate_pending_
 * revision`). `impact` is optional: an `ApprovalRequest` created before this
 * shipped has no `impact` key on its stored `tool_args.plan`, and
 * `RevisionPlanCard` renders no banner in that case rather than fabricate
 * one client-side. `dropped` is the same optionality story (2026-07-20
 * hotfix): absent/empty means nothing was cut; non-empty means the tutor is
 * approving a SMALLER plan than what was asked for, and `RevisionPlanCard`
 * must say so before he approves it. */
export interface RevisionPlan {
  summary: string;
  ops: RevisionPlanOp[];
  impact?: RevisionImpact;
  dropped?: RevisionPlanDroppedOp[];
}

export interface ResolveApprovalInput {
  decision: "approve" | "reject";
  /** Only meaningful alongside `decision: "approve"` — the tutor's edited
   * `tool_args` to run the tool with instead of the originally-proposed ones
   * (the "Edit" path in `ApprovalCard`). Omitted/`null` means "run it with
   * the args as originally proposed" (mirrors `ApprovalResolveRequest.
   * edited_args`, which the API itself only ever reads on the approve
   * branch — see `resolve_approval`'s reject branch in `routers/chat.py`). */
  editedArgs?: Record<string, unknown> | null;
}

/** A session without its transcript-derived fields — mirrors `schemas/chat.py`'s
 * `ChatSessionOut`, which is what `PATCH /chat/{id}` answers with (a rename
 * cannot change a count or a preview). `title` is NULL until the session's
 * first user message names it (server-side truncation, no model call). */
export interface ChatSessionOut {
  id: string;
  title: string | null;
  locale: string;
  student_id: string | null;
  created_at: string;
  updated_at: string;
}

/** One row of the chat sidebar's list — mirrors `schemas/chat.py`'s
 * `ChatSessionSummary`. `message_count`/`preview` count user+assistant rows
 * only, so the count matches the number of bubbles that will render. Sessions
 * with no messages are never in this list at all — see `list_chat_sessions`. */
export interface ChatSessionSummary extends ChatSessionOut {
  message_count: number;
  last_message_at: string;
  preview: string | null;
}

/** `POST /chat`'s response — mirrors `schemas/chat.py`'s
 * `ChatSessionCreated`: just enough to start posting messages/resolving
 * approvals against this session. `studentId` is optional (a chat session
 * need not be scoped to one student). `locale` records which UI language the
 * conversation was STARTED in — persisted, not re-derived at resume time.
 * `rootId` (Unit D, Task D2b) BINDS the session to one curriculum — the
 * revise drawer on `curricula/[rootId]` is the only caller that passes it.
 * The API transiently injects that curriculum's compact tree onto every
 * turn (`routers/chat.py`'s `_inject_curriculum_context`) so the model
 * always has the right `root_id` to pass to `propose_curriculum_revision`/
 * `apply_curriculum_revision`, without it ever entering the cached
 * system+tools prefix or the persisted transcript. */
export function createChatSession(
  studentId?: string | null,
  locale?: string,
  rootId?: string | null,
): Promise<{ session_id: string }> {
  return request<{ session_id: string }>("/chat", {
    method: "POST",
    body: JSON.stringify({ student_id: studentId ?? null, locale: locale ?? null, root_id: rootId ?? null }),
  });
}

/** `GET /curricula/{root}/chat-session` — GET-or-create the ONE chat session
 * bound to this curriculum (chat overhaul persistence fix). The "Revise with
 * AI" drawer used to call `createChatSession` on every open: fine within one
 * page visit (it only creates once React holds the id), but a page RELOAD
 * resets that state, so the drawer spent a brand-new session on every reload
 * and orphaned whatever conversation was already under way. This calls the
 * curriculum-scoped endpoint instead, which resumes the existing session for
 * `rootId` (keyed on `ChatSession.root_id`) or creates the first one — no
 * `locale`/`root_id` body needed, the locale rides the same `X-App-Locale`
 * header every call already sends (`request()`'s own `uiLocale()`). */
export function getOrCreateCurriculumChatSession(rootId: string): Promise<{ session_id: string }> {
  return request<{ session_id: string }>(`/curricula/${rootId}/chat-session`);
}

/** `GET /curricula/interview/{id}/chat-session` — the Part 5 planning phase's
 * own get-or-create, mirroring `getOrCreateCurriculumChatSession` above
 * exactly (same shape, same "resume-or-create" contract) but scoped to an
 * IN-PROGRESS interview rather than a materialized curriculum. */
export function getOrCreateInterviewChatSession(interviewId: string): Promise<{ session_id: string }> {
  return request<{ session_id: string }>(`/curricula/interview/${interviewId}/chat-session`);
}

/** `POST /curricula/interview/{id}/distill` — turns the planning chat's
 * transcript into an editable brief. 409 when the transcript is empty OR the
 * model produced an empty brief — both retryable-with-a-message, which is
 * why this stays a thrown `ApiError` rather than a special return shape:
 * `PlanningChat`'s catch branches on `err.status === 409` for its localized
 * `distillEmpty` message. */
export function distillInterviewBrief(interviewId: string): Promise<{ brief: string }> {
  return request<{ brief: string }>(`/curricula/interview/${interviewId}/distill`, { method: "POST" });
}

/** `PUT /curricula/interview/{id}/planning-brief` — persists the tutor's
 * (possibly hand-edited) brief onto the interview; an empty string clears
 * it. 204, no body. */
export function putInterviewPlanningBrief(interviewId: string, brief: string): Promise<void> {
  return request<void>(`/curricula/interview/${interviewId}/planning-brief`, {
    method: "PUT",
    body: JSON.stringify({ brief }),
  });
}

/** Every session that has actually been spoken in, most-recently-active
 * first (`GET /chat`). */
export function listChatSessions(): Promise<ChatSessionSummary[]> {
  return request<ChatSessionSummary[]>("/chat");
}

/** Renames a session. The server keeps the new title through subsequent
 * turns (`_ensure_title` only fires on a NULL title), so this sticks. */
export function renameChatSession(sessionId: string, title: string): Promise<ChatSessionOut> {
  return request<ChatSessionOut>(`/chat/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

/** Deletes a session and, by `ON DELETE CASCADE`, its whole transcript and
 * every approval record on it. Irreversible — every call site must go
 * through `useConfirm()` first. */
export function deleteChatSession(sessionId: string): Promise<void> {
  return request<void>(`/chat/${sessionId}`, { method: "DELETE" });
}

/** Posts one user turn and runs the agent loop against it. Callers MUST NOT
 * call this again while a previous `ChatTurnOut` came back `awaiting_
 * approval` and hasn't been resolved yet — the API 409s (see this section's
 * top docstring) — `chat-panel.tsx` enforces that by disabling its composer
 * for exactly that window. */
export function sendChatMessage(sessionId: string, content: string): Promise<ChatTurnOut> {
  return request<ChatTurnOut>(`/chat/${sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

/** Terminal outcome of `streamChatMessage` (Plan 11 Task 3, C4): "done" is a
 * genuine, now-persisted plain answer (mirrors `ChatTurnOut`'s "answer"
 * case, `citations` included); "fallback" means the SSE endpoint declined to
 * stream this turn (a tool/mutation call, a post-turn guard trip, or a mid-
 * stream error — see `app/routers/chat.py`'s `post_message_stream` docstring
 * for the exact reasons) and persisted NOTHING — the caller MUST resend the
 * same `content` through the existing `sendChatMessage` REST call to get an
 * authoritative response (that call is what actually runs the full ReAct
 * loop, the HITL suspend, and the C3 tablature guard; this stream endpoint
 * only ever handles the safe common case). */
export type ChatStreamOutcome =
  | { status: "done"; citations: ChatCitation[] | null }
  | { status: "fallback"; reason: string }
  /** A TRANSPORT failure — fetch rejected, or the stream died mid-body. Unlike
   * "fallback" (the server's own event, guaranteeing it persisted nothing),
   * here the server MAY have completed and persisted the whole billed turn
   * with only the response lost in transit. Callers must NOT auto-resend on
   * this — that re-runs the full retrieval+generation on the client's own API
   * key (~2x cost) and can duplicate the turn. Re-sync from history instead. */
  | { status: "error" };

/** One parsed `event: <name>\ndata: <json>\n\n` block off the stream — see
 * `app/routers/chat.py`'s `post_message_stream` for the exact 3 event names
 * this ever emits ("delta"/"done"/"fallback"). Returns `null` for a block
 * this client doesn't recognize (forward-compatible: an unrecognized event
 * is just skipped, not a parse error) or one with unparseable JSON `data`. */
function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let eventName: string | null = null;
  let dataLine: string | null = null;
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) dataLine = line.slice("data:".length).trim();
  }
  if (!eventName || dataLine == null) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLine) };
  } catch {
    return null;
  }
}

/**
 * SSE token streaming for the plain-answer path (Plan 11 Task 3, C4: `POST
 * /chat/{session_id}/messages/stream`, `text/event-stream`). Calls `onDelta`
 * with each text chunk AS IT ARRIVES (so a caller can append it to a live
 * transcript bubble), then resolves to the stream's terminal outcome — see
 * `ChatStreamOutcome`'s own docstring for what "done" vs "fallback" means
 * and what a caller MUST do on "fallback".
 *
 * A network/parse failure (fetch rejects, a non-2xx status, the body ends
 * without ever reaching a terminal event) resolves to `{status:
 * "fallback", reason: "error"}` rather than throwing — same posture as the
 * server's own "the safe path never persists a half-formed turn" contract;
 * callers don't need a separate try/catch just to reach the same REST
 * fallback a `"fallback"` outcome already tells them to take.
 *
 * Deliberately `fetch` + a manual `ReadableStream` reader, NOT the browser's
 * `EventSource` — `EventSource` only supports a bare `GET` with no request
 * body, and this call needs to POST the user's `content`.
 *
 * Each parsed event is followed by a `setTimeout(0)` yield before the next
 * one is processed — even when several arrive in the same underlying network
 * chunk (plausible for a small/local response) — so the caller's `onDelta`-
 * driven UI updates land as SEPARATE renders instead of one micro-batched
 * swap. This is deliberate smoothing (the same idea real streaming chat UIs
 * apply regardless of network chunking), not a workaround for a bug: without
 * it, "streamed" text could legitimately still LOOK like a single swap on a
 * fast/local connection, defeating the entire point of streaming (Chris's
 * complaint was the dead pause, not the byte-level transport).
 */
export async function streamChatMessage(
  sessionId: string,
  content: string,
  onDelta: (text: string) => void,
): Promise<ChatStreamOutcome> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/chat/${sessionId}/messages/stream`, {
      method: "POST",
      // THIS FETCH BYPASSES `request()` ENTIRELY — it has to, because it reads a
      // streaming body rather than awaiting `.json()`. Which means it bypasses
      // BOTH things `request()` adds for everyone else, and it is the only call
      // in the app that does:
      //   - `credentials: "include"`: miss it and every other page works while
      //     chat — the flagship — silently 401s and falls back to the
      //     non-streaming path. It looks like a streaming bug, not an auth bug.
      //   - `X-App-Locale`: miss it and the ONE endpoint the tutor talks to most
      //     is the ONE endpoint with no locale, so the streamed answer comes back
      //     in the default language while every other surface honours the UI.
      headers: { "Content-Type": "application/json", [LOCALE_HEADER]: uiLocale() },
      body: JSON.stringify({ content }),
      credentials: "include",
    });
  } catch {
    // The POST may have REACHED the server even though we never saw a
    // response — "error", not "fallback": a resend could double-bill.
    return { status: "error" };
  }
  if (!res.ok || !res.body) {
    if (res?.status === 401) handleUnauthorized("/chat");
    // A non-2xx means the server refused BEFORE starting the turn — nothing
    // persisted, nothing billed; the REST fallback is safe and will surface
    // a proper error message.
    return { status: "fallback", reason: "http" };
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseBlock(block);
        if (!parsed) continue;

        if (parsed.event === "delta") {
          const text = (parsed.data as { text?: string }).text;
          if (text) onDelta(text);
        } else if (parsed.event === "done") {
          const citations = (parsed.data as { citations?: ChatCitation[] | null }).citations ?? null;
          return { status: "done", citations };
        } else if (parsed.event === "fallback") {
          const reason = (parsed.data as { reason?: string }).reason ?? "unknown";
          return { status: "fallback", reason };
        }
        // Deliberate yield — see this function's own docstring.
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
  } catch {
    // Mid-stream death: the server-side generation was running and may have
    // finished + persisted. NOT safe to resend.
    return { status: "error" };
  }
  // The body ended without a "done"/"fallback" event — same ambiguity as a
  // mid-stream death: the turn may exist server-side. Not a resend.
  return { status: "error" };
}

/** Resolves one pending approval: reject narrates the refusal and hands
 * control back to the model; approve either runs the mutation synchronously
 * (`status: "answer"`) or, for the one `async_job` tool (`generate_
 * curriculum`), enqueues a background job (`status: "job_pending"`, `job_id`
 * set) — see `resolve_approval` in `routers/chat.py`. Callers handling
 * `job_pending` should poll the EXISTING Plan 8 `getJob` below, same ~2s/cap
 * convention as `components/curriculum/generate-dialog.tsx`. */
export function resolveApproval(
  sessionId: string,
  approvalId: string,
  input: ResolveApprovalInput,
): Promise<ChatTurnOut> {
  return request<ChatTurnOut>(`/chat/${sessionId}/approvals/${approvalId}/resolve`, {
    method: "POST",
    body: JSON.stringify({
      decision: input.decision,
      edited_args: input.editedArgs ?? null,
    }),
  });
}

/** The session's transcript (user/assistant rows only, oldest first) —
 * `chat-panel.tsx` hydrates from this on every mount now that the session id
 * lives in the URL (`/{locale}/chat/{sessionId}`). Tool rows are omitted
 * server-side; a `content: null` row is a tool-calls-only assistant turn. */
export function getChatHistory(sessionId: string): Promise<ChatMessageOut[]> {
  return request<ChatMessageOut[]>(`/chat/${sessionId}`);
}

/** The session's currently-open approval, or `null` — the other half of
 * hydration. A session reloaded mid-approval must come back with its HITL
 * card intact and its composer disabled; this supplies the tool call, and the
 * TRAILING ASSISTANT ROW of `getChatHistory` supplies the card's description
 * (the API deliberately carries no `has_tool_calls`/`description` field on
 * `MessageOut` — see `routers/chat.py`). */
export function getPendingApproval(sessionId: string): Promise<PendingApprovalOut | null> {
  return request<PendingApprovalOut | null>(`/chat/${sessionId}/pending`);
}

/** Mirrors `schemas/chat.py`'s `SuggestionsOut` — `POST /chat/{id}/
 * suggestions`'s response: 0-3 short, concrete "next move" chips. An empty
 * list means either "nothing to suggest yet" or "the call failed" — this
 * endpoint deliberately never distinguishes the two on the wire (see that
 * schema's own docstring), so callers must not either. */
export interface ChatSuggestionsOut {
  suggestions: string[];
}

/** Suggestion CHIPS (chat overhaul, Piece B) — a SEPARATE, lightweight call
 * from the turn itself, meant to be fired AFTER an assistant answer already
 * rendered (`chat-panel.tsx`'s own `fetchSuggestions`) and never awaited
 * before showing that answer. Never throws past a network-level failure in
 * the ordinary sense that matters here: the API itself already degrades
 * every internal failure to `{"suggestions": []}` rather than a 4xx/5xx, so
 * the one thing a caller still needs to guard is the fetch itself failing
 * (offline, CORS, ...) — which `request()`'s normal `ApiError` throw covers,
 * same as every other call in this file. */
export function getChatSuggestions(sessionId: string): Promise<ChatSuggestionsOut> {
  return request<ChatSuggestionsOut>(`/chat/${sessionId}/suggestions`, { method: "POST" });
}

/** `POST /chat/{session_id}/distill` — the revise chat's "talk it through
 * first" exit: distills the conversation into ONE tutor-voiced revision
 * instruction and returns it for the tutor to review/edit in the composer.
 * Curriculum-bound sessions only; the API 409s otherwise (and for a chat with
 * no tutor turns yet). Nothing is planned or applied by this call. */
export function distillChatInstruction(sessionId: string): Promise<{ instruction: string }> {
  return request<{ instruction: string }>(`/chat/${sessionId}/distill`, { method: "POST" });
}

/**
 * Typed fetch helpers for the Library API (`routers/library.py`, Plan 9
 * Task 6) — collections, the source patch/retry/OCR controls, and the
 * page-level reader. Same direct-from-browser convention as every other
 * section of this file (see this file's top docstring). Ingestion itself
 * (`createSource`/`uploadSource`/`listSources`/`deleteSource` above) stays
 * where it is — this section is everything downstream of a `SourceOut`
 * already existing, mirroring the API-side router split.
 */

export interface CollectionOut {
  id: string;
  name: string;
  source_count: number;
}

export function listCollections(): Promise<CollectionOut[]> {
  return request<CollectionOut[]>("/library/collections");
}

export function createCollection(name: string): Promise<CollectionOut> {
  return request<CollectionOut>("/library/collections", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

/** Deletes the FOLDER, never the tutor's material: `KnowledgeSource.
 * collection_id` is `ondelete="SET NULL"`, so the sources inside it just
 * become Unfiled (`routers/library.py::delete_collection`, spec D7). The
 * route has existed since the Library was built and had no caller — the UI
 * could create folders it could never remove. */
export function deleteCollection(id: string): Promise<void> {
  return request<void>(`/library/collections/${id}`, { method: "DELETE" });
}

/** Mirrors `routers/library.py::patch_source`'s response — deliberately NOT
 * the full `SourceOut` (the API only echoes back the 3 fields it touched).
 * Callers that need the full row (every caller in this app) just `refresh()`
 * their own `listSources()` afterward rather than trying to splice this
 * partial shape back into one. */
export interface SourcePatchResult {
  id: string;
  title: string;
  collection_id: string | null;
}

/** Files (or un-files, via `collectionId: null`) a source into a
 * `Collection`. `collection_id` is ALWAYS sent explicitly (never omitted) —
 * `routers/library.py::patch_source` distinguishes "field omitted" (leave
 * untouched) from "field explicitly null" (move to Unfiled) via Pydantic's
 * `model_fields_set`, and this call only ever means the latter. */
export function moveSource(id: string, collectionId: string | null): Promise<SourcePatchResult> {
  return request<SourcePatchResult>(`/knowledge/sources/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ collection_id: collectionId }),
  });
}

/** Retries a failed/empty ingest. 202 with a `job_id` for "pdf" (re-enqueues
 * OCR) and "url" (re-fetches the source's own stored `url`) sources; the API
 * 409s for any other type ("text"/"note"/"image" have no original to
 * re-fetch — see `routers/library.py::retry_source`'s docstring), which
 * surfaces here as a thrown `ApiError`. Callers (`SourceRow`) only render
 * the Retry button for "pdf"/"url" sources in the first place, so a 409 here
 * would mean that gate has a bug, not an expected response to design
 * around — same posture this file takes for `sendChatMessage`'s 409. */
export function retrySource(id: string): Promise<OcrJobRef> {
  return request<OcrJobRef>(`/knowledge/sources/${id}/retry`, { method: "POST" });
}

/** What both OCR producers return. `already_running: true` means the server
 * DECLINED to start a second job and handed back the one already reading this
 * book (`routers/library.py::_enqueue_ocr` — the in-flight guard). The UI does
 * not need to care: either way it now has the id of the one job that is running,
 * and progress comes from the server regardless. */
export interface OcrJobRef {
  job_id: string | null;
  already_running?: boolean;
}

/** Renames a source. The API has supported this since Plan 9 (`PATCH
 * /knowledge/sources/{id}` takes `title`) and NOTHING in the UI ever called it —
 * a PDF filed under a typo'd title was a typo forever. */
export function renameSource(id: string, title: string): Promise<SourcePatchResult> {
  return request<SourcePatchResult>(`/knowledge/sources/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

/** Renames a `Collection` (`PATCH /library/collections/{id}`) — same story as
 * `renameSource`: the route existed, the UI never called it. */
export function renameCollection(id: string, name: string): Promise<CollectionOut> {
  return request<CollectionOut>(`/library/collections/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

/** `GET /knowledge/sources/{id}/progress` — SERVER-COMPUTED OCR progress
 * (`schemas/library.py::SourceProgress`). Every field is derived from `Page`
 * rows + the in-flight `GenerationJob`, so it reads the same in a tab that never
 * pressed the button, after a hard reload, and tomorrow. This is what replaced
 * `library/page.tsx`'s `ocrProgress`/`watchOcr` tab-local state machine.
 *
 * `current_page` is null when nothing is running — a finished book is not "on" a
 * page. `empty` pages are NOT failures: a real scan has blank pages, and counting
 * them as defects would paint a healthy book amber. */
export interface SourceProgressOut {
  source_id: string;
  total: number;
  ready: number;
  failed: number;
  empty: number;
  pending: number;
  current_page: number | null;
  active: boolean;
  job_id: string | null;
}

export function getSourceProgress(id: string): Promise<SourceProgressOut> {
  return request<SourceProgressOut>(`/knowledge/sources/${id}/progress`);
}

/* `startOcr` (POST /knowledge/sources/{id}/ocr) USED TO LIVE HERE, and
 * `AddSourceDialog` called it the instant a PDF upload landed. That was free and
 * right while OCR was a local Qwen box. It is now Claude on the tutor's
 * subscription at ~40s a page, so dropping his four books in would have silently
 * started 888 pages = 8-12 hours of model time against a 5-hour cap, with no
 * dialog and no estimate. Reading a book is now a decision he makes, on a button
 * that says what it costs — `reocrSource` below.
 *
 * The server route still exists (it is what `retrySource` reaches for a PDF, and
 * it has its own tests); it simply has no unprompted caller in the UI any more,
 * which is the entire point. Do not re-add one. */

/** `POST /knowledge/sources/{id}/reocr` — re-read a book's unread pages with
 * the vision model. THE BUTTON, and it resumes.
 *
 * Deliberately NOT called on upload, and it must never become so: at ~40s a page
 * through `claude -p`, the tutor's four books are 888 pages — 8-12 hours of model
 * time and a repeated slice of a 5-hour subscription cap shared with everything
 * else the app does for him. So it is his decision, and the confirm dialog quotes
 * what it costs (`SourceRow.requestReocr`).
 *
 * Every press costs only the pages still unread (`ocr_source` picks up nothing
 * that is already `ready`), which is what makes an 8-hour run survivable across
 * the several sittings it will really take: press, hit the cap, come back
 * tomorrow, press again.
 *
 * 409 for a non-PDF (nothing to re-read) — `SourceRow` only renders the action
 * for "pdf" sources, so a 409 here means that gate has a bug, not an expected
 * response to design around. Same posture as `retrySource`. */
export function reocrSource(id: string): Promise<OcrJobRef> {
  return request<OcrJobRef>(`/knowledge/sources/${id}/reocr`, { method: "POST" });
}

/** One row of `GET /knowledge/sources/{id}/pages` — mirrors
 * `schemas/library.py`'s `PageSummary`. `status` is the full
 * `PAGE_STATUSES` set (`pending`/`ocr_running`/`ready`/`failed`/`empty`) as
 * a plain `string`, not a union — same "unrecognized future value still
 * round-trips" reasoning as `SourceOut.status`/`JobOut.status` above. Used
 * by the Library page to derive OCR progress ("page N of M ready") for a
 * source it's actively watching — see `library/page.tsx`'s `watchOcr`. */
export interface PageSummary {
  page_no: number;
  status: string;
}

export function listSourcePages(id: string): Promise<PageSummary[]> {
  return request<PageSummary[]>(`/knowledge/sources/${id}/pages`);
}

/** `GET /knowledge/sources/{id}/pages/{n}` — mirrors `schemas/library.py`'s
 * `PageOut`: the reader's single-page view. `image_url` is a path relative
 * to `API_BASE` (`/media/pages/{page_id}.jpg`, only set when the page has a
 * scan — see `routers/library.py::get_page_image`'s docstring on why a D2
 * degenerate url/text/note page has none); resolve it with `apiMediaUrl`
 * below before using it as an `<img src>`. */
export interface PageDetailOut {
  id: string;
  page_no: number;
  status: string;
  text: string | null;
  image_url: string | null;
  total_pages: number;
}

export function getSourcePage(id: string, pageNo: number): Promise<PageDetailOut> {
  return request<PageDetailOut>(`/knowledge/sources/${id}/pages/${pageNo}`);
}

/** Resolves a server-relative media path (`PageDetailOut.image_url`) against
 * `API_BASE` for use as an `<img src>` — same "browser is the caller, not a
 * Next.js server" reasoning as this file's top docstring, so this can't be a
 * root-relative Next.js asset path. */
export function apiMediaUrl(path: string): string {
  return `${API_BASE}${path}`;
}

/**
 * Typed fetch helper for the Library-to-lesson-authoring seam
 * (`routers/lessons.py::from_selection`, Plan 10 Task 1, extended Plan 12
 * Task 4 / G4 for a range) — the Reader's "author a lesson" action.
 * Enqueues a `GenerationJob(kind="lesson")` and returns 202 almost
 * immediately (same `JobAccepted` shape/convention as
 * `startCurriculumGeneration` below): `draft_lesson_from_selection` is a
 * blocking guided-JSON LLM call, too slow for a synchronous request/response
 * cycle. Callers MUST poll `getJob(job_id)` until it reaches a terminal
 * status — `result_root_id` is the drafted lesson's root Block id, fetchable
 * via `getLesson` once `status === "succeeded"` — same "enqueue, then poll"
 * contract `components/library/selection-action.tsx` drives, mirroring
 * `components/curriculum/generate-dialog.tsx`'s poll loop verbatim.
 *
 * `pageFrom`/`pageTo` is the current shape — the continuous-scroll Reader
 * lets a selection cross a page boundary (G4), so this always sends BOTH
 * bounds (equal for a single-page selection); the API still separately
 * accepts a legacy `page_no`-only body from anything else that posts here.
 */
export function authorFromSelection(
  sourceId: string,
  pageFrom: number,
  pageTo: number,
  text: string,
): Promise<JobAccepted> {
  return request<JobAccepted>("/lessons/from-selection", {
    method: "POST",
    body: JSON.stringify({
      source_id: sourceId, page_from: pageFrom, page_to: pageTo, text,
    }),
  });
}

/**
 * Typed fetch helpers for Lesson Authoring (`routers/lessons.py`, Plan 10
 * Task 4) — the lesson list + outline editor. Same direct-from-browser
 * convention as every other section of this file. `getLesson`/
 * `splitSession`/`mergeSessions`/`addSession` all return the SAME `BlockNode`
 * tree shape `getCurriculum`/`getBlock` do above (`block_to_tree`, reused
 * verbatim by `routers.lessons` from `routers.curriculum` — see that
 * router's own docstring) — a lesson is just `Block(kind="lesson")` with
 * `session` children and `item` grandchildren, no separate shape needed.
 * Renaming/deleting any single session or item block reuses the EXISTING
 * `updateBlock`/`deleteBlock` above — there is no lesson-specific PATCH/
 * DELETE route.
 */

export interface LessonProvenance {
  source_id: string;
  page_no: number;
  /** The whole range a lesson was drafted from (G4, Plan 12 Task 4) —
   * absent on lessons drafted before this range was recorded, and always
   * equal to `{page_no, page_no}` for a single-page selection. `page_no`
   * itself is always `page_from` (see `app.lessons.draft._persist_tree`), so
   * anything that only knows about `page_no` (this file's own `ProvenanceChip`
   * usages elsewhere) keeps working unchanged. */
  page_from?: number;
  page_to?: number;
}

/** `GET /lessons` row shape — mirrors `schemas/lessons.py`'s
 * `LessonListItem`. `provenance` is `null` for a lesson created any way
 * other than `POST /lessons/from-selection` (there is currently no other
 * way to create one, but the API leaves room for it). */
export interface LessonListItem {
  id: string;
  title: string;
  created_at: string;
  provenance: LessonProvenance | null;
}

export function listLessons(): Promise<LessonListItem[]> {
  return request<LessonListItem[]>("/lessons");
}

/** `GET /lessons/{id}` — the lesson -> session -> item tree. Deliberately
 * does NOT carry `provenance` (that only lives on `LessonListItem`, above —
 * see `schemas/lessons.py`'s `get_lesson` route, which returns the shared
 * `BlockTreeOut` with no provenance field at all): a caller that needs both
 * the tree AND the provenance chip (the editor page) also calls
 * `listLessons()` and looks its own id up there. */
export function getLesson(id: string): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${id}`);
}

export interface SplitSessionInput {
  session_minutes: number;
}

/** Cuts one over-long session into several, packed to ~`session_minutes`
 * each (`app.lessons.edit.split_session`'s deterministic bin-packer — no LLM
 * call). Returns the WHOLE lesson tree, not just the new sessions — callers
 * replace their entire tree state with the response wholesale (see
 * `components/lessons/session-card.tsx`'s `applyTree`) rather than trying to
 * splice the split session's replacement in by hand. 422s (thrown here as an
 * `ApiError`) if the session has no items to split. */
export function splitSession(lessonId: string, sessionId: string, input: SplitSessionInput): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${lessonId}/sessions/${sessionId}/split`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Folds >=2 sessions of the same lesson into the first (`sessionIds[0]`,
 * the survivor). The API 422s (surfaced as an `ApiError`) unless the given
 * sessions are ADJACENT in order (`app.lessons.edit.merge_sessions`'s own
 * docstring) — this app's UI never offers a picker that could ask for a
 * non-adjacent pair (`SessionCard`'s "merge with the one below" only ever
 * passes `[this session, the very next one]`), so a 422 here is a genuine
 * edge case to surface honestly, not a client bug to paper over. Returns
 * the whole lesson tree, same as `splitSession`. */
export function mergeSessions(lessonId: string, sessionIds: string[]): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${lessonId}/sessions/merge`, {
    method: "POST",
    body: JSON.stringify({ session_ids: sessionIds }),
  });
}

export interface AddSessionInput {
  title: string;
  est_minutes?: number | null;
  after?: string | null;
}

/** Appends a new, empty session (or inserts it right after `input.after`).
 * Returns the whole lesson tree, same as `splitSession`/`mergeSessions`. */
export function addSession(lessonId: string, input: AddSessionInput): Promise<BlockNode> {
  return request<BlockNode>(`/lessons/${lessonId}/sessions`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Auth + Settings (Plan 13 Stage 3).
 *
 * `login` deliberately does NOT throw on a wrong password. A 401 here is the
 * expected answer to a question the form exists to ask, not an exception — and
 * `request()`'s 401 interceptor skips `/auth/*` precisely so this can be true
 * (see `handleUnauthorized`). The caller reads `authenticated` and renders one
 * calm sentence.
 */

export interface AuthState {
  authenticated: boolean;
  /** False on a dev/local build with `AUTH_ENABLED=0`: there is no password to
   * type, so the login page must not be a door the tutor can get stuck behind. */
  auth_enabled: boolean;
}

export function getAuthState(): Promise<AuthState> {
  return request<AuthState>("/auth/me");
}

export async function login(password: string): Promise<AuthState> {
  // One of three fetches in this file that bypass `request()` entirely — the
  // other two are `downloadCurriculumDocx` (a binary response `request()`'s
  // JSON-only parsing can't handle) and `streamChatMessage` (a streamed body
  // it never awaits `.json()` on). This one bypasses it for a different
  // reason: a 401 here is a WRONG PASSWORD, not an expired session, so it
  // must not reach the redirect-to-login interceptor `request()` wires up on
  // 401. It still carries the locale — "every request to the API carries
  // X-App-Locale" is only a checkable invariant if it has no exceptions.
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", [LOCALE_HEADER]: uiLocale() },
    body: JSON.stringify({ password }),
    credentials: "include",
  });
  if (res.status === 401) return { authenticated: false, auth_enabled: true };
  if (!res.ok) throw new ApiError(res.status, (await parseError(res)).detail);
  return (await res.json()) as AuthState;
}

export function logout(): Promise<AuthState> {
  return request<AuthState>("/auth/logout", { method: "POST" });
}

export type LlmModel = "claude-sonnet-5" | "claude-haiku-4-5";

export interface AppSettings {
  provider: string;
  model: LlmModel | (string & {});
  /** Is there a usable key? Drives the "not configured" banner in the AppShell. */
  configured: boolean;
  /** THE LAST FOUR CHARACTERS OF THE KEY, and nothing else — the server never
   * sends more (see `settings_store.mask_key`). The `sk-ant-…` prefix you see in
   * the UI is rendered by the browser from a constant, so that a regression test
   * can grep the API's response for `sk-ant-` and fail if it ever appears. */
  key_hint: string | null;
}

export interface SettingsTestResult {
  ok: boolean;
  model: string;
  /** One of: not_configured | invalid_key | no_access | unknown_model |
   * rate_limited | network | generation_failed. The UI maps this to ONE plain
   * sentence in the tutor's language. He never sees a status code, a stack
   * trace, or the word "Anthropic" followed by a number. */
  code: string | null;
}

export function getSettings(): Promise<AppSettings> {
  return request<AppSettings>("/settings");
}

export function saveSettings(input: {
  anthropic_key?: string;
  model?: LlmModel;
}): Promise<AppSettings> {
  return request<AppSettings>("/settings", { method: "PUT", body: JSON.stringify(input) });
}

/** A failed test comes back as HTTP 200 with `ok: false` — a wrong key is the
 * expected outcome of a button whose entire job is to find out, not an error. */
export function testSettings(anthropic_key?: string): Promise<SettingsTestResult> {
  return request<SettingsTestResult>("/settings/test", {
    method: "POST",
    body: JSON.stringify({ anthropic_key: anthropic_key ?? null }),
  });
}

/**
 * Typed fetch helpers for the Prompts API (`/prompts/*`) — every prompt this app
 * sends to a model, readable; the one sentence that carries no contract,
 * editable. Mirrors `apps/api/app/routers/prompts.py`'s response models
 * field-for-field.
 *
 * READING IS THE FEATURE. Four of these five calls exist to SHOW; one changes
 * something, and the thing it can change is how the lesson writer is told to
 * address a student.
 *
 * `title_el`/`what_it_does_el`/`when_it_runs_el`/`label_el` are Greek on the
 * WIRE, and that is not a locale bug — they are the registry's own annotation of
 * a prompt (`app/prompts/registry.py`), written once beside the thing they
 * describe, not UI chrome. `messages/{el,en}.json` owns the card's chrome; the
 * API owns the account of what each prompt does. The `_el` suffix is the API's,
 * kept verbatim here so the two files can be diffed against each other.
 */

export interface PromptSummary {
  id: string;
  flow: string;
  /** "prompt" | "fragment" — a fragment is injected INTO another prompt rather
   * than sent on its own. Kept a plain `string` for the same reason
   * `BlockNode.kind` is: an unrecognized future kind should still render. */
  kind: string;
  title_el: string;
  what_it_does_el: string;
  when_it_runs_el: string;
  /** The provider that sends this, when only one does; `null` = all of them. The
   * viewer must show what the ACTIVE provider (`AppSettings.provider`) actually
   * sends — a card for a prompt the app is not currently sending is a lie. */
  provider: string | null;
  /** This prompt sits inside the cached prefix (`curriculum/corpus.py`). */
  cache_prefix: boolean;
  /** The model's LANGUAGE for this prompt is decided by the COURSE — the wizard's
   * "who" step asks for it explicitly (`_answer_who` in
   * `app.curriculum.interview`) — not by the cockpit locale. Chris caught the
   * viewer claiming otherwise: the preview said Greek while a course whose
   * language is 'en' had the model told English. 5 of his 6 courses are English. */
  language_from_course: boolean;
  /** Whether the UI must warn before a change. Equal to `cache_prefix` today and
   * a different question: that one is where the prompt sits on the wire, this one
   * is what an edit costs. Never re-derive it from `cache_prefix` here — the API
   * already knows the rule. */
  cache_cost_warning: boolean;
  /** One of the ten prompts that actually shape a course/lesson (`curriculum.system`,
   * `curriculum.library`, `curriculum.outline`, `curriculum.extend`, `lesson.draft`,
   * `lesson.tier_library`, `lesson.tier_web`, `lesson.gap`, `lesson.deepen`,
   * `lesson.repair`). `prompt-list.tsx` renders these as a synthetic "Curriculum"
   * group at the top — a shortcut into the flow groups below, not a second copy
   * of them. */
  curriculum_group: boolean;
  has_override: boolean;
}

/** One message as the provider receives it. `cached` marks the prompt-cache
 * breakpoint. */
export interface PromptMessage {
  role: string;
  content: string;
  cached: boolean;
}

/** Where an interpolated variable landed in `PromptDetail.text`, so the UI can
 * draw it as a labelled chip instead of a hole: `text.slice(start, end) ===
 * value` is a test on the API side. `value` is a representative sample of his
 * data — except a slice's span, which is the real resolved text. */
export interface PromptSpan {
  name: string;
  label_el: string;
  value: string;
  start: number;
  end: number;
}

/** A contiguous, contract-free region the tutor may edit. There is exactly ONE
 * in the whole app (`student.pitch`), and that is a finding, not a placeholder:
 * an audit found the pedagogy is interwoven with the contracts everywhere else. */
export interface PromptSlice {
  id: string;
  prompt_id: string;
  label_el: string;
  /** "replace" | "append". */
  kind: string;
  /** The text in code — the reset target, and why defaults are not seeded into
   * the table. */
  default: string;
  /** What the model actually gets right now: his override if he saved one, else
   * `default`. */
  effective: string;
  has_override: boolean;
  max_chars: number;
  cache_cost_warning: boolean;
}

export interface PromptDetail extends PromptSummary {
  /** "app/agent/prompts.py:67". NOT shown to the tutor any more — Chris: *"i dont
   * think this should be seen by the tutor"*, and he is right: a file path is the
   * same category as a stack trace or a status code, which `settings/page.tsx:22-37`
   * says he never sees. Kept on the wire because the registry's completeness test
   * leans on it and because a developer reading this route is who it is for. */
  source_ref: string;
  /** The language this preview was rendered at. Only meaningful (non-null) when
   * `language_from_course` — it is the answer to "which language am I looking at?",
   * which the cockpit locale cannot be trusted to give for these prompts. */
  course_language: string | null;
  /** The full rendered prompt: a "\n\n" join of `messages[].content`, and what
   * `spans` index into. `messages` is what goes on the wire; this is the
   * presentational join. */
  text: string;
  messages: PromptMessage[];
  spans: PromptSpan[];
  slices: PromptSlice[];
}

export interface PromptSliceHistoryEntry {
  id: string;
  text: string;
  replaced_at: string;
}

/** Every prompt, in registration order (chat first). No `text`: rendering all 31
 * means building all 31, and the largest is ~14,000 chars of tool schemas. The
 * list draws the cards; `getPrompt` fills one in when he opens it. */
export function listPrompts(): Promise<PromptSummary[]> {
  return request<PromptSummary[]>("/prompts");
}

/** One prompt, rendered as the model would receive it right now — including any
 * slice he has overridden, because the preview runs the same builders the live
 * path does. Carries `X-App-Locale` like every call here, which matters: the
 * language directive is injected into 8 of these.
 *
 * `courseLanguage` only does anything for a `language_from_course` prompt, and it is
 * the honest answer to a real bug: those prompts take their language from the COURSE,
 * so the cockpit locale this call already sends is the wrong control for them. Passing
 * it renders the preview at the other language, so he can SEE that the value is not
 * this screen's to give. */
export function getPrompt(id: string, courseLanguage?: string): Promise<PromptDetail> {
  const q = courseLanguage ? `?course_language=${encodeURIComponent(courseLanguage)}` : "";
  return request<PromptDetail>(`/prompts/${encodeURIComponent(id)}${q}`);
}

/** Validate -> snapshot -> save. Validation runs BEFORE the write and the write
 * is all-or-nothing, so a rejected edit leaves the stored text untouched. Throws
 * `ApiError` with `code` one of: `unknown_slice`, `empty`, `too_long`,
 * `missing_placeholder`, `unknown_placeholder`, `malformed_braces`. */
export function savePromptSlice(sliceId: string, text: string): Promise<PromptSlice> {
  return request<PromptSlice>(`/prompts/slices/${encodeURIComponent(sliceId)}`, {
    method: "PUT",
    body: JSON.stringify({ text }),
  });
}

/** Back to the code default. IDEMPOTENT — resetting something never overridden
 * is a 200 with `has_override: false`, not a 404. */
export function resetPromptSlice(sliceId: string): Promise<PromptSlice> {
  return request<PromptSlice>(`/prompts/slices/${encodeURIComponent(sliceId)}`, {
    method: "DELETE",
  });
}

/** Every text this slice used to have, NEWEST FIRST — the question this answers
 * is "give me back what I just lost", and the answer is almost always the top
 * row. Empty for a never-edited slice, and after the FIRST edit of one (what
 * that replaced was the code default, which is in git). */
export function getPromptSliceHistory(sliceId: string): Promise<PromptSliceHistoryEntry[]> {
  return request<PromptSliceHistoryEntry[]>(
    `/prompts/slices/${encodeURIComponent(sliceId)}/history`,
  );
}

/**
 * Typed fetch helpers for the LESSON BLUEPRINT (`/blueprint/*`, Plan C, Task 6) —
 * the 8-section lesson skeleton, lifted out of code and into per-curriculum DATA
 * so a tutor can reshape it without touching the engine. Mirrors
 * `app/routers/blueprint.py`'s response models and `app.curriculum.blueprint`'s
 * shape field-for-field.
 *
 * TWO resolvers exist on the server and this file mirrors both, never conflating
 * them: `getBlueprintDefault` is the SETTINGS default (the tutor's edit if he has
 * one, else the code default) — read/written here and pre-filled into the wizard's
 * "structure" step. `getBlueprintCodeDefault` is ALWAYS the git-backed default —
 * the Restore target, never affected by a save. Editing the settings default never
 * touches a course that already exists (spec invariant #3); that guarantee is
 * entirely server-side, this file only reads and writes the row.
 */

/** One section of a blueprint. `kind` decides what is locked in the editor:
 * `exercises` (keyed `exercises`) and `qa` (keyed `qa_prompts`) are STRUCTURED —
 * their model-facing schema is a fixed shape built in code, keyed by `kind`, so
 * their `key` stays read-only while they exist. They may be reweighted,
 * re-audienced, reordered, relabelled, disabled, or REMOVED ENTIRELY and later
 * re-added from the editor's Add menu (2026-07-19 follow-up: full tutor
 * control, not just enable/disable) — the one thing that never changes is the
 * canonical key a present structured section carries. `prose` sections may be
 * freely added, removed, or renamed. `audience` is forward-looking plumbing
 * only (no print/handout UI consumes it yet, by design — Resolved design call
 * #3). */
export interface BlueprintSection {
  key: string;
  label: { el: string; en: string };
  description: string;
  kind: "prose" | "exercises" | "qa" | (string & {});
  weight: number;
  audience: "teacher" | "student" | "both" | (string & {});
  enabled: boolean;
}

/** The whole lesson skeleton — mirrors `app.curriculum.blueprint`'s canonical
 * shape. `version` is always `1` today; kept on the wire so a future shape bump
 * has somewhere to branch on the client without guessing. */
export interface BlueprintShape {
  version: number;
  sections: BlueprintSection[];
}

export interface BlueprintDefaultOut {
  blueprint: BlueprintShape;
  /** Whether the tutor has saved a settings default (a `blueprint_default` row
   * exists). Drives the Settings card's "you've customised this" / Restore
   * affordance — there is no history table (Resolved design call #4): Restore is
   * simply deleting the row, and the code default is the target. */
  is_override: boolean;
}

/** The RESOLVED settings default — his edit if he has one, else the code
 * default — plus whether it is a customisation. What NEW curricula are seeded
 * with, and what the wizard's "structure" step pre-fills. */
export function getBlueprintDefault(): Promise<BlueprintDefaultOut> {
  return request<BlueprintDefaultOut>("/blueprint/default");
}

/** ALWAYS the git-backed code default — never affected by a save. The Restore
 * target and the diff baseline for "what did I change from the original". */
export function getBlueprintCodeDefault(): Promise<{ blueprint: BlueprintShape }> {
  return request<{ blueprint: BlueprintShape }>("/blueprint/code-default");
}

/** Validate + save the tutor's settings default. Throws `ApiError` with `code`
 * one of `BlueprintInvalid`'s: `bad_version`, `no_sections`, `bad_key`,
 * `dup_key`, `bad_label`, `bad_description`, `bad_weight`, `bad_kind`,
 * `bad_audience`, `bad_enabled`, `structured_section_duplicate`,
 * `structured_section_renamed`, `no_enabled_sections` — each mapped to one
 * Greek sentence under `blueprint.errors.<code>`. Validation runs BEFORE the
 * write, so a rejected blueprint leaves any existing default untouched.
 *
 * A structured (`exercises`/`qa`) section may now be entirely ABSENT — full
 * delete + re-add is legal (2026-07-19 follow-up to Plan C's Task 6). What is
 * still rejected: two sections of the same structured kind
 * (`structured_section_duplicate`), a structured section present under any key
 * but its canonical one (`structured_section_renamed`), and a blueprint with no
 * ENABLED section at all (`no_enabled_sections`). */
export function saveBlueprintDefault(blueprint: BlueprintShape): Promise<BlueprintDefaultOut> {
  return request<BlueprintDefaultOut>("/blueprint/default", {
    method: "PUT",
    body: JSON.stringify({ blueprint }),
  });
}

/** Reset to the code default by deleting the row. IDEMPOTENT: resetting an
 * already-default settings is a 200 with `is_override: false`, not a 404. Never
 * re-drafts anything and never touches an existing course (Task 8's opt-in
 * "re-draft" button is the only path that rewrites lessons). */
export function resetBlueprintDefault(): Promise<BlueprintDefaultOut> {
  return request<BlueprintDefaultOut>("/blueprint/default", { method: "DELETE" });
}

/**
 * BACKUP / RESTORE (`app/routers/backup.py`) — the whole deployment (database +
 * page scans) as one `guitar-backup-YYYY-MM-DD.tar.gz`, the same format the
 * desktop seed bundle uses (`desktop/scripts/make-seed.sh`).
 */

/** The manifest that rode inside a restored archive — `{created, app_commit,
 * pg_major, schema}`, echoed back by a successful restore. */
export interface RestoreBackupOut {
  ok: boolean;
  restored_manifest: Record<string, unknown>;
}

/** The export URL for a plain anchor / `window.location` GET — NOT a `fetch`.
 * Unlike the DOCX export above (a blob small enough to buffer), a backup can be
 * hundreds of MB of page scans, and the browser/webview's own download path
 * streams it to disk with a progress UI we don't have to build. The session
 * cookie rides along: a top-level navigation GET sends SameSite=Lax cookies,
 * which is exactly what `gt_session` is. */
export function backupExportUrl(): string {
  return `${API_BASE}/backup/export`;
}

/** The same URL as a SERVER render produces it — and the ONLY thing that may be
 * put in the anchor's `href` on the first client render.
 *
 * This exists because a rendered URL and a fetched URL are not the same kind of
 * fact. A fetch happens in the browser, where `API_BASE` is right. An `href` is
 * decided while the HTML is being produced, and the first render happens on the
 * SERVER, where there is no `window`: rules 1 and 3 cannot run, so the server
 * cannot see the shell's injected base OR the origin the page will be served
 * under. React then refuses to patch a hydration attribute mismatch — so
 * whatever the server guessed is what the tutor clicks, forever.
 *
 * NOT, as this comment used to claim, because `NEXT_PUBLIC_API_BASE` is a
 * build-time constant on both sides. IT IS NOT: on the server it is a live
 * `process.env` read (`envApiBase` has the details), and a production
 * `next start` with the variable exported at run time renders that runtime value
 * straight into this href. The premise was false; the function is still right,
 * for a plainer reason.
 *
 * That reason: BOTH SIDES CALL THIS SAME FUNCTION for the one render hydration
 * compares. The server renders it, and the client's first render returns it too
 * (`backup-card.tsx` feeds it to `useSyncExternalStore` as the server snapshot),
 * so the markup matches by construction — and the anchor is a real, focusable,
 * right-clickable link from the first paint rather than an href-less stub. Only
 * once hydration is done does the client read `backupExportUrl()` and update the
 * attribute for real, which React DOES apply, because by then it is reconciling
 * against its own previous render rather than against server HTML.
 *
 * The one way the two sides can still disagree falls out of the same asymmetry:
 * a deployment that sets `NEXT_PUBLIC_API_BASE` at RUN time on a bundle built
 * without it renders one base and hydrates another, which costs one React
 * hydration warning on this attribute — and then the post-hydration swap lands
 * on the right URL anyway.
 *
 * `backup-card.tsx` is the one caller; the pre-hydration window this leaves open
 * — a click before hydration follows the SERVER's base — is argued out in full
 * on `exportUrl` there, including why nothing can close it. */
export function serverBackupExportUrl(): string {
  return `${serverApiBase()}/backup/export`;
}

/** Upload a backup archive and REPLACE ALL DATA with its contents. The server
 * answers 4xx + `code` (`not_a_backup`, `pg_major_mismatch`, `pg_tools_missing`,
 * `restore_failed`, `jobs_running`, `backup_busy`) — the card maps each to one
 * Greek sentence under `backup.errors.*`. Multipart via `request()`, which
 * deliberately does NOT set Content-Type on FormData bodies (the browser must
 * write the boundary itself). On success the caller reloads the app: every
 * screen is stale by definition after a restore. */
export function restoreBackup(file: File): Promise<RestoreBackupOut> {
  const form = new FormData();
  form.append("file", file);
  return request<RestoreBackupOut>("/backup/restore", { method: "POST", body: form });
}
