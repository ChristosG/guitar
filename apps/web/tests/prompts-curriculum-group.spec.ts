import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * THE "CURRICULUM" SHORTCUT GROUP (Plan C, Task 7 / C1).
 *
 * Ten prompts actually shape what a course/lesson becomes — the rest of the ~30
 * are surrounding machinery (fallback substitutions, refine, from-selection, tool
 * schemas…). `curriculum_group=True` on the registry entry marks exactly those
 * ten (`test_prompts_registry.py::test_the_curriculum_group_is_exactly_the_spec_s_set`
 * pins the backend side), and this file pins the frontend side: a synthetic
 * "Curriculum" group renders them a SECOND time, at the very top of the page —
 * a shortcut into the ordinary flow groups below, not a new editing mechanism.
 * `PromptRow` is reused UNCHANGED for the actual rendering (text, spans, the
 * slice editor); only the always-mounted outer ids are namespaced
 * (`curriculum-group-prompt-*`) so the shortcut copy and its "home" flow-group
 * copy do not fight over the same `data-testid`.
 *
 * Same mocking convention as `prompts.spec.ts`: the whole API origin is routed
 * (a bare `**\/settings` glob would also swallow the page navigation itself), and
 * OPTIONS is answered explicitly because `X-App-Locale` makes every call a
 * non-simple request.
 */

const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

const summary = (over: Record<string, unknown>) => ({
  kind: "prompt",
  provider: null,
  cache_prefix: false,
  cache_cost_warning: false,
  language_from_course: false,
  curriculum_group: false,
  has_override: false,
  ...over,
});

const textSlice = (promptId: string, text: string, over: Record<string, unknown> = {}) => ({
  id: promptId,
  prompt_id: promptId,
  label_el: "Το κείμενο της οδηγίας",
  kind: "replace",
  default: text,
  effective: text,
  has_override: false,
  max_chars: Math.max(2000, text.length * 2),
  cache_cost_warning: false,
  ...over,
});

// The exact ten the backend flags — kept here as a literal so a drift between
// the two sides (a prompt added to one list but not the other) fails a TEST
// rather than surfacing as a silent gap on the page.
const CURRICULUM_GROUP_IDS = [
  "curriculum.system",
  "curriculum.library",
  "curriculum.outline",
  "curriculum.extend",
  "lesson.draft",
  "lesson.tier_library",
  "lesson.tier_web",
  "lesson.gap",
  "lesson.deepen",
  "lesson.repair",
];

const CHAT_SYSTEM = summary({
  id: "chat.system", flow: "chat",
  title_el: "Ο βοηθός συνομιλίας",
  what_it_does_el: "Λέει στον βοηθό τι να κάνει.",
  when_it_runs_el: "Σε κάθε μήνυμα.",
});

// One prompt PER curriculum flow that is deliberately NOT in the group, to prove
// the filter is `curriculum_group`, not "everything under this flow".
const CURRICULUM_REFINE = summary({
  id: "curriculum.refine", flow: "curriculum",
  title_el: "Η διόρθωση ενός κομματιού",
  what_it_does_el: "Διορθώνει ό,τι ζήτησες, μόνο αυτό.",
  when_it_runs_el: "Όταν ζητάς αλλαγή σε ένα κομμάτι.",
});
const LESSON_RETRIEVED = summary({
  id: "lesson.retrieved", flow: "lesson", kind: "fragment",
  title_el: "Τα αποσπάσματα του μαθήματος",
  what_it_does_el: "Μπαίνει όταν η βιβλιοθήκη δεν χωράει ολόκληρη.",
  when_it_runs_el: "Σπάνια.",
});

const OUTLINE_EL = "YOUR TASK: design the outline of a course.\n\nLANGUAGE: write everything you produce in Greek (el)";
const LANG_LABEL = "Ο κανόνας γλώσσας — τον ορίζει η γλώσσα του μαθητή/προγράμματος, όχι αυτή η οθόνη";

const CURRICULUM_GROUP_PROMPTS = [
  summary({
    id: "curriculum.system", flow: "curriculum", curriculum_group: true,
    title_el: "Η εισαγωγή για τη συγγραφή ύλης",
    what_it_does_el: "Η πρώτη οδηγία σε κάθε δημιουργία ύλης.",
    when_it_runs_el: "Κάθε φορά που φτιάχνεις πρόγραμμα σπουδών.",
    cache_prefix: true, cache_cost_warning: true,
  }),
  summary({
    id: "curriculum.library", flow: "curriculum", curriculum_group: true,
    title_el: "Ολόκληρη η βιβλιοθήκη σου",
    what_it_does_el: "Δίνει στον βοηθό όλα τα βιβλία που διάλεξες.",
    when_it_runs_el: "Κάθε φορά που φτιάχνεις πρόγραμμα σπουδών.",
    cache_prefix: true, cache_cost_warning: true,
  }),
  summary({
    id: "curriculum.outline", flow: "curriculum", curriculum_group: true,
    language_from_course: true,
    title_el: "Ο σκελετός του προγράμματος",
    what_it_does_el: "Ζητάει μόνο τη δομή του προγράμματος.",
    when_it_runs_el: "Μία φορά, μόλις πατήσεις δημιουργία προγράμματος.",
  }),
  summary({
    id: "curriculum.extend", flow: "curriculum", curriculum_group: true,
    language_from_course: true,
    title_el: "Η νέα ενότητα σε υπάρχον πρόγραμμα",
    what_it_does_el: "Προσθέτει μία καινούργια ενότητα.",
    when_it_runs_el: "Όταν προσθέτεις ενότητα.",
  }),
  summary({
    id: "lesson.draft", flow: "lesson", curriculum_group: true,
    language_from_course: true,
    title_el: "Η συγγραφή ενός μαθήματος",
    what_it_does_el: "Ζητάει το ίδιο το μάθημα.",
    when_it_runs_el: "Μία φορά για κάθε μάθημα.",
  }),
  summary({
    id: "lesson.tier_library", flow: "lesson", kind: "fragment", curriculum_group: true,
    title_el: "Οδηγία: μέσα στα βιβλία σου",
    what_it_does_el: "Λέει να διδάξει από τις σελίδες σου.",
    when_it_runs_el: "Σε κάθε μάθημα καλυμμένο από τη βιβλιοθήκη.",
  }),
  summary({
    id: "lesson.tier_web", flow: "lesson", kind: "fragment", curriculum_group: true,
    title_el: "Οδηγία: θέλει πρόσφατες πληροφορίες",
    what_it_does_el: "Λέει να σημειώνει πού χρειάζεται έλεγχος.",
    when_it_runs_el: "Σε μαθήματα «web».",
  }),
  summary({
    id: "lesson.gap", flow: "lesson", kind: "fragment", curriculum_group: true,
    title_el: "Οδηγία: ΔΕΝ είναι στα βιβλία σου",
    what_it_does_el: "Λέει να ΜΗΝ αναφέρει καμία σελίδα.",
    when_it_runs_el: "Σε μαθήματα «γενικές γνώσεις».",
  }),
  summary({
    id: "lesson.deepen", flow: "lesson", curriculum_group: true,
    language_from_course: true,
    title_el: "Το ξαναγράψιμο ενός κοντού μαθήματος",
    what_it_does_el: "Ξαναγράφει ένα μάθημα που βγήκε κοντό.",
    when_it_runs_el: "Μόνο όταν ένα μάθημα βγει κάτω από το όριο.",
  }),
  summary({
    id: "lesson.repair", flow: "lesson", curriculum_group: true,
    title_el: "Όταν παραπέμπει σε σελίδα που δεν υπάρχει",
    what_it_does_el: "Ζητάει ξαναγράψιμο με σωστές παραπομπές.",
    when_it_runs_el: "Μόνο όταν πιαστεί λάθος παραπομπή.",
  }),
];

const PROMPTS = [CHAT_SYSTEM, CURRICULUM_REFINE, LESSON_RETRIEVED, ...CURRICULUM_GROUP_PROMPTS];

const DETAILS: Record<string, Record<string, unknown>> = {
  "curriculum.outline": {
    ...CURRICULUM_GROUP_PROMPTS.find((p) => p.id === "curriculum.outline"),
    source_ref: "app/curriculum/outline.py:161",
    course_language: "el",
    text: OUTLINE_EL,
    messages: [{ role: "user", content: OUTLINE_EL, cached: false }],
    spans: [
      {
        name: "language_directive",
        label_el: LANG_LABEL,
        value: "LANGUAGE: write everything you produce in Greek (el)",
        start: OUTLINE_EL.indexOf("LANGUAGE:"),
        end: OUTLINE_EL.length,
      },
    ],
    slices: [textSlice("curriculum.outline", OUTLINE_EL)],
  },
};

interface Opts {
  put?: { status: number; json: unknown };
}

async function mockApi(page: Page, opts: Opts = {}) {
  const calls: { method: string; pathname: string; body: unknown }[] = [];

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
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(b) });

    if (pathname === "/settings") {
      return json({ provider: "anthropic", model: "claude-sonnet-5", configured: true, key_hint: "7f2a" });
    }
    if (pathname === "/prompts") return json(PROMPTS);

    if (pathname.startsWith("/prompts/slices/")) {
      const rest = pathname.slice("/prompts/slices/".length);
      if (rest.endsWith("/history")) return json([]);
      if (method === "PUT") {
        if (opts.put) return json(opts.put.json, opts.put.status);
        const slice = textSlice(rest, (body as { text: string }).text, { has_override: true });
        return json(slice);
      }
      if (method === "DELETE") return json(textSlice(rest, OUTLINE_EL));
    }

    if (pathname.startsWith("/prompts/")) {
      const id = decodeURIComponent(pathname.slice("/prompts/".length));
      const detail = DETAILS[id];
      if (!detail) return json({ detail: { code: "unknown_prompt", message: "no" } }, 404);
      return json(detail);
    }
    return json([]);
  });

  return calls;
}

test("the Curriculum group renders at the TOP of the page and lists exactly the ten curriculum-crucial prompts", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");

  const trigger = page.getByTestId("prompt-flow-curriculum-group");
  await expect(trigger).toBeVisible();
  await expect(trigger).toContainText(String(CURRICULUM_GROUP_IDS.length));

  // AT THE TOP: it must appear before every ordinary flow trigger, chat included
  // (chat is registered first among the flows themselves).
  const flowTriggers = page.locator('[data-testid^="prompt-flow-"]');
  await expect(flowTriggers.first()).toHaveAttribute("data-testid", "prompt-flow-curriculum-group");

  await trigger.click();
  for (const id of CURRICULUM_GROUP_IDS) {
    await expect(page.getByTestId(`curriculum-group-prompt-toggle-${id}`)).toBeVisible();
  }

  // The two DELIBERATE non-members must not have leaked in under this group.
  await expect(page.getByTestId("curriculum-group-prompt-toggle-curriculum.refine")).toHaveCount(0);
  await expect(page.getByTestId("curriculum-group-prompt-toggle-lesson.retrieved")).toHaveCount(0);
  await expect(page.getByTestId("curriculum-group-prompt-toggle-chat.system")).toHaveCount(0);
});

test("a prompt still renders in its ORIGINAL flow group too — the shortcut is a second door, not a move", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");

  // Its home group ("curriculum") still has it, under the UNPREFIXED id — proving
  // this is a genuine reuse of the existing rendering, not a relocation.
  await page.getByTestId("prompt-flow-curriculum").click();
  await expect(page.getByTestId("prompt-toggle-curriculum.outline")).toBeVisible();
});

test("opening curriculum.outline from the shortcut shows the chip and the editable text, and Save PUTs it", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/el/settings");

  await page.getByTestId("prompt-flow-curriculum-group").click();
  await page.getByTestId("curriculum-group-prompt-toggle-curriculum.outline").click();

  const card = page.getByTestId("curriculum-group-prompt-curriculum.outline");
  const text = card.getByTestId("prompt-text-curriculum.outline");
  await expect(text).toContainText("design the outline of a course");

  // The read-only span chip over the language directive — `{var}` renders as a
  // chip, exactly as it already does everywhere else in this card.
  await expect(card.getByTestId("prompt-span-language_directive")).toBeVisible();

  // And the SAME editable textarea the normal card would show — no new override
  // system, just `PromptRow` rendered a second time.
  const box = card.getByTestId("slice-input-curriculum.outline");
  await expect(box).toHaveValue(OUTLINE_EL);
  await box.fill("ΝΕΟ ΚΕΙΜΕΝΟ ΓΙΑ ΤΟ ΣΚΕΛΕΤΟ.");
  await card.getByTestId("slice-save-curriculum.outline").click();

  await expect(card.getByTestId("slice-saved-curriculum.outline")).toBeVisible();
  const put = calls.find((c) => c.method === "PUT" && c.pathname === "/prompts/slices/curriculum.outline");
  expect(put?.body).toEqual({ text: "ΝΕΟ ΚΕΙΜΕΝΟ ΓΙΑ ΤΟ ΣΚΕΛΕΤΟ." });
});

test("?promptGroup=curriculum — the wizard's step-3 deep-link — opens the group automatically", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings?promptGroup=curriculum");

  // No click needed: it is ALREADY open, and scrolled to.
  await expect(page.getByTestId("curriculum-group-prompt-toggle-curriculum.system")).toBeVisible();
});
