import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * THE PROMPTS CARD — the surface Chris asked for first and waited for longest:
 * *"They will actually be for the teacher, but we cant degrade their quality so
 * the teacher understands them better. He wont tweak them himself, but he just
 * needs to watch them."*
 *
 * So what this file pins is READING, not editing. The load-bearing assertions
 * are that the English arrives VERBATIM (`chat.system` still says "Never write
 * tablature as plain text" — not a friendlier Greek paraphrase of it), that an
 * interpolated variable renders as a labelled chip CONTAINING its sample value
 * rather than as a hole where the student brief goes, and that a prompt with no
 * editable slice has no textarea and says why. Editing is one textarea in the
 * whole app (`student.pitch`), and its tests are the footnote they should be.
 *
 * The API origin is mocked wholesale rather than per-glob, for two reasons the
 * hard way: `page.route("**\/settings")` would also swallow the page navigation
 * to `/el/settings` itself, and `X-App-Locale` makes every call a non-simple
 * request, so the OPTIONS preflight has to be answered or the real call is never
 * sent (see `locale.spec.ts`, which learned this first).
 */

const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

// --- fixtures: shaped exactly like `app/routers/prompts.py`'s response models,
//     with text lifted from the real prompts so a paraphrase would fail here.

const CHAT_SYSTEM_TEXT =
  "You are the guitar tutor's copilot. You do NOT have his students, curricula, " +
  "or library memorized — search first, and never state anything you did not " +
  "read in a tool result. Never invent a citation. Never write tablature as " +
  "plain text.";

const PITCH_DEFAULT =
  "Write for THIS student: pitch the explanations at his level, and where his " +
  "notes say he is stuck, address it directly instead of teaching past it.";

const BRIEF_STUDENT = "Γιάννης, 14, beginner";
const BRIEF_TEXT =
  `Student brief:\n${BRIEF_STUDENT}\nNotes: keeps muting the B string on the D chord.\n\n` +
  PITCH_DEFAULT;

const CURRICULUM_SYSTEM_TEXT =
  "You are writing for a real guitar teacher. You will read his entire library, " +
  "then be given one specific job. Answer with the requested data only.";

const summary = (over: Record<string, unknown>) => ({
  kind: "prompt",
  provider: null,
  cache_prefix: false,
  cache_cost_warning: false,
  has_override: false,
  ...over,
});

const CHAT_SYSTEM = summary({
  id: "chat.system",
  flow: "chat",
  title_el: "Ο βοηθός συνομιλίας",
  what_it_does_el: "Λέει στον βοηθό ότι δεν ξέρει τίποτα από μόνος του για τους μαθητές σου.",
  when_it_runs_el: "Σε κάθε μήνυμα που γράφεις στη συνομιλία.",
});

const TOOLS_DESCRIPTIONS = summary({
  id: "tools.descriptions",
  flow: "tools",
  kind: "fragment",
  title_el: "Τα εργαλεία του βοηθού",
  what_it_does_el: "Περιγράφει στον βοηθό τι μπορεί να κάνει.",
  when_it_runs_el: "Σε κάθε μήνυμα στη συνομιλία.",
});

const TOOLS_CLAUDE_CLI = summary({
  id: "tools.system_claude_cli",
  flow: "tools",
  title_el: "Τα εργαλεία, γραμμένα σαν οδηγίες (τρέχουσα σύνδεση)",
  what_it_does_el: "Με τη σύνδεση που χρησιμοποιείς τώρα, τα εργαλεία γράφονται σε απλό κείμενο.",
  when_it_runs_el: "Σε κάθε μήνυμα, όσο μιλάει μέσω της τοπικής γέφυρας.",
  provider: "claude_cli",
});

const CURRICULUM_SYSTEM = summary({
  id: "curriculum.system",
  flow: "curriculum",
  title_el: "Η εισαγωγή για τη συγγραφή ύλης",
  what_it_does_el: "Η πρώτη οδηγία σε κάθε δημιουργία ύλης.",
  when_it_runs_el: "Κάθε φορά που φτιάχνεις πρόγραμμα σπουδών.",
  cache_prefix: true,
  cache_cost_warning: true,
});

const STUDENT_BRIEF = summary({
  id: "shared.student_brief",
  flow: "shared",
  kind: "fragment",
  title_el: "Το προφίλ του μαθητή",
  what_it_does_el: "Λέει στον βοηθό ποιος είναι ο μαθητής και πού κολλάει.",
  when_it_runs_el: "Κάθε φορά που γράφεται μάθημα για συγκεκριμένο μαθητή.",
});

const PROMPTS = [CHAT_SYSTEM, TOOLS_DESCRIPTIONS, TOOLS_CLAUDE_CLI, CURRICULUM_SYSTEM, STUDENT_BRIEF];

const pitchSlice = (over: Record<string, unknown> = {}) => ({
  id: "student.pitch",
  prompt_id: "shared.student_brief",
  label_el: "Πώς να απευθύνεται στον μαθητή",
  kind: "replace",
  default: PITCH_DEFAULT,
  effective: PITCH_DEFAULT,
  has_override: false,
  max_chars: 2000,
  cache_cost_warning: false,
  ...over,
});

const DETAILS: Record<string, Record<string, unknown>> = {
  "chat.system": {
    ...CHAT_SYSTEM,
    source_ref: "app/agent/prompts.py:67",
    text: CHAT_SYSTEM_TEXT,
    messages: [{ role: "system", content: CHAT_SYSTEM_TEXT, cached: false }],
    spans: [],
    slices: [],
  },
  "tools.system_claude_cli": {
    ...TOOLS_CLAUDE_CLI,
    source_ref: "app/llm/claude_cli.py:543",
    text: "You have access to the following tools.",
    messages: [{ role: "system", content: "You have access to the following tools.", cached: false }],
    spans: [],
    slices: [],
  },
  "curriculum.system": {
    ...CURRICULUM_SYSTEM,
    source_ref: "app/curriculum/corpus.py:242",
    text: CURRICULUM_SYSTEM_TEXT,
    messages: [{ role: "system", content: CURRICULUM_SYSTEM_TEXT, cached: true }],
    spans: [],
    slices: [],
  },
  "shared.student_brief": {
    ...STUDENT_BRIEF,
    source_ref: "app/students/context.py:88",
    text: BRIEF_TEXT,
    messages: [{ role: "user", content: BRIEF_TEXT, cached: false }],
    spans: [
      {
        name: "student",
        label_el: "Ο μαθητής",
        value: BRIEF_STUDENT,
        start: BRIEF_TEXT.indexOf(BRIEF_STUDENT),
        end: BRIEF_TEXT.indexOf(BRIEF_STUDENT) + BRIEF_STUDENT.length,
      },
      {
        name: "pitch",
        label_el: "Πώς να απευθύνεται στον μαθητή",
        value: PITCH_DEFAULT,
        start: BRIEF_TEXT.indexOf(PITCH_DEFAULT),
        end: BRIEF_TEXT.indexOf(PITCH_DEFAULT) + PITCH_DEFAULT.length,
      },
    ],
    slices: [pitchSlice()],
  },
};

interface Call {
  method: string;
  pathname: string;
  body: unknown;
}

interface Opts {
  provider?: string;
  prompts?: unknown[];
  details?: Record<string, Record<string, unknown>>;
  /** Force the PUT's answer — the only way to see a server rejection from here. */
  put?: { status: number; json: unknown };
  history?: unknown[];
}

async function mockApi(page: Page, opts: Opts = {}): Promise<Call[]> {
  const calls: Call[] = [];
  const details = opts.details ?? DETAILS;

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
        provider: opts.provider ?? "anthropic",
        model: "claude-sonnet-5",
        configured: true,
        key_hint: "7f2a",
      });
    }
    if (pathname === "/prompts") return json(opts.prompts ?? PROMPTS);

    if (pathname.startsWith("/prompts/slices/")) {
      const rest = pathname.slice("/prompts/slices/".length);
      if (rest.endsWith("/history")) return json(opts.history ?? []);
      if (method === "PUT") {
        if (opts.put) return json(opts.put.json, opts.put.status);
        return json(pitchSlice({ effective: (body as { text: string }).text, has_override: true }));
      }
      if (method === "DELETE") return json(pitchSlice());
    }

    if (pathname.startsWith("/prompts/")) {
      const id = decodeURIComponent(pathname.slice("/prompts/".length));
      const detail = details[id];
      if (!detail) return json({ detail: { code: "unknown_prompt", message: "no" } }, 404);
      return json(detail);
    }
    return json([]);
  });

  return calls;
}

/** Opens the flow group, then the prompt inside it — the two clicks the tutor
 * makes, kept in one place so a change of shape breaks one function. */
async function open(page: Page, flow: string, promptId: string) {
  await page.getByTestId(`prompt-flow-${flow}`).click();
  await page.getByTestId(`prompt-toggle-${promptId}`).click();
  return page.getByTestId(`prompt-${promptId}`);
}

// --- reading: the feature ------------------------------------------------

test("groups the prompts by flow, and opens with all of them shut", async ({ page }) => {
  // 31 prompts is a wall of text for a man who is a beginner with computers.
  await mockApi(page);
  await page.goto("/el/settings");

  await expect(page.getByTestId("settings-prompts-card")).toBeVisible();
  await expect(page.getByTestId("prompt-flow-chat")).toBeVisible();
  await expect(page.getByTestId("prompt-flow-curriculum")).toBeVisible();
  await expect(page.getByTestId("prompt-flow-shared")).toBeVisible();

  // Nothing English is on screen until he asks for it.
  await expect(page.getByTestId("prompt-toggle-chat.system")).not.toBeVisible();
  await expect(page.getByText("You are the guitar tutor's copilot")).not.toBeVisible();
});

test("the flow groups are named in Greek, never by their code id", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  await expect(page.getByTestId("prompt-flow-chat")).toContainText("Συνομιλία");
  await expect(page.getByTestId("prompt-flow-curriculum")).toContainText("ύλη");
});

test("opening a prompt shows the English VERBATIM, with the Greek beside it", async ({ page }) => {
  // The whole spec in one test: we annotate, we never rewrite. The English is
  // the text the model gets, so it is the text he reads.
  await mockApi(page);
  await page.goto("/el/settings");
  const card = await open(page, "chat", "chat.system");

  await expect(card).toContainText("Ο βοηθός συνομιλίας");
  await expect(card).toContainText("Λέει στον βοηθό ότι δεν ξέρει τίποτα");
  await expect(card).toContainText("Σε κάθε μήνυμα που γράφεις στη συνομιλία.");

  const text = card.getByTestId("prompt-text-chat.system");
  await expect(text).toContainText("You are the guitar tutor's copilot.");
  await expect(text).toContainText("Never write tablature as plain text.");
  await expect(text).toContainText("Never invent a citation.");
});

test("the verbatim text is selectable but not editable, and names its source file", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  const card = await open(page, "chat", "chat.system");

  // He may want to paste it somewhere; he may not type into it.
  await expect(card.getByTestId("prompt-text-chat.system")).toHaveAttribute("data-selectable", "true");
  await expect(card.locator("textarea")).toHaveCount(0);
  await expect(card).toContainText("app/agent/prompts.py:67");
});

test("a prompt with nothing editable says WHY, in Greek, and reads as engineering", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  const card = await open(page, "chat", "chat.system");

  const locked = card.getByTestId("prompt-locked-chat.system");
  await expect(locked).toBeVisible();
  // "a textarea is the wrong instrument for editing a guard" — not "you may not".
  await expect(locked).toContainText("κώδικα");
  await expect(locked).not.toContainText("prompts.");
});

test("an interpolated variable is a labelled chip holding its sample value — not a hole", async ({ page }) => {
  // He must see WHERE the student brief goes, and what it looks like when it
  // gets there.
  await mockApi(page);
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  const chip = card.getByTestId("prompt-span-student");
  await expect(chip).toBeVisible();
  await expect(chip).toContainText(BRIEF_STUDENT);
  await expect(chip).toContainText("Ο μαθητής");

  // …and the prompt around it is still whole.
  const text = card.getByTestId("prompt-text-shared.student_brief");
  await expect(text).toContainText("Notes: keeps muting the B string on the D chord.");
  await expect(text).toContainText(PITCH_DEFAULT);
});

test("a prompt inside the cached prefix says so, without inventing a price", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");
  const card = await open(page, "curriculum", "curriculum.system");

  const note = card.getByTestId("prompt-cache-curriculum.system");
  await expect(note).toBeVisible();
  await expect(note).toContainText("μία φορά");
  // P2 carries no dollar figure on purpose: an honest number needs a corpus
  // scan. A made-up one is worse than none.
  await expect(note).not.toContainText("$");
});

test("a prompt only the OTHER connection sends is not listed at all", async ({ page }) => {
  // The viewer must show what the ACTIVE provider sends, not an idealised one.
  await mockApi(page, { provider: "anthropic" });
  await page.goto("/el/settings");
  await page.getByTestId("prompt-flow-tools").click();

  await expect(page.getByTestId("prompt-toggle-tools.descriptions")).toBeVisible();
  await expect(page.getByTestId("prompt-toggle-tools.system_claude_cli")).toHaveCount(0);
});

test("…and it IS listed when it is the connection actually in use", async ({ page }) => {
  await mockApi(page, { provider: "claude_cli" });
  await page.goto("/el/settings");
  await page.getByTestId("prompt-flow-tools").click();
  await expect(page.getByTestId("prompt-toggle-tools.system_claude_cli")).toBeVisible();
});

// --- editing: the footnote -----------------------------------------------

test("the one editable slice saves his sentence", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  const box = card.getByTestId("slice-input-student.pitch");
  await expect(box).toHaveValue(PITCH_DEFAULT);
  await expect(card).toContainText("Πώς να απευθύνεται στον μαθητή");

  await box.fill("Μίλα του απλά, χωρίς θεωρία.");
  await card.getByTestId("slice-save-student.pitch").click();

  await expect(card.getByTestId("slice-saved-student.pitch")).toBeVisible();
  const put = calls.find((c) => c.method === "PUT" && c.pathname === "/prompts/slices/student.pitch");
  expect(put?.body).toEqual({ text: "Μίλα του απλά, χωρίς θεωρία." });
});

test("a rejected save reverts the card and says ONE Greek sentence — and keeps his text", async ({ page }) => {
  await mockApi(page, {
    put: { status: 422, json: { detail: { code: "too_long", message: "2100 > 2000" } } },
  });
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  const box = card.getByTestId("slice-input-student.pitch");
  await box.fill("υπερβολικά μεγάλο κείμενο");
  await card.getByTestId("slice-save-student.pitch").click();

  const error = card.getByTestId("slice-error-student.pitch");
  await expect(error).toBeVisible();
  await expect(error).toContainText("μεγάλο");
  await expect(error).not.toContainText("422");

  // The optimistic "edited" badge must come back off — the server refused.
  await expect(card.getByTestId("slice-overridden-student.pitch")).not.toBeVisible();
  // But he does NOT lose what he typed.
  await expect(box).toHaveValue("υπερβολικά μεγάλο κείμενο");
});

test("an error code we have never seen never renders a raw key path at him", async ({ page }) => {
  await mockApi(page, {
    put: { status: 422, json: { detail: { code: "some_future_code", message: "…" } } },
  });
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  await card.getByTestId("slice-input-student.pitch").fill("κάτι");
  await card.getByTestId("slice-save-student.pitch").click();

  const error = card.getByTestId("slice-error-student.pitch");
  await expect(error).toBeVisible();
  await expect(error).not.toContainText("prompts.errors");
  await expect(error).not.toContainText("some_future_code");
});

test("reset puts the code default back", async ({ page }) => {
  const overridden = { ...STUDENT_BRIEF, has_override: true };
  const calls = await mockApi(page, {
    prompts: [CHAT_SYSTEM, TOOLS_DESCRIPTIONS, TOOLS_CLAUDE_CLI, CURRICULUM_SYSTEM, overridden],
    details: {
      ...DETAILS,
      "shared.student_brief": {
        ...DETAILS["shared.student_brief"],
        ...overridden,
        slices: [pitchSlice({ effective: "Η δική του πρόταση.", has_override: true })],
      },
    },
  });
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  const box = card.getByTestId("slice-input-student.pitch");
  await expect(box).toHaveValue("Η δική του πρόταση.");
  await expect(card.getByTestId("slice-overridden-student.pitch")).toBeVisible();

  await card.getByTestId("slice-reset-student.pitch").click();

  await expect(box).toHaveValue(PITCH_DEFAULT);
  await expect(card.getByTestId("slice-overridden-student.pitch")).not.toBeVisible();
  expect(
    calls.some((c) => c.method === "DELETE" && c.pathname === "/prompts/slices/student.pitch"),
  ).toBe(true);
});

test("history lists what he replaced, newest first", async ({ page }) => {
  await mockApi(page, {
    history: [
      { id: "h2", text: "Η δεύτερη εκδοχή.", replaced_at: "2026-07-17T10:57:19Z" },
      { id: "h1", text: "Η πρώτη εκδοχή.", replaced_at: "2026-07-16T09:00:00Z" },
    ],
  });
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  await card.getByTestId("slice-history-toggle-student.pitch").click();
  const rows = card.getByTestId("slice-history-student.pitch").getByRole("listitem");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("Η δεύτερη εκδοχή.");
  await expect(rows.nth(1)).toContainText("Η πρώτη εκδοχή.");
});

test("history left OPEN across a save refetches — it never sits on a spinner", async ({ page }) => {
  // Self-review caught this: the save invalidates the history it just made
  // stale, and nothing was re-fetching it. The list he was already looking at
  // would have spun forever.
  let calls = 0;
  await mockApi(page);
  await page.route(`${API_ORIGIN}/prompts/slices/student.pitch/history`, (r) => {
    calls += 1;
    return r.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify(
        calls === 1
          ? []
          : [{ id: "h1", text: PITCH_DEFAULT, replaced_at: "2026-07-17T10:57:19Z" }],
      ),
    });
  });
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  await card.getByTestId("slice-history-toggle-student.pitch").click();
  await expect(card).toContainText("Δεν υπάρχει τίποτα εδώ ακόμα");

  await card.getByTestId("slice-input-student.pitch").fill("Μίλα του απλά.");
  await card.getByTestId("slice-save-student.pitch").click();

  // The history he is LOOKING AT must now show what his save replaced.
  const rows = card.getByTestId("slice-history-student.pitch").getByRole("listitem");
  await expect(rows).toHaveCount(1);
  await expect(rows.nth(0)).toContainText(PITCH_DEFAULT);
});

test("a slice inside the cached prefix warns BEFORE he saves", async ({ page }) => {
  // Unreachable today — no shipped slice sits in the prefix — but the field is
  // on `SliceOut` and the day one does, the warning must already be wired.
  await mockApi(page, {
    details: {
      ...DETAILS,
      "shared.student_brief": {
        ...DETAILS["shared.student_brief"],
        slices: [pitchSlice({ cache_cost_warning: true })],
      },
    },
  });
  await page.goto("/el/settings");
  const card = await open(page, "shared", "shared.student_brief");

  const warning = card.getByTestId("slice-cache-warning-student.pitch");
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("μία φορά");
  await expect(warning).not.toContainText("$");
});

// --- i18n ----------------------------------------------------------------

test("every prompts.* string exists in BOTH Greek and English", () => {
  // Greek is the product, and this card is 40-odd new strings. An `en.json` that
  // silently lags is how the English side renders a raw key path.
  const read = (locale: string) =>
    JSON.parse(readFileSync(join(__dirname, `../src/messages/${locale}.json`), "utf8"));

  const flatten = (obj: Record<string, unknown>, prefix = ""): string[] =>
    Object.entries(obj).flatMap(([k, v]) =>
      v && typeof v === "object"
        ? flatten(v as Record<string, unknown>, `${prefix}${k}.`)
        : [`${prefix}${k}`],
    );

  const el = flatten(read("el").prompts ?? {}).sort();
  const en = flatten(read("en").prompts ?? {}).sort();
  expect(el.length).toBeGreaterThan(0);
  expect(el).toEqual(en);
});
