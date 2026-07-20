/** The arithmetic behind the outline editor's live footer — *"5 modules · 20
 * lessons · ~44,000 words · est. $2.40"*.
 *
 * WHY THESE NUMBERS EXIST IN TYPESCRIPT AT ALL. The tutor is editing an outline
 * that has not been drafted yet, and every module he adds is real money. The
 * footer has to move WHILE HE TYPES — a round trip per keystroke to ask the API
 * what it would cost is absurd, so the two constants that decide it are mirrored
 * here, and named after the Python they mirror so a change there is greppable.
 */

/** `app/curriculum/depth.py::WORDS_PER_MINUTE`. 55 words a minute of taught
 * material: a 40-minute lesson lands on 2,200 words, the middle of Chris's own
 * range. */
const WORDS_PER_MINUTE = 55;

/* No Q&A carve-out any more (`shape.py`, 2026-07-21): 50 means 50 — the whole
 * booked slot is teaching time, and Q&A only exists as a weighted blueprint
 * section inside it. */

/** What ONE lesson costs to draft, at Sonnet 5 prices, WITH THE LIBRARY ALREADY
 * CACHED: ~90K library tokens read at 0.1x ($0.027) + ~6K output tokens ($0.09).
 * From the measured cost table in the plan — a 20-lesson Greek curriculum came to
 * ~$2.72 all-in, of which ~$0.39 was the outline call that has ALREADY RUN by the
 * time this editor is on screen.
 *
 * So the number in the footer is the cost of pressing the button, not the cost of
 * the session so far. Which is the only one he can still decide about.
 */
const EST_COST_PER_LESSON_USD = 0.12;

export interface OutlineTotals {
  modules: number;
  lessons: number;
  words: number;
  costUsd: number;
}

interface Sized {
  lessons: { est_minutes: number }[];
}

/** Mirrors `depth.target_words(shape.teaching_minutes)` per lesson, so the words a
 * lesson is *drafted* to and the words this footer *promises* are the same number,
 * derived the same way, from the minutes he can edit right here. */
export function targetWords(estMinutes: number): number {
  return Math.max(1, estMinutes || 0) * WORDS_PER_MINUTE;
}

export function outlineTotals(modules: Sized[]): OutlineTotals {
  const lessons = modules.reduce((n, m) => n + m.lessons.length, 0);
  const words = modules.reduce(
    (w, m) => w + m.lessons.reduce((lw, l) => lw + targetWords(l.est_minutes), 0),
    0,
  );
  return {
    modules: modules.length,
    lessons,
    words,
    costUsd: lessons * EST_COST_PER_LESSON_USD,
  };
}
