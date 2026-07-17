import { test, expect } from "@playwright/test";

/**
 * THE PROMPTS CARD, AGAINST THE REAL STACK — the deployed `web` image on :8790,
 * talking to the real `api` on :8791 and its real registry of 31 prompts.
 *
 * WHY THIS EXISTS when `tests/prompts.spec.ts` already covers the same card:
 * that suite stubs the API and boots its own `next dev`. It proves the component
 * renders what the API *says*; it cannot prove the API says anything true, and
 * it never touches the image that actually ships. Yesterday an e2e spec caught a
 * 19-HOUR-STALE bundle for exactly that reason. A source fix that is not
 * deployed is not a fix, and the only thing that can tell the difference is a
 * test that opens the real port.
 *
 * READ-ONLY, ON PURPOSE. It logs in, opens two prompts and asserts. It never
 * saves a slice: this runs against the tutor's live database, and a test that
 * writes an override would leave his app configured by a robot. It also costs
 * NOTHING — `GET /prompts/{id}` renders the prompt from the registry's own
 * builders with sample data; there is no model call on this path. That is why
 * this one live spec is cheap enough to keep, unlike its neighbours here which
 * spend forty minutes reading a book.
 *
 * Run:  npm run test:e2e -- e2e/prompts-card.spec.ts
 */

const PASSWORD = process.env.APP_PASSWORD ?? "guitar24";

/** A phrase from the REAL `SYSTEM_PROMPT` (`app/agent/prompts.py:67`) — the
 * anti-hallucination guard, which is the sentence this whole feature exists to
 * make visible. If someone paraphrases it to be friendlier, this fails, and that
 * is the point: "we cant degrade their quality so the teacher understands them
 * better." */
const GUARD = "never invent a student, curriculum, id, citation, URL/link, or fact";

/** The real `student.pitch` default (`app/students/context.py`) — the one
 * editable sentence in the entire app. */
const PITCH = "Write for THIS student: pitch the explanations at his level";

test.setTimeout(120_000);

test("the DEPLOYED settings page shows the real prompts, verbatim, from the real API", async ({
  page,
}) => {
  await page.goto("/el/settings");
  if (page.url().includes("/login")) {
    await page.getByTestId("login-password").fill(PASSWORD);
    await page.getByTestId("login-submit").click();
  }
  await expect(page).toHaveURL(/\/el\/settings/);

  // The card is served by the running image — not by a `next dev` in this repo.
  await expect(page.getByTestId("settings-prompts-card")).toBeVisible();

  // Every flow the live registry actually has, grouped and shut.
  for (const flow of ["chat", "tools", "curriculum", "lesson", "retrieval", "artifacts", "ocr", "shared"]) {
    await expect(page.getByTestId(`prompt-flow-${flow}`)).toBeVisible();
  }
  await expect(page.getByTestId("prompt-toggle-chat.system")).not.toBeVisible();

  // --- the guard, verbatim, out of the real registry ----------------------
  await page.getByTestId("prompt-flow-chat").click();
  await page.getByTestId("prompt-toggle-chat.system").click();
  const chat = page.getByTestId("prompt-chat.system");
  await expect(chat.getByTestId("prompt-text-chat.system")).toContainText(GUARD);
  await expect(chat).toContainText("app/agent/prompts.py:67");
  // 1,301 characters of guards, and no textarea anywhere near them.
  await expect(chat.getByTestId("prompt-locked-chat.system")).toBeVisible();
  await expect(chat.locator("textarea")).toHaveCount(0);

  // --- the one editable sentence in the app -------------------------------
  await page.getByTestId("prompt-flow-shared").click();
  await page.getByTestId("prompt-toggle-shared.student_brief").click();
  const brief = page.getByTestId("prompt-shared.student_brief");
  const box = brief.getByTestId("slice-input-student.pitch");
  await expect(box).toBeVisible();
  await expect(box).toHaveValue(new RegExp(PITCH.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));

  // The student's data lands as a labelled chip, not a hole — `_locate` found
  // these spans in the text the live builder actually produced.
  await expect(brief.getByTestId("prompt-span-pitch")).toBeVisible();
  await expect(brief.getByTestId("prompt-span-goals")).toBeVisible();
});
