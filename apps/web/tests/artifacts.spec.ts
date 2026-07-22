import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the real Artifacts gallery (Plan 4 Task
// 5): `/en/artifacts` used to be a static demo route with fixed in-file
// specs and zero API calls (Task 2/3) — it's now a real gallery (create via
// generate + list existing), so every `/artifacts/*` call is intercepted via
// `page.route` and answered from an in-memory fixture, same convention as
// `knowledge.spec.ts`/`cockpit.spec.ts` (CORS headers + explicit OPTIONS
// handling + an "unexpected request -> 500" catch-all so a routing mistake
// fails loudly instead of leaking to a real backend).
//
// Route matching uses a PREDICATE function, not a glob string like
// `knowledge.spec.ts`'s `**/knowledge/**` — deliberately: every `/knowledge/*`
// path has a segment after "knowledge/", so a bare suffix glob works there,
// but this API's list/create endpoint is the BARE path `/artifacts` (no
// trailing segment — filters are query params: `?block_id=&kind=`), and
// `cockpit.spec.ts` already flags the general risk of a suffix glob
// colliding with this app's own `/en/artifacts` page URL (same word,
// "artifacts"). A predicate keyed on `url.origin`+`url.pathname` sidesteps
// glob-escaping edge cases entirely (a bare, no-wildcard glob would only
// match a request whose FULL url is byte-for-byte identical — never true
// once `?block_id=...` is appended) and is unambiguous to read.
const API_ORIGIN = "http://localhost:8791";

// `Access-Control-Allow-Origin` must echo the app's real origin (and pair
// with `Allow-Credentials`) rather than "*": since the auth slice made every
// call in `lib/api.ts` `credentials: "include"`, a browser REJECTS a
// wildcard-ACAO response outright — the page then renders its "could not
// load" error and every assertion below it fails for a reason that has
// nothing to do with what the test is checking.
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

interface FixtureArtifact {
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

function makeArtifact(
  overrides: Partial<FixtureArtifact> & Pick<FixtureArtifact, "kind" | "spec" | "title">,
): FixtureArtifact {
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    tags: [],
    source: "ai",
    block_id: null,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

// Real, previously-live-verified specs (Task 2/3's own demo constants) reused
// as canned "generate"/"list" fixture data — good, already-validated shapes,
// and reusing the exact G-major chord + SRV tone recipe keeps this suite's
// assertions directly comparable to the real e2e run (task report).
const CHORD_SPEC = {
  name: "G",
  frets: [3, 2, 0, 0, 0, 3],
  fingers: [3, 2, 0, 0, 0, 4],
  barres: [],
  baseFret: 1,
};

const TONE_SPEC = {
  artist: "Stevie Ray Vaughan",
  song: "Texas Flood",
  guitar: '1963 Fender Stratocaster ("Number One"), heavy-gauge strings, tuned a half-step down',
  amp: "Blackface-era Fender (Vibroverb/Super Reverb) pushed loud, into natural tube breakup",
  drive: "Ibanez Tube Screamer (TS808) run as a clean-ish boost ahead of the amp",
  chain: "Strat -> Vox wah (occasional) -> Tube Screamer (boost) -> cranked tube amp",
  hands: "Heavy pick attack, wide aggressive vibrato, big string bends, thumb-over chord/rhythm grip",
  listen: ["Texas Flood (1983)", "Pride and Joy", "Couldn't Stand the Weather"],
};

const SCALE_SPEC = {
  name: "A Minor Pentatonic — Box 1",
  root: "A",
  positions: [
    { string: 6, fret: 5, degree: "1" },
    { string: 5, fret: 5, degree: "4" },
    { string: 4, fret: 5, degree: "b7" },
  ],
};

const TAB_SPEC = {
  title: "E Minor Pentatonic Lick",
  alphaTex: `
\\tempo 90
.
:8 0.6 3.6 0.5 2.5 0.4 2.4 0.3 2.3 |
2.3 0.3 2.4 0.4 2.5 0.5 3.6 0.6.2
`,
};

function fillerSpec(i: number) {
  return { nodes: [{ label: `Node ${i}` }, { label: "Amp" }] };
}

/** Mirrors `derive_title` (`app.artifacts.generate`) closely enough for
 * realistic mock titles: name/title field first, then tone_recipe's
 * artist/song join, else the prompt itself. */
function titleForGenerated(kind: string, spec: Record<string, unknown>, prompt: string): string {
  const nameLike = spec.name ?? spec.title;
  if (typeof nameLike === "string" && nameLike) return nameLike;
  if (kind === "tone_recipe") {
    const label = [spec.artist, spec.song].filter((v): v is string => typeof v === "string" && !!v).join(" — ");
    if (label) return label;
  }
  return prompt;
}

const GENERATED_SPEC_BY_KIND: Record<string, Record<string, unknown>> = {
  chord_diagram: CHORD_SPEC,
  tone_recipe: TONE_SPEC,
};

/** Wires up an in-memory mock of the Artifacts API (`/artifacts`,
 * `/artifacts/generate`, `/artifacts/{id}`) — see this file's top docstring
 * for why matching is done via a predicate, not a glob string. Returns the
 * mutable fixture state plus call counts / last request bodies / the last
 * `GET /artifacts` query string seen, so tests can assert both the rendered
 * UI *and* that the API was called with the right payload. */
async function mockArtifactsApi(
  page: Page,
  { initial = [] as FixtureArtifact[], generateDelayMs = 0 } = {},
) {
  const artifacts = [...initial];
  const calls = { list: 0, create: 0, generate: 0, delete: 0 };
  const lastBody: { create?: unknown; generate?: unknown } = {};
  let lastListQuery: URLSearchParams | null = null;
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const url = new URL(req.url());
    const { pathname } = url;

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/artifacts" && method === "GET") {
      calls.list++;
      lastListQuery = url.searchParams;
      const blockId = url.searchParams.get("block_id");
      const kind = url.searchParams.get("kind");
      let results = artifacts;
      if (blockId) results = results.filter((a) => a.block_id === blockId);
      if (kind) results = results.filter((a) => a.kind === kind);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(results),
      });
      return;
    }

    if (pathname === "/artifacts" && method === "POST") {
      calls.create++;
      const payload = req.postDataJSON() as {
        kind: string;
        spec: Record<string, unknown>;
        title?: string | null;
        tags?: string[];
        block_id?: string | null;
      };
      lastBody.create = payload;
      const created = makeArtifact({
        kind: payload.kind,
        spec: payload.spec,
        title: payload.title ?? titleForGenerated(payload.kind, payload.spec, "artifact"),
        tags: payload.tags ?? [],
        block_id: payload.block_id ?? null,
      });
      artifacts.unshift(created);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
      });
      return;
    }

    if (pathname === "/artifacts/generate" && method === "POST") {
      calls.generate++;
      const payload = req.postDataJSON() as {
        kind: string;
        prompt: string;
        block_id?: string | null;
        ground?: boolean;
      };
      lastBody.generate = payload;
      const spec = GENERATED_SPEC_BY_KIND[payload.kind] ?? { name: payload.prompt };
      const created = makeArtifact({
        kind: payload.kind,
        spec,
        title: titleForGenerated(payload.kind, spec, payload.prompt),
        block_id: payload.block_id ?? null,
      });
      artifacts.unshift(created);
      if (generateDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, generateDelayMs));
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
      });
      return;
    }

    const idMatch = pathname.match(/^\/artifacts\/([^/]+)$/);
    if (idMatch && method === "DELETE") {
      calls.delete++;
      const idx = artifacts.findIndex((a) => a.id === idMatch[1]);
      if (idx >= 0) artifacts.splice(idx, 1);
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    if (idMatch && method === "GET") {
      const found = artifacts.find((a) => a.id === idMatch[1]);
      await route.fulfill({
        status: found ? 200 : 404,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(found ?? { detail: "not found" }),
      });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(
    (url) => url.origin === API_ORIGIN && (url.pathname === "/artifacts" || url.pathname.startsWith("/artifacts/")),
    handler,
  );

  return {
    artifacts,
    calls,
    lastBody,
    get lastListQuery() {
      return lastListQuery;
    },
    unexpected,
  };
}

interface FixtureBlockNode {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  est_minutes: number | null;
  order: number;
  language: string;
  plane: string;
  student_id: string | null;
  meta: Record<string, unknown> | null;
  /** EMBEDDED. `block_to_tree` now serves the whole tree's artifacts from one
   * `WHERE block_id IN (...)`, so the board fetches nothing per leaf. */
  artifacts: unknown[];
  children: FixtureBlockNode[];
}

/** course -> module -> lesson -> segment, with the segment's id pinned to
 * `segmentId` (a caller-chosen, known-ahead-of-time UUID) so a test can seed a
 * matching artifact's `block_id` before the tree exists — and with that artifact
 * EMBEDDED on the segment, which is how it now arrives. */
function makeTreeWithSegment(
  segmentId: string,
  title: string,
  language: string,
  segmentArtifacts: unknown[] = [],
): FixtureBlockNode {
  return {
    id: randomUUID(), kind: "course", title, body: null, est_minutes: null, order: 0,
    language, plane: "content", student_id: null, meta: null, artifacts: [],
    children: [
      {
        id: randomUUID(), kind: "module", title: "Open Chords", body: null,
        est_minutes: null, order: 0, language, plane: "content", student_id: null,
        meta: { tier: "library", coverage_note: "Your book, p. 12." }, artifacts: [],
        children: [
          {
            id: randomUUID(), kind: "lesson", title: "G Major",
            body: "Learn the open G major chord.", est_minutes: 15, order: 0,
            language, plane: "content", student_id: null,
            meta: { draft_status: "ready", word_count: 2200 }, artifacts: [],
            children: [
              {
                id: segmentId, kind: "segment", title: "Fretting G major",
                body: "Practice fretting the G shape cleanly.", est_minutes: 5, order: 0,
                language, plane: "content", student_id: null, meta: null,
                artifacts: segmentArtifacts,
                children: [],
              },
            ],
          },
        ],
      },
    ],
  };
}

/** Just enough `/curricula` to put ONE known tree on the board — the template list
 * plus the tree itself plus its progress poll.
 *
 * It no longer drives the interview to get there, and it no longer mocks
 * `/jobs/{id}`. Both were scaffolding around a flow that does not exist any more:
 * the interview materializes the tree at CONFIRM, and the board opens on it
 * directly. Clicking the template is the same board, in two lines instead of ten.
 *
 * The segment's artifact is EMBEDDED in the tree (`artifacts: [...]`), which is the
 * whole point of the test below: the board must not fetch it. */
async function mockCurriculaForSegmentTest(page: Page, tree: FixtureBlockNode) {
  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (pathname === "/curricula" && method === "GET") {
      return json([
        {
          id: tree.id, title: tree.title, language: tree.language,
          target_profile: null, created_at: new Date().toISOString(),
        },
      ]);
    }
    if (pathname === `/curricula/${tree.id}/progress` && method === "GET") {
      return json({
        root_id: tree.id, total: 1, queued: 0, drafting: 0, ready: 1, failed: 0, done: true,
      });
    }
    const getMatch = pathname.match(/^\/curricula\/([^/]+)$/);
    if (getMatch && method === "GET") {
      return getMatch[1] === tree.id ? json(tree) : json({ detail: "not found" }, 404);
    }
    return json({ detail: "unmocked request in test" }, 500);
  }

  await page.route(
    (url) =>
      url.origin === API_ORIGIN &&
      (url.pathname === "/curricula" || url.pathname.startsWith("/curricula/")),
    handler,
  );
}

test.describe("artifacts gallery (mocked API)", () => {
  test("lists existing artifacts on load", async ({ page }) => {
    const seedChain = makeArtifact({ kind: "signal_chain", spec: fillerSpec(0), title: "My Chain" });
    const seedScale = makeArtifact({ kind: "scale_diagram", spec: SCALE_SPEC, title: "A Minor Pentatonic — Box 1" });
    const mock = await mockArtifactsApi(page, { initial: [seedScale, seedChain] });

    await page.goto("/en/artifacts");
    await expect(page.getByTestId("artifacts-heading")).toBeVisible();
    await expect(page.getByTestId("artifact-list")).toBeVisible();
    await expect(page.getByTestId("artifact-item")).toHaveCount(2);
    await expect(page.getByTestId("signal-chain")).toBeVisible();
    await expect(page.getByTestId("scale-diagram-name")).toHaveText(/A Minor Pentatonic/);

    expect(mock.calls.list).toBeGreaterThanOrEqual(1);
    expect(mock.unexpected).toEqual([]);
  });

  test("generates a chord diagram and a grounded tone recipe via the create form, rendering live with correct payloads", async ({
    page,
  }) => {
    const mock = await mockArtifactsApi(page, { generateDelayMs: 400 });
    await page.goto("/en/artifacts");
    await expect(page.getByTestId("artifacts-empty")).toBeVisible();

    // 1) A G-major chord diagram — the default-selected kind.
    await page.getByTestId("artifact-kind-chord_diagram").click();
    await page.getByTestId("artifact-generate-prompt").fill("G major open chord");
    await page.getByTestId("artifact-generate-submit").click();

    // Non-frozen loading state while the (deliberately delayed) mock request
    // is in flight — same requirement `generate-dialog.tsx`'s own test holds
    // it to, just via a simple inline spinner instead of a blocking dialog.
    await expect(page.getByTestId("artifact-generate-loading")).toBeVisible();
    await expect(page.getByTestId("artifact-generate-submit")).toBeDisabled();
    await expect(page.getByTestId("artifact-generate-prompt")).toBeDisabled();

    await expect(page.getByTestId("chord-diagram-name")).toHaveText("G", { timeout: 10_000 });
    expect(mock.calls.generate).toBe(1);
    expect(mock.lastBody.generate).toEqual({
      kind: "chord_diagram",
      prompt: "G major open chord",
      block_id: null,
      ground: false,
    });

    // 2) A grounded Stevie Ray Vaughan tone recipe.
    await page.getByTestId("artifact-kind-tone_recipe").click();
    await page.getByTestId("artifact-generate-prompt").fill("Stevie Ray Vaughan Texas Flood");
    await page.getByTestId("artifact-generate-ground").click();
    await page.getByTestId("artifact-generate-submit").click();

    await expect(page.getByTestId("tone-recipe-title")).toHaveText("Texas Flood", { timeout: 10_000 });
    await expect(page.getByTestId("tone-recipe-amp")).toContainText("Blackface-era Fender");
    expect(mock.calls.generate).toBe(2);
    expect(mock.lastBody.generate).toEqual({
      kind: "tone_recipe",
      prompt: "Stevie Ray Vaughan Texas Flood",
      block_id: null,
      ground: true,
    });

    // Both generated artifacts are now in the gallery (newest-first).
    await expect(page.getByTestId("artifact-item")).toHaveCount(2);
    expect(mock.unexpected).toEqual([]);
  });

  test("deletes an artifact from the gallery", async ({ page }) => {
    const seed = makeArtifact({ kind: "signal_chain", spec: fillerSpec(0), title: "Delete Me" });
    const mock = await mockArtifactsApi(page, { initial: [seed] });

    await page.goto("/en/artifacts");
    const item = page.getByTestId("artifact-item").filter({ hasText: "Delete Me" });
    await expect(item).toBeVisible();

    await item.getByTestId("artifact-delete").click();
    // Every destructive action in this app is now guarded (see confirm.spec.ts):
    // the DELETE only leaves the browser after the tutor accepts the dialog.
    await page.getByTestId("confirm-accept").click();
    await expect(page.getByTestId("artifact-item").filter({ hasText: "Delete Me" })).toHaveCount(0);
    await expect(page.getByTestId("artifacts-empty")).toBeVisible();

    expect(mock.calls.delete).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });

  test("lazy-mounts the tab renderer only once scrolled into view, with no third-party requests", async ({
    page,
    baseURL,
  }) => {
    // A short viewport makes "below the fold" deterministic regardless of
    // exact card heights — everything past the first row or two is
    // guaranteed off-screen (and beyond LazyTabView's 300px rootMargin).
    await page.setViewportSize({ width: 800, height: 400 });

    const filler = Array.from({ length: 16 }, (_, i) =>
      makeArtifact({ kind: "signal_chain", spec: fillerSpec(i), title: `Filler ${i}` }),
    );
    const tabArtifact = makeArtifact({ kind: "tab", spec: TAB_SPEC, title: "E Minor Pentatonic Lick" });
    await mockArtifactsApi(page, { initial: [...filler, tabArtifact] });

    // Same offline/no-CDN check Task 3's suite made (tab-view.tsx must never
    // fetch its font/soundfont/script from a third-party origin) — refined
    // here to also allow this app's OWN cross-origin API calls (this gallery
    // legitimately talks to `API_ORIGIN`, unlike the old static demo page,
    // which made no API calls at all), so this only flags a genuine
    // third-party/CDN request, not the mocked same-app API traffic.
    const baseOrigin = new URL(baseURL!).origin;
    const thirdPartyRequests: string[] = [];
    page.on("request", (request) => {
      const origin = new URL(request.url()).origin;
      if (origin !== baseOrigin && origin !== API_ORIGIN) thirdPartyRequests.push(request.url());
    });

    await page.goto("/en/artifacts");
    await expect(page.getByTestId("artifact-item")).toHaveCount(filler.length + 1);

    // Below the fold at load -> the lazy placeholder shows, AlphaTab's own
    // notation container doesn't exist in the DOM at all yet (not just
    // "not visible" — it genuinely hasn't mounted, proving the gate works).
    const pending = page.getByTestId("tab-view-pending");
    await expect(pending).toBeVisible();
    await expect(page.getByTestId("tab-view-notation")).toHaveCount(0);

    await pending.scrollIntoViewIfNeeded();

    await expect(page.getByTestId("tab-view-notation").locator("svg").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("tab-view-play")).toBeVisible();
    await expect(page.getByTestId("tab-view-pending")).toHaveCount(0);

    expect(thirdPartyRequests).toEqual([]);
  });

  test("the Artifacts nav link routes to the gallery", async ({ page }) => {
    await mockArtifactsApi(page);
    await page.goto("/en/today");
    await page.getByTestId("nav-artifacts").click();
    await expect(page).toHaveURL(/\/en\/artifacts$/);
    await expect(page.getByTestId("artifacts-heading")).toBeVisible();
    await expect(page.getByTestId("artifacts-empty")).toBeVisible();
  });
});

test.describe("curriculum board segment artifacts (mocked API)", () => {
  test("a segment's artifact arrives EMBEDDED in the tree — the board fetches none — and it can attach another", async ({
    page,
  }) => {
    const segmentId = randomUUID();
    const preAttached = makeArtifact({ kind: "chord_diagram", spec: CHORD_SPEC, title: "G", block_id: segmentId });
    const tree = makeTreeWithSegment(segmentId, "Open Chords Basics", "en", [preAttached]);

    const artifactsMock = await mockArtifactsApi(page, { initial: [preAttached] });
    await mockCurriculaForSegmentTest(page, tree);

    await page.goto("/en/curricula");
    await page.getByTestId("template-item").click();
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // Only the course ROOT is expanded on load (`block-card.tsx`'s
    // `useState(isRoot)` — the 2843-node board redesign). The segment does not
    // exist in the DOM until its module and lesson are opened, so walk down.
    await page.locator('[data-testid="block-card"][data-kind="module"]').first()
      .getByTestId("block-card-header").first().click();
    await page.locator('[data-testid="block-card"][data-kind="lesson"]').first()
      .getByTestId("block-card-header").first().click();

    // The segment shows its chord diagram with NO request of its own. Every segment
    // leaf used to fire `GET /artifacts?block_id=` on mount — ~120 in parallel on a
    // real curriculum, which IS the "Could not load attached artifacts" error.
    await expect(page.getByTestId("segment-artifact-list")).toBeVisible();
    await expect(page.getByTestId("chord-diagram-name")).toHaveText("G");
    expect(artifactsMock.calls.list).toBe(0);
    expect(artifactsMock.lastListQuery).toBeNull();

    // Attaching a NEW one still works, through the segment's own dialog.
    await page.getByTestId("block-card-attach-artifact").click();
    await expect(page.getByTestId("attach-artifact-dialog")).toBeVisible();
    await page.getByTestId("attach-artifact-kind-tone_recipe").click();
    await page.getByTestId("attach-artifact-prompt").fill("Stevie Ray Vaughan Texas Flood");
    await page.getByTestId("attach-artifact-ground").click();
    await page.getByTestId("attach-artifact-submit").click();
    await expect(page.getByTestId("attach-artifact-dialog")).toBeHidden();

    await expect(page.getByTestId("tone-recipe-title")).toHaveText("Texas Flood");
    await expect(page.getByTestId("segment-artifact-item")).toHaveCount(2);

    expect(artifactsMock.calls.generate).toBe(1);
    expect(artifactsMock.lastBody.generate).toEqual({
      kind: "tone_recipe",
      prompt: "Stevie Ray Vaughan Texas Flood",
      block_id: segmentId,
      ground: true,
    });

    expect(artifactsMock.unexpected).toEqual([]);
  });
});
