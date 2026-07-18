import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * THE SETTINGS BLUEPRINT EDITOR (Plan C, Task 6) — the tutor-editable default
 * lesson skeleton (`GET/PUT/DELETE /blueprint/default`). Every NEW curriculum is
 * seeded from this; editing it never touches a course that already exists
 * (spec invariant #3 — that guarantee lives entirely on the API side, this card
 * just reads/writes the resolved default).
 *
 * The two STRUCTURED sections (`exercises`, `qa_prompts`) are LOCKED here: no
 * Remove, no rename — only `prose` sections may be added, removed or renamed
 * (invariant #4). That is the one behavioural rule this spec exists to pin, on
 * top of the ordinary load/edit/save/restore loop every settings card has.
 *
 * The API origin is mocked wholesale, same convention as `prompts.spec.ts`: a
 * per-glob route would also swallow the `/el/settings` navigation itself, and
 * `X-App-Locale` makes every call a non-simple request, so OPTIONS needs an
 * answer or the real call never fires.
 */

const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

// --- fixture: the code default, shaped exactly like
//     `app.curriculum.blueprint.default_blueprint()` / the plan's canonical table.

interface Section {
  key: string;
  label: { el: string; en: string };
  description: string;
  weight: number;
  kind: "prose" | "exercises" | "qa";
  audience: "teacher" | "student" | "both";
  enabled: boolean;
}

function section(over: Partial<Section> & Pick<Section, "key">): Section {
  return {
    label: { el: over.key, en: over.key },
    description: `Description for ${over.key}.`,
    weight: 0.1,
    kind: "prose",
    audience: "teacher",
    enabled: true,
    ...over,
  };
}

const DEFAULT_SECTIONS: Section[] = [
  section({ key: "warm_up", label: { el: "Ζέσταμα", en: "Warm-up" }, weight: 0.07 }),
  section({ key: "theory", label: { el: "Θεωρία", en: "Theory" }, weight: 0.25 }),
  section({ key: "demonstration", label: { el: "Επίδειξη", en: "Demonstration" }, weight: 0.18 }),
  section({
    key: "exercises", label: { el: "Ασκήσεις", en: "Exercises" }, weight: 0.22,
    kind: "exercises", audience: "student",
    description: "Prose introducing and sequencing the exercises.",
  }),
  section({ key: "common_mistakes", label: { el: "Συνήθη λάθη", en: "Common mistakes" }, weight: 0.1 }),
  section({ key: "recap", label: { el: "Ανακεφαλαίωση", en: "Recap" }, weight: 0.06, audience: "student" }),
  section({ key: "homework", label: { el: "Εργασία για το σπίτι", en: "Homework" }, weight: 0.06, audience: "student" }),
  section({
    key: "qa_prompts", label: { el: "Ερωτήσεις & συζήτηση", en: "Q&A and discussion" }, weight: 0.06,
    kind: "qa", audience: "teacher",
    description: "How to open the 10-minute discussion block.",
  }),
];

function defaultBlueprint(): { version: number; sections: Section[] } {
  return { version: 1, sections: structuredClone(DEFAULT_SECTIONS) };
}

interface Call {
  method: string;
  pathname: string;
  body: unknown;
}

interface Opts {
  /** Force the PUT's response — the only way to see a server rejection here. */
  put?: { status: number; json: unknown };
  /** Seed `is_override` / the resolved blueprint GET returns, for a test that
   * starts from an already-customised settings default. */
  resolved?: { blueprint: unknown; is_override: boolean };
}

async function mockApi(page: Page, opts: Opts = {}): Promise<Call[]> {
  const calls: Call[] = [];

  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    let body: unknown = null;
    try {
      body = req.postDataJSON();
    } catch {
      body = null;
    }
    calls.push({ method, pathname, body });

    const json = (b: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(b),
      });

    if (pathname === "/settings") {
      return json({
        provider: "anthropic",
        model: "claude-sonnet-5",
        configured: true,
        key_hint: "7f2a",
      });
    }
    if (pathname === "/prompts") return json([]);

    if (pathname === "/blueprint/code-default") {
      return json({ blueprint: defaultBlueprint() });
    }
    if (pathname === "/blueprint/default") {
      if (method === "GET") {
        return json(opts.resolved ?? { blueprint: defaultBlueprint(), is_override: false });
      }
      if (method === "PUT") {
        if (opts.put) return json(opts.put.json, opts.put.status);
        const sent = (body as { blueprint: { sections: Section[] } }).blueprint;
        return json({ blueprint: sent, is_override: true });
      }
      if (method === "DELETE") {
        return json({ blueprint: defaultBlueprint(), is_override: false });
      }
    }
    return json([]);
  });

  return calls;
}

// ---------------------------------------------------------------------------

test("the card loads the code default — all 8 sections", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");

  const card = page.getByTestId("blueprint-default-card");
  await expect(card).toBeVisible();
  for (const s of DEFAULT_SECTIONS) {
    await expect(card.getByTestId(`blueprint-section-${s.key}`)).toBeVisible();
  }
});

test("editing a prose description and Save sends the mutated blueprint", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  const description = card.getByTestId("blueprint-description-warm_up");
  await description.fill("A brand-new warm-up description, his own words.");
  await card.getByTestId("blueprint-save").click();

  await expect(card.getByTestId("blueprint-saved")).toBeVisible();
  const put = calls.find((c) => c.method === "PUT" && c.pathname === "/blueprint/default");
  expect(put).toBeTruthy();
  const sentSections = (put!.body as { blueprint: { sections: Section[] } }).blueprint.sections;
  const warmUp = sentSections.find((s) => s.key === "warm_up");
  expect(warmUp?.description).toBe("A brand-new warm-up description, his own words.");
  expect(sentSections).toHaveLength(8);
});

test("Add section then Save sends nine sections", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  await card.getByTestId("blueprint-add-section").click();
  await card.getByTestId("blueprint-save").click();

  await expect(card.getByTestId("blueprint-saved")).toBeVisible();
  const put = calls.find((c) => c.method === "PUT" && c.pathname === "/blueprint/default");
  const sentSections = (put!.body as { blueprint: { sections: Section[] } }).blueprint.sections;
  expect(sentSections).toHaveLength(9);
});

test("the structured `exercises` row has no Remove button and a read-only key", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  const row = card.getByTestId("blueprint-section-exercises");
  await expect(row.getByTestId("blueprint-remove-exercises")).toHaveCount(0);
  await expect(row.getByTestId("blueprint-key-exercises")).toBeDisabled();
  await expect(row.getByTestId("blueprint-locked-exercises")).toBeVisible();
});

test("the structured `qa_prompts` row has no Remove button and a read-only key", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  const row = card.getByTestId("blueprint-section-qa_prompts");
  await expect(row.getByTestId("blueprint-remove-qa_prompts")).toHaveCount(0);
  await expect(row.getByTestId("blueprint-key-qa_prompts")).toBeDisabled();
});

test("a prose row has a working Remove button", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  await expect(card.getByTestId("blueprint-section-recap")).toBeVisible();
  await card.getByTestId("blueprint-remove-recap").click();
  await expect(card.getByTestId("blueprint-section-recap")).toHaveCount(0);
});

test("Restore opens the confirm modal and, on accept, issues DELETE", async ({ page }) => {
  const calls = await mockApi(page, { resolved: { blueprint: defaultBlueprint(), is_override: true } });
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  await card.getByTestId("blueprint-restore").click();
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);

  await page.getByTestId("confirm-accept").click();
  await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
  expect(calls.some((c) => c.method === "DELETE" && c.pathname === "/blueprint/default")).toBe(true);
});

test("Restore, cancelled, issues no DELETE at all", async ({ page }) => {
  const calls = await mockApi(page, { resolved: { blueprint: defaultBlueprint(), is_override: true } });
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  await card.getByTestId("blueprint-restore").click();
  await page.getByTestId("confirm-cancel").click();

  await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);
});

test("a rejected save renders exactly one Greek sentence, never a raw code", async ({ page }) => {
  const calls = await mockApi(page, {
    put: { status: 422, json: { detail: { code: "structured_section_renamed" } } },
  });
  await page.goto("/el/settings");
  const card = page.getByTestId("blueprint-default-card");

  await card.getByTestId("blueprint-description-warm_up").fill("anything, to make it dirty");
  await card.getByTestId("blueprint-save").click();

  const error = card.getByTestId("blueprint-error");
  await expect(error).toBeVisible();
  await expect(error).not.toContainText("structured_section_renamed");
  await expect(error).not.toContainText("422");
  expect(calls.some((c) => c.method === "PUT")).toBe(true);
});

test("every blueprint.* string exists in BOTH Greek and English", () => {
  const read = (locale: string) =>
    JSON.parse(readFileSync(join(__dirname, `../src/messages/${locale}.json`), "utf8"));

  const flatten = (obj: Record<string, unknown>, prefix = ""): string[] =>
    Object.entries(obj).flatMap(([k, v]) =>
      v && typeof v === "object"
        ? flatten(v as Record<string, unknown>, `${prefix}${k}.`)
        : [`${prefix}${k}`],
    );

  const el = flatten(read("el").blueprint ?? {}).sort();
  const en = flatten(read("en").blueprint ?? {}).sort();
  expect(el.length).toBeGreaterThan(0);
  expect(el).toEqual(en);
});
