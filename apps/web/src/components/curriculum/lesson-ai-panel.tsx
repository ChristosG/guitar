"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Maximize2, Minimize2, Pencil, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { LessonWhatChanged } from "@/components/curriculum/lesson-what-changed";
import { WhatChanged } from "@/components/curriculum/what-changed";
import { useLessonAiScope } from "@/components/curriculum/lesson-ai-scope";
import {
  ApiError,
  applyLessonAi,
  getJob,
  planLessonAi,
  type BlockNode,
  type LessonPlan,
} from "@/lib/api";
import { jobErrorText } from "@/lib/job-errors";
import { cn } from "@/lib/utils";

/** Poll cadence + cap for a `lesson_ai` job — the SAME constants as
 * `chat/chat-panel.tsx`'s own loop, and duplicated for the same stated reason
 * that one duplicates `generate-dialog.tsx`'s: a 10-line loop used in a handful
 * of places is small deliberate duplication, not a missing abstraction. ~2s
 * between polls, capping at ~10 minutes — a `claude -p` planner turn was
 * measured at 6-8 minutes, and the job keeps running server-side past the cap
 * regardless (the cap ends THIS component's wait, not the work). */
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 300;

/** Where the panel is in the one flow it has. `idle` is also where a failed
 * plan lands, so the box he typed in is the thing he gets back. */
type Phase = "idle" | "planning" | "card" | "applying" | "done";

interface LessonAiPanelProps {
  /** The page's CURRENT tree. Read only — to find the lesson the scope points
   * at, so the panel can show its sections, its hand-edited strip, and (after
   * an apply) its `prev_segments` diff. Re-handed fresh by the page after
   * `onApplied`, which is how the done state sees the new segments. */
  tree: BlockNode;
  /** The page's `refreshTree` — called after an apply succeeds, because the
   * lesson's segments were replaced underneath the board. */
  onApplied: () => void;
}

/** «AI ΣΤΟ ΜΑΘΗΜΑ» — the one AI door on a lesson row, and what is behind it.
 *
 * THE SHAPE OF THE FLOW IS THE POINT. He types (or taps a chip), presses
 * «Φτιάξε πλάνο», and gets a PLAN back — one row per section, with `keep` on
 * most of them. Nothing on the lesson changes until he ticks sections and
 * presses apply. That gate exists because the alternative is what the revise
 * engine does to a lesson: requeue it and rewrite all 2.750 words from scratch,
 * including the paragraphs he wrote himself. A lesson he has already read and
 * curated is not regenerable content.
 *
 * WHY A JOB AND NOT A REQUEST: the planner runs `claude -p` over the lesson,
 * its neighbours and its sources — measured in minutes. Inside an HTTP request
 * that answer never arrives; the Cloudflare edge cuts at ~100s while the server
 * keeps working (verified live 2026-07-21, again 2026-09-11). So both doors
 * return 202 and this component polls, exactly as the revise drawer's turn does.
 *
 * WHY IT SITS HERE rather than inside the row: it is a SIDE PANEL, a sibling of
 * the board, so the row's button reaches it through `lesson-ai-scope.tsx`'s
 * context. Same fixed-aside + backdrop construction as `ReviseDrawer` (there is
 * still no Sheet primitive under `components/ui` — only a centered `dialog`),
 * same Escape handling, same `openRequest` counter.
 *
 * ERRORS ARE NEVER THE SERVER'S OWN WORDS. `ApiError.detail` is English prose
 * written on the server; `el` is this product's default locale. So this panel
 * branches on `code` (`lesson_busy` has its own sentence, because "wait" is a
 * different instruction from "try again") and otherwise says its own generic
 * Greek line. A failed JOB is different — its `error_kind` is a taxonomy the
 * API maintains precisely so the frontend can localize it, which is what
 * `jobErrorText` does.
 */
export function LessonAiPanel({ tree, onApplied }: LessonAiPanelProps) {
  const t = useTranslations("curricula.lessonAi");
  const tJobErrors = useTranslations("jobErrors");
  const scope = useLessonAiScope();

  const [open, setOpen] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [plan, setPlan] = useState<LessonPlan | null>(null);
  const [error, setError] = useState<string | null>(null);

  const lessonId = scope?.lesson?.id ?? null;
  const lessonNode = useMemo(
    () => (lessonId ? findNode(tree, lessonId) : null),
    [tree, lessonId],
  );
  const segments = useMemo(
    () => (lessonNode?.children ?? []).filter((c) => c.kind === "segment"),
    [lessonNode],
  );
  const edited = useMemo(() => segments.filter((s) => s.meta?.tutor_edited), [segments]);

  // A PANEL POINTED AT A NEW LESSON IS A NEW PANEL. Without this, opening
  // «Μπράτσο» after «Καβαλάρης» would show the second lesson's title above the
  // first one's plan — and apply would send that plan's sections to the wrong
  // lesson. Keyed on the id rather than the node so a tree refetch (which mints
  // new node objects for the same lesson) does not wipe the plan he is in the
  // middle of reading.
  //
  // Adjusted DURING RENDER rather than in an effect — React's own documented
  // shape for "reset state when a prop changes" (you-might-not-need-an-effect),
  // and the one the lint rule against setState-in-an-effect is pointing at. It
  // re-renders immediately, before anything paints, so there is no frame in
  // which the previous lesson's plan is on screen under the new lesson's title.
  const [lastLessonId, setLastLessonId] = useState(lessonId);
  if (lessonId !== lastLessonId) {
    setLastLessonId(lessonId);
    setInstruction("");
    setPlan(null);
    setPhase("idle");
    setError(null);
  }

  // Escape-to-close, on `window` rather than the aside: focus can be in the
  // textarea, on a chip, or inside the «Τι άλλαξε;» dialog when he reaches for
  // it, and a listener scoped to one element would miss all of those. Skips a
  // keystroke something else already handled (the diff dialog also closes on
  // Escape) so one press doesn't close both.
  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !e.defaultPrevented) setOpen(false);
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  // The row button asks to open by bumping a counter. A counter and not a
  // boolean because THIS component owns open/closed (Escape, the X, the
  // backdrop) — see `lesson-ai-scope.tsx`.
  const lastRequest = useRef(0);
  useEffect(() => {
    const n = scope?.openRequest ?? 0;
    if (n === 0 || n === lastRequest.current) return;
    lastRequest.current = n;
    setOpen(true);
  }, [scope?.openRequest]);

  // WHICH LESSON IS THE PANEL ON *NOW*. A plan job runs for minutes, and
  // nothing stops him closing the panel and opening another lesson while it
  // does. Without this, the old job's answer would land on the new lesson —
  // its title above someone else's plan, and an apply that rewrites the wrong
  // sections. Every handler captures the id it started on and refuses to write
  // state once this ref has moved on. A ref and not state: it must be readable
  // by an async closure without re-running it.
  const lessonIdRef = useRef(lessonId);
  useEffect(() => {
    lessonIdRef.current = lessonId;
  }, [lessonId]);

  /** Poll one job to a terminal status, or to the cap. */
  const waitForJob = useCallback(async (jobId: string) => {
    let job = await getJob(jobId);
    let polls = 1;
    while (job.status !== "succeeded" && job.status !== "failed" && polls < MAX_POLLS) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      job = await getJob(jobId);
      polls++;
    }
    return job;
  }, []);

  /** Server errors, in Greek. `lesson_busy` earns its own sentence — the
   * action it asks for is "wait", not "try again" — and everything else gets
   * this panel's generic line rather than the server's English `detail`. */
  const errorText = useCallback(
    (err: unknown) => (err instanceof ApiError && err.code === "lesson_busy" ? t("busy") : t("error")),
    [t],
  );

  async function handlePlan(extraNote?: string) {
    const target = lessonId;
    if (!target) return;
    setPhase("planning");
    setError(null);
    try {
      const accepted = await planLessonAi(target, { instruction, note: extraNote });
      const job = await waitForJob(accepted.job_id);
      if (lessonIdRef.current !== target) return;
      if (job.status !== "succeeded") {
        setError(jobErrorText(job, tJobErrors));
        // Back to whatever he had: a re-plan that failed must not throw away
        // the plan he was already looking at.
        setPhase(plan ? "card" : "idle");
        return;
      }
      setPlan((job.progress as { plan?: LessonPlan } | null)?.plan ?? null);
      setPhase("card");
    } catch (err) {
      if (lessonIdRef.current !== target) return;
      setError(errorText(err));
      setPhase(plan ? "card" : "idle");
    }
  }

  async function handleApply(picks: { section: string; brief: string }[], extraNote: string) {
    const target = lessonId;
    if (!target) return;
    setPhase("applying");
    setError(null);
    try {
      const accepted = await applyLessonAi(target, { instruction, note: extraNote, sections: picks });
      const job = await waitForJob(accepted.job_id);
      if (lessonIdRef.current !== target) return;
      if (job.status !== "succeeded") {
        setError(jobErrorText(job, tJobErrors));
        setPhase("card");
        return;
      }
      // The lesson's segments were replaced — the board and this panel's own
      // done state both read the tree, so refetch before showing either.
      onApplied();
      setPhase("done");
    } catch (err) {
      if (lessonIdRef.current !== target) return;
      setError(errorText(err));
      setPhase("card");
    }
  }

  const busy = phase === "planning" || phase === "applying";
  const lesson = scope?.lesson ?? null;

  if (!open || !lesson) return null;

  return (
    <div
      className={cn("fixed inset-0 z-40 flex", fullScreen ? "justify-center" : "justify-end")}
      data-testid="lesson-ai-panel"
      data-fullscreen={fullScreen ? "true" : "false"}
    >
      <button
        type="button"
        aria-label={t("close")}
        className="absolute inset-0 h-full w-full bg-black/10 backdrop-blur-xs"
        onClick={() => setOpen(false)}
      />
      <aside
        className={cn(
          "relative flex h-full w-full flex-col gap-4 overflow-y-auto border-border bg-background p-4 shadow-xl",
          fullScreen ? "max-w-full border-l-0" : "max-w-md border-l",
        )}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="font-heading text-base font-medium">{t("heading")}</h2>
            {/* WHICH LESSON. A panel that looked identical for every row would
                be a trap: he would type an instruction meant for one lesson and
                watch another one change. */}
            <p className="truncate text-sm font-medium" data-testid="lesson-ai-scope">
              {lesson.moduleTitle
                ? t("scopedTo", { lesson: lesson.title, module: lesson.moduleTitle })
                : lesson.title}
            </p>
            <p className="text-sm text-muted-foreground">{t("description")}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              data-testid="lesson-ai-fullscreen-toggle"
              onClick={() => setFullScreen((prev) => !prev)}
              title={fullScreen ? t("collapse") : t("expand")}
            >
              {fullScreen ? <Minimize2 /> : <Maximize2 />}
              <span className="sr-only">{fullScreen ? t("collapse") : t("expand")}</span>
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              data-testid="lesson-ai-close"
              onClick={() => setOpen(false)}
            >
              <X />
              <span className="sr-only">{t("close")}</span>
            </Button>
          </div>
        </div>

        {/* WHAT HE WROTE HIMSELF, named before he types anything. It is the
            reason the «Ενημέρωσε τις υπόλοιπες ενότητες» chip exists, and each
            chip opens the same diff the row's own tutor-edited chip does — so
            "what did I actually change in there?" is answerable without
            closing this panel. */}
        {edited.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <p className="text-xs text-muted-foreground">{t("editedStrip")}</p>
            <div className="flex flex-wrap gap-1.5">
              {edited.map((segment) => (
                <EditedChip key={segment.id} segment={segment} />
              ))}
            </div>
          </div>
        )}

        {/* Stays on screen while a job runs — DISABLED, not removed. What he
            asked for is the thing he is waiting on; taking the sentence away
            mid-wait would leave him watching a spinner with no idea what it is
            answering. Gone only in the done state, where «Νέα αλλαγή» brings it
            back empty. */}
        {phase !== "done" && (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap gap-1.5">
              {/* Offered ONLY when something was hand-edited — otherwise it
                  would ask the model to propagate changes that do not exist,
                  and the plan would come back keeping everything. */}
              {edited.length > 0 && (
                <Chip
                  testId="lesson-ai-chip-propagate"
                  disabled={busy}
                  onClick={() => setInstruction(t("chipPropagate"))}
                >
                  {t("chipPropagate")}
                </Chip>
              )}
              <Chip testId="lesson-ai-chip-harder" disabled={busy} onClick={() => setInstruction(t("chipHarder"))}>
                {t("chipHarder")}
              </Chip>
              <Chip
                testId="lesson-ai-chip-examples"
                disabled={busy}
                onClick={() => setInstruction(t("chipExamples"))}
              >
                {t("chipExamples")}
              </Chip>
            </div>

            <Textarea
              data-testid="lesson-ai-instruction"
              value={instruction}
              disabled={busy}
              placeholder={t("placeholder")}
              onChange={(e) => setInstruction(e.target.value)}
            />

            <div>
              <Button
                type="button"
                data-testid="lesson-ai-plan"
                disabled={instruction.trim().length < 3 || busy}
                onClick={() => void handlePlan()}
              >
                {busy ? <Loader2 className="animate-spin" /> : <Sparkles />}
                {t("plan")}
              </Button>
            </div>
          </div>
        )}

        {busy && (
          <p
            role="status"
            data-testid="lesson-ai-status"
            className="flex items-center gap-2 text-sm text-muted-foreground"
          >
            <Loader2 className="size-4 shrink-0 animate-spin" />
            {phase === "planning" ? t("planning") : t("applying")}
          </p>
        )}

        {error && (
          <p role="alert" data-testid="lesson-ai-error" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {phase === "card" && plan && (
          <LessonPlanCard
            plan={plan}
            onReplan={(note) => void handlePlan(note)}
            onApply={(picks, note) => void handleApply(picks, note)}
            busy={false}
          />
        )}

        {phase === "done" && (
          <div className="flex flex-col gap-2">
            <p className="text-sm" data-testid="lesson-ai-done">
              {t("done")}
            </p>
            {/* The way back. `prev_segments` is the snapshot the apply took
                before replacing anything, and restore TOGGLES — so this is an
                inspector plus an undo, not a one-way door. */}
            {lessonNode && Array.isArray(lessonNode.meta?.prev_segments) && (
              <LessonWhatChanged
                lessonId={lessonNode.id}
                prevSegments={lessonNode.meta.prev_segments}
                liveSegments={segments}
                instruction={lessonNode.meta.ai_instruction}
                onRestored={() => onApplied()}
              />
            )}
            <div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="lesson-ai-again"
                onClick={() => {
                  setInstruction("");
                  setPlan(null);
                  setError(null);
                  setPhase("idle");
                }}
              >
                {t("again")}
              </Button>
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}

function Chip({
  testId,
  disabled,
  onClick,
  children,
}: {
  testId: string;
  disabled?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className="rounded-full border border-border bg-muted px-2.5 py-1 text-xs hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  );
}

/** One hand-edited section, and its diff on demand. Reuses `WhatChanged`
 * directly (rather than the lesson-level `LessonWhatChanged`) because a SEGMENT
 * has a real before/after in `meta.tutor_edited.prev_body` and already has its
 * own Undo elsewhere — there is no restore to offer here. */
function EditedChip({ segment }: { segment: BlockNode }) {
  const [open, setOpen] = useState(false);
  const editedMeta = segment.meta?.tutor_edited;

  return (
    <>
      <button
        type="button"
        data-testid="lesson-ai-edited-chip"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-1 text-xs hover:bg-accent"
      >
        <Pencil className="size-3 shrink-0" aria-hidden />
        {segment.title}
      </button>
      <WhatChanged
        open={open}
        onOpenChange={setOpen}
        before={editedMeta?.prev_body ?? ""}
        after={segment.body ?? ""}
      />
    </>
  );
}

interface LessonPlanCardProps {
  plan: LessonPlan;
  /** «Δεν μου αρέσει, ξανακάνε το» — replans with an extra note appended to
   * the original instruction. */
  onReplan: (note: string) => void;
  /** The ticked sections, plus whatever he added in the card's note box. */
  onApply: (picks: { section: string; brief: string }[], note: string) => void;
  busy: boolean;
}

/** PLACEHOLDER — Task 2.6 replaces the body of this component with the real
 * per-section card (tick boxes, editable briefs, the impact line, the dropped
 * list). Its PROPS are already the ones that card takes, and the panel already
 * passes working handlers into them, so 2.6 is a body swap and not a rewiring
 * of the flow. What it renders today is the one line the tutor most needs from
 * a plan — the planner's own summary. */
function LessonPlanCard({ plan }: LessonPlanCardProps) {
  return (
    <div data-testid="lesson-plan-card" className="rounded-lg border border-border p-3 text-sm">
      {plan.summary}
    </div>
  );
}

/** Depth-first search by id. The scope carries an id (the row button had one);
 * the panel needs the NODE, for its segments and its `prev_segments`. */
function findNode(node: BlockNode, id: string): BlockNode | null {
  if (node.id === id) return node;
  for (const child of node.children ?? []) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}
