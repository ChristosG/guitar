"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import {
  Bold,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Italic,
  Layers,
  Loader2,
  MoreVertical,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  Underline,
} from "lucide-react";
import { Artifact } from "@/components/artifacts/artifact";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { InlineMarks } from "@/components/ui/inline-marks";
import { toggleMark, type MarkName } from "@/lib/inline-marks";
import { Textarea } from "@/components/ui/textarea";
import { useConfirm } from "@/components/ui/confirm";
import { AddLessonDialog } from "@/components/curriculum/add-lesson-dialog";
import { AttachArtifactDialog } from "@/components/curriculum/attach-artifact-dialog";
import { SegmentDialog } from "@/components/curriculum/segment-dialog";
import { ExtendWithChat } from "@/components/curriculum/extend-with-chat";
import { LessonWhatChanged } from "@/components/curriculum/lesson-what-changed";
import { useLessonAiScope } from "@/components/curriculum/lesson-ai-scope";
import { useReviseScope } from "@/components/curriculum/revise-scope";
import { WhatChanged } from "@/components/curriculum/what-changed";
import { LessonSources } from "@/components/curriculum/lesson-sources";
import { TierBadge } from "@/components/curriculum/tier-badge";
import {
  ApiError,
  deepenLesson,
  deleteBlock,
  deleteCurriculum,
  reorderBlock,
  updateBlock,
  type ArtifactOut,
  type BlockNode,
  type DraftStatus,
  type SegmentStatus,
} from "@/lib/api";
import { cn } from "@/lib/utils";

interface SubtreeCounts {
  total: number;
  modules: number;
  lessons: number;
}

/** What a `DELETE /blocks/{id}` is actually about to take with it. The API cascades
 * (ORM `delete-orphan` AND `ON DELETE CASCADE`), so one click on the root's Trash
 * icon destroys the entire generated tree — which is exactly why the confirm dialog
 * has to be able to SAY so, with numbers, rather than asking "are you sure?" about
 * an unnamed quantity. */
function countSubtree(nodes: BlockNode[]): SubtreeCounts {
  return nodes.reduce<SubtreeCounts>(
    (acc, child) => {
      const sub = countSubtree(child.children);
      return {
        total: acc.total + 1 + sub.total,
        modules: acc.modules + (child.kind === "module" ? 1 : 0) + sub.modules,
        lessons: acc.lessons + (child.kind === "lesson" ? 1 : 0) + sub.lessons,
      };
    },
    { total: 0, modules: 0, lessons: 0 },
  );
}

const KIND_TITLE_CLASS: Record<string, string> = {
  course: "text-xl font-semibold tracking-tight",
  module: "text-base font-semibold",
  lesson: "text-sm font-medium",
  segment: "text-sm font-medium text-muted-foreground",
  delivery_root: "text-base font-semibold",
  session: "text-sm font-medium",
};

const KIND_SHELL_CLASS: Record<string, string> = {
  course: "rounded-2xl border border-border bg-card p-5 ring-1 ring-foreground/5",
  module: "rounded-2xl border border-border bg-card p-4 ring-1 ring-foreground/5",
  lesson: "rounded-xl border border-border/70 bg-background p-3",
  segment: "rounded-xl bg-muted/30 p-3",
  delivery_root: "rounded-2xl border border-border bg-card p-4 ring-1 ring-foreground/5",
  session: "rounded-xl border border-border/70 bg-background p-3",
};

const DRAFT_STATUS_CLASS: Record<DraftStatus, string> = {
  queued: "border-border bg-muted text-muted-foreground",
  drafting: "border-primary/40 bg-primary/10 text-primary",
  ready: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  failed: "border-destructive/40 bg-destructive/10 text-destructive",
};

// `done` (or no `segment_status` at all — every segment older than this
// chip) renders nothing; only the two states worth a tutor's attention get a
// chip, same "chip per non-default state" idiom as `DRAFT_STATUS_CLASS`.
const SEGMENT_STATUS_CLASS: Record<Exclude<SegmentStatus, "done">, string> = {
  queued: "border-border bg-muted text-muted-foreground",
  failed: "border-destructive/40 bg-destructive/10 text-destructive",
};

/** How long the tutor has to stop typing before the draft goes to the API. Long
 * enough that a sentence is one PATCH, short enough that a phone call mid-edit
 * does not lose the paragraph. */
const AUTOSAVE_MS = 1500;

/** The three buttons over the textarea — the toolbar and the shortcuts are the
 * same table, so Ctrl+B and the B button can never drift apart. */
const MARK_BUTTONS = [
  { mark: "strong", key: "b", testId: "mark-bold", label: "markBold", Icon: Bold },
  { mark: "em", key: "i", testId: "mark-italic", label: "markItalic", Icon: Italic },
  { mark: "u", key: "u", testId: "mark-underline", label: "markUnderline", Icon: Underline },
] as const satisfies readonly { mark: MarkName; key: string; testId: string; label: string; Icon: typeof Bold }[];

interface BlockCardProps {
  node: BlockNode;
  locale: string;
  /** The title of the block that rendered this one, threaded one level down by
   * the recursion. A lesson needs it because «AI στο μάθημα» has to tell the
   * panel which MODULE it is working inside — the card has `node.title` but no
   * way to look upwards otherwise. */
  parentTitle?: string;
  isRoot?: boolean;
  index?: number;
  siblingCount?: number;
  onChanged: (node: BlockNode) => void;
  onRemoved: (id: string) => void;
  onRefresh?: () => void;
}

/** One node of the curriculum tree, rendered recursively.
 *
 * THE TUTOR-FRIENDLY REWRITE (Chris: "everything is expanded and a chaos"):
 *
 *  - COLLAPSED BY DEFAULT. Only the course root opens expanded, so a curriculum
 *    opens as a list of module rows — a table of contents, not a 45,000-word
 *    wall. A module opens to its lesson rows; a lesson opens to its full prose.
 *  - THE WHOLE ROW IS THE TOGGLE. Clicking anywhere on a header expands or
 *    collapses it — not just the 16px chevron. Buttons inside the row stop
 *    propagation, so actions never accidentally fold the tree.
 *  - RENAME IS AN ACTION, NOT A TITLE CLICK. Clicking a title used to open the
 *    rename input, which is why "click the row" could never work and why the
 *    tutor believed only titles were editable. Rename now lives in the ⋯ menu.
 *  - THE BODY IS EDITABLE. Every module objective, lesson objective and segment's
 *    prose gets an Edit button (`PATCH /blocks/{id}` has accepted `body` all
 *    along — the UI just never offered it).
 *  - THE ACTION CLUTTER IS FOLDED into one ⋯ menu per row (rename / move /
 *    add lesson / delete). What stays visible is what the tutor actually reads:
 *    tier badges, draft status, word counts — and Deepen, the one-click fix for
 *    a thin lesson.
 *
 * Artifacts arrive embedded in the tree (`node.artifacts`); ones attached THIS
 * session land in `added`, deduped against the embedded list — the board refetches
 * the whole tree on every draft-poll tick, and without the dedupe every attached
 * artifact rendered twice as soon as the refetch landed.
 */
export function BlockCard({
  node,
  locale,
  parentTitle,
  isRoot = false,
  index = 0,
  siblingCount = 1,
  onChanged,
  onRemoved,
  onRefresh,
}: BlockCardProps) {
  const t = useTranslations("curricula.tree");
  // Root-only: reused for the SAME 409 ("still drafting") copy
  // `CurriculumActionsMenu` shows for the header's ⋯ menu — one Greek/English
  // sentence for the one 409 the tutor is guaranteed to hit, whichever door
  // he deletes through.
  const tActions = useTranslations("curricula.actions");
  const confirm = useConfirm();

  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(node.title);
  // Collapsed by default — the root is the only node that opens expanded, so the
  // board reads as a table of contents.
  // NOTE FOR E2E AUTHORS: non-root nodes mount collapsed, so a test must expand
  // one (e.g. click its title) before asserting on lesson content beneath it.
  const [expanded, setExpanded] = useState(isRoot);
  const [editingBody, setEditingBody] = useState(false);
  const [draftBody, setDraftBody] = useState("");
  const [busy, setBusy] = useState(false);
  /** What the status line beside the toolbar says. Deliberately NOT `busy`:
   * `busy` disables the textarea, and an autosave that greys out the box the
   * tutor is typing in is worse than no autosave at all. */
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  /** The text the API is known to hold. Every save compares against it, which is
   * what makes a blur-then-Save (one click produces both) ONE PATCH. */
  const lastSavedRef = useRef(node.body ?? "");
  /** The text the editor opened with — what Cancel has to put back, since by
   * then an autosave may already have replaced it server-side. */
  const originalRef = useRef(node.body ?? "");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** The PATCH in flight, if any. Cancel and Save both await it: the debounce
   * can fire microseconds before a click, and the restore must land AFTER the
   * autosave it is undoing. */
  const inflightRef = useRef<Promise<void> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState<ArtifactOut[]>([]);
  /** The segment's own «Τι άλλαξε;» — local, because the chip and the dialog
   * are both this card's and nothing above it needs to know it is open. */
  const [tutorDiffOpen, setTutorDiffOpen] = useState(false);
  /** Module-only: «Προσθήκη μαθήματος». Local for the same reason — the ⋯ item
   * that opens it and the dialog it opens are both this card's. */
  const [addLessonOpen, setAddLessonOpen] = useState(false);

  const meta = node.meta ?? {};
  // null outside the curriculum board (no provider) — the module's
  // restructure item simply does not render there.
  const reviseScope = useReviseScope();
  // Same deal for the lesson row's «AI στο μάθημα»: no provider, no button.
  const lessonAi = useLessonAiScope();
  const isSegment = node.kind === "segment";
  const isLesson = node.kind === "lesson";
  const isModule = node.kind === "module";
  const reorderable = (isModule || isLesson) && siblingCount > 1;

  const contentChildren = node.children.filter((c) => c.plane === "content");
  const deliveryChildren = node.children.filter((c) => c.plane !== "content");
  const hasChildren = node.children.length > 0;
  const lessonCount = contentChildren.filter((c) => c.kind === "lesson").length;

  const embedded = node.artifacts ?? [];
  const artifacts = [
    ...added.filter((a) => !embedded.some((e) => e.id === a.id)),
    ...embedded,
  ];
  const draftStatus = meta.draft_status;
  const wordCount = meta.word_count;
  const segmentStatus = meta.segment_status;

  const kindKey = `kinds.${node.kind}`;
  const kindLabel = t.has(kindKey) ? t(kindKey) : node.kind;

  async function run(fn: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : fallback);
    } finally {
      setBusy(false);
    }
  }

  async function commitTitle() {
    const next = draftTitle.trim();
    setEditing(false);
    if (!next || next === node.title) {
      setDraftTitle(node.title);
      return;
    }
    await run(async () => {
      onChanged(await updateBlock(node.id, { title: next }));
    }, t("updateError"));
  }

  function startBodyEdit() {
    const current = node.body ?? "";
    setDraftBody(current);
    lastSavedRef.current = current;
    originalRef.current = current;
    setSaveState("idle");
    setEditingBody(true);
    setExpanded(true);
  }

  function clearAutosaveTimer() {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }

  // The pending debounce must not outlive the card — a tree refetch can unmount
  // a segment mid-edit, and a timer that fires afterwards PATCHes from a dead
  // component.
  useEffect(() => clearAutosaveTimer, []);

  /** Save without touching `busy` — the autosave's whole job is to be invisible. */
  async function saveQuiet(text: string) {
    if (text === lastSavedRef.current) return;
    setSaveState("saving");
    const pending = (async () => {
      try {
        const updated = await updateBlock(node.id, { body: text });
        lastSavedRef.current = text;
        onChanged(updated);
        setSaveState("saved");
      } catch (err) {
        setSaveState("error");
        setError(err instanceof ApiError ? err.detail : t("editBodyError"));
      }
    })();
    inflightRef.current = pending;
    try {
      await pending;
    } finally {
      if (inflightRef.current === pending) inflightRef.current = null;
    }
  }

  function scheduleAutosave(text: string) {
    clearAutosaveTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      void saveQuiet(text);
    }, AUTOSAVE_MS);
  }

  function applyMark(mark: MarkName) {
    const el = textareaRef.current;
    if (!el) return;
    const next = toggleMark(draftBody, el.selectionStart, el.selectionEnd, mark);
    setDraftBody(next.text);
    scheduleAutosave(next.text);
    // After React has written the new value: put the caret back where the tutor
    // left it (just inside the markers) and hand the box back.
    requestAnimationFrame(() => {
      const box = textareaRef.current;
      if (!box) return;
      box.focus();
      box.setSelectionRange(next.start, next.end);
    });
  }

  async function commitBody(e: FormEvent) {
    e.preventDefault();
    const next = draftBody;
    clearAutosaveTimer();
    setEditingBody(false);
    await run(async () => {
      // The same click already blurred the textarea, which may have started the
      // very PATCH this one would repeat; `saveQuiet` then sees nothing to do.
      if (inflightRef.current) await inflightRef.current;
      await saveQuiet(next);
    }, t("editBodyError"));
  }

  /** Cancel means "as it was when I opened this", which after an autosave is a
   * PATCH of its own rather than simply not saving. */
  async function cancelBodyEdit() {
    clearAutosaveTimer();
    const original = originalRef.current;
    setEditingBody(false);
    setSaveState("idle");
    await run(async () => {
      if (inflightRef.current) await inflightRef.current;
      if (lastSavedRef.current === original) return;
      lastSavedRef.current = original;
      onChanged(await updateBlock(node.id, { body: original }));
    }, t("editBodyError"));
  }

  async function handleDelete() {
    const counts = countSubtree(node.children);
    const ok = await confirm({
      title: isRoot
        ? t("confirmDelete.rootTitle", { title: node.title })
        : t("confirmDelete.title", { title: node.title }),
      body: (
        <>
          <p>{t("confirmDelete.lead", { title: node.title, kind: kindLabel })}</p>
          {counts.total > 0 && <p>{t("confirmDelete.counts", { ...counts })}</p>}
          <p className="font-medium text-destructive">
            {isRoot ? t("confirmDelete.rootWarning") : t("confirmDelete.irreversible")}
          </p>
        </>
      ),
      confirmLabel: t("confirmDelete.confirm"),
      destructive: true,
    });
    if (!ok) return;

    // ROOT CARD, NOT A GENERIC BLOCK (review fix — closes the "two-door"
    // seam): a course root deleted from HERE used to go straight through
    // `deleteBlock` -> `DELETE /blocks/{id}`, which has no drafting guard
    // and skips `delete_curriculum`'s cleanup of bound chat sessions,
    // interviews, and `GenerationJob.result_root_id` pointers
    // (`routers/curriculum.py`). The header's ⋯ menu
    // (`CurriculumActionsMenu`) already deletes roots the right way; this
    // card must go through the exact same `deleteCurriculum()` call, not a
    // second, incomplete implementation of the same action. Not routed
    // through the generic `run()` helper below because the 409 needs its
    // own localized copy, same as `CurriculumActionsMenu.handleDelete`.
    if (isRoot) {
      setBusy(true);
      setError(null);
      try {
        await deleteCurriculum(node.id);
        onRemoved(node.id);
      } catch (err) {
        setError(
          err instanceof ApiError
            ? (err.status === 409 ? tActions("deleteDrafting") : err.detail)
            : t("deleteError"),
        );
      } finally {
        setBusy(false);
      }
      return;
    }

    await run(async () => {
      await deleteBlock(node.id);
      onRemoved(node.id);
    }, t("deleteError"));
  }

  async function handleReorder(direction: "up" | "down") {
    await run(async () => {
      await reorderBlock(node.id, direction);
      onRefresh?.();
    }, t("reorderError"));
  }

  async function handleDeepen() {
    await run(async () => {
      await deepenLesson(node.id);
      onChanged({ ...node, meta: { ...meta, draft_status: "queued", error: null } });
    }, t("deepenError"));
  }

  function toggle() {
    if (hasChildren && !editing) setExpanded((v) => !v);
  }

  const header = (
    <div
      className={cn(
        "flex min-w-0 flex-wrap items-center gap-2",
        // THE WHOLE ROW IS THE TOGGLE — with a hover tint so it reads as
        // clickable before the first click.
        hasChildren && "-m-1.5 cursor-pointer rounded-lg p-1.5 transition-colors hover:bg-muted/50",
      )}
      data-testid="block-card-header"
      onClick={toggle}
    >
      {hasChildren ? (
        <CollapsibleTrigger
          aria-label={expanded ? t("collapse") : t("expand")}
          data-testid="block-card-toggle"
          className="shrink-0 text-muted-foreground hover:text-foreground"
          onClick={(e) => e.stopPropagation()}
        >
          {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </CollapsibleTrigger>
      ) : (
        <span className="inline-block size-4 shrink-0" />
      )}

      {editing ? (
        <Input
          autoFocus
          value={draftTitle}
          onChange={(e) => setDraftTitle(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onBlur={commitTitle}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitTitle();
            } else if (e.key === "Escape") {
              setDraftTitle(node.title);
              setEditing(false);
            }
          }}
          placeholder={t("editTitlePlaceholder")}
          data-testid="block-card-title-input"
          className="h-8 min-w-0 flex-1"
        />
      ) : (
        <span
          data-testid="block-card-title"
          // From `sm` up this truncates (see the class below), and a module
          // called «Από το πετάλι στον ενισχυτή: αλυσίδα σήματος και τελικ…»
          // was unreadable past the ellipsis with no way to see the rest.
          // Native tooltip, same as the segment-error chip below — no portal to
          // position, which matters on the desktop build.
          title={node.title}
          // Mobile: the title WRAPS and the badges drop below it — every chip in
          // this row is shrink-0, so a truncating title was the only thing that
          // could yield and it collapsed to nothing. The 12rem flex-BASIS is the
          // load-bearing part: `flex-1` means basis 0%, and a zero-basis item
          // never claims a line in a flex-wrap row — the badges stayed put and
          // the title wrapped one character per column. With a real basis the
          // chips can't fit beside it on a phone and wrap below instead. From
          // `sm` up there is room for one line, so it truncates as before.
          className={cn(
            "min-w-0 flex-[1_1_12rem] break-words text-left sm:flex-1 sm:truncate",
            KIND_TITLE_CLASS[node.kind] ?? "text-sm font-medium",
          )}
        >
          {node.title}
        </span>
      )}

      {isModule && (meta.tier !== "library" || meta.coverage_note) && (
        <TierBadge tier={meta.tier} coverageNote={meta.coverage_note} />
      )}

      {isModule && !expanded && lessonCount > 0 && (
        <Badge variant="outline" data-testid="module-lesson-count">
          {t("lessonCount", { count: lessonCount })}
        </Badge>
      )}

      {/* NO PILL FOR «Έτοιμο». "Ready" is the resting state of every lesson on
          a finished board, so a pill for it was a green sticker on every row —
          decoration the eye learns to skip, which is exactly what makes the
          `queued`/`drafting`/`failed` pills beside it worth seeing. The pill
          now means "this row is NOT settled yet". */}
      {isLesson && draftStatus && draftStatus !== "ready" && (
        <span
          data-testid="lesson-status"
          data-status={draftStatus}
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
            DRAFT_STATUS_CLASS[draftStatus] ?? DRAFT_STATUS_CLASS.queued,
          )}
        >
          {draftStatus === "drafting" && <Loader2 className="size-3 animate-spin" aria-hidden />}
          {t.has(`draftStatus.${draftStatus}`) ? t(`draftStatus.${draftStatus}`) : draftStatus}
        </span>
      )}

      {isSegment && (segmentStatus === "queued" || segmentStatus === "failed") && (
        <span
          data-testid="segment-status"
          data-status={segmentStatus}
          title={segmentStatus === "failed" ? meta.segment_error ?? undefined : undefined}
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
            SEGMENT_STATUS_CLASS[segmentStatus],
          )}
        >
          {t(`segmentStatus.${segmentStatus}`)}
        </span>
      )}

      {/* The word count used to sit here as a badge. It moved INTO the opened
          lesson (`lesson-words-line`), next to its target — a bare "2.340" on a
          collapsed row is a number with nothing to be measured against, and the
          row's job is to be scannable. */}

      {!isLesson && node.est_minutes != null && (
        <Badge variant="outline" data-testid="block-card-minutes">
          {t("estMinutes", { count: node.est_minutes })}
        </Badge>
      )}

      <div
        className="ml-auto flex shrink-0 items-center gap-1"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ONE DOOR for everything AI on a lesson. Disabled while the lesson
            is still being written — there is nothing to talk about yet, and
            the panel's actions all rewrite text a worker is mid-way through. */}
        {isLesson && lessonAi && (
          <Button
            type="button" variant="ghost" size="sm"
            data-testid="lesson-ai"
            disabled={draftStatus === "drafting" || draftStatus === "queued"}
            onClick={() => lessonAi.openForLesson(node.id, node.title, parentTitle ?? "")}
          >
            <Sparkles />
            {t("lessonAi")}
          </Button>
        )}

        {isLesson && (
          <Button
            type="button" variant="ghost" size="sm"
            data-testid="lesson-deepen"
            disabled={busy || draftStatus === "drafting"}
            onClick={handleDeepen}
          >
            {busy ? <Loader2 className="animate-spin" /> : <Sparkles />}
            {t("deepen")}
          </Button>
        )}

        {/* THE MODULE'S OWN DOOR. It used to live only inside the ⋯ menu as
            «Αναδιάρθρωση με AI» — a real capability hidden behind a click that
            gives no hint it is there. This is the same call
            (`reviseScope.openForModule`), just visible, like the lesson row's
            `lesson-ai` button. The menu item stays — muscle memory, and the
            drawer opens identically either way. */}
        {isModule && reviseScope && (
          <Button
            type="button" variant="ghost" size="sm"
            data-testid="module-ai"
            onClick={() =>
              reviseScope.openForModule(
                node.id,
                node.title,
                t("restructureSeed", { title: node.title, id: node.id }),
              )
            }
          >
            <Sparkles />
            {t("moduleAi")}
          </Button>
        )}

        {node.kind === "course" && (
          <SegmentDialog blockId={node.id} blockTitle={node.title} onSegmented={() => onRefresh?.()} />
        )}
        {isSegment && (
          <AttachArtifactDialog
            blockId={node.id}
            blockTitle={node.title}
            onAttached={(artifact) => setAdded((prev) => [artifact, ...prev])}
          />
        )}

        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                data-testid="block-card-menu"
                aria-label={t("moreActions")}
                disabled={busy}
              />
            }
          >
            {busy ? <Loader2 className="animate-spin" /> : <MoreVertical />}
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            {/* ROOT CARD HAS NO INLINE RENAME (review fix — closes the other
             * half of the "two-door" seam). `commitTitle` below calls
             * `updateBlock` -> `PATCH /blocks/{id}` directly, which never
             * reaches the detail page's own `title` state (the page owns a
             * SEPARATE copy for the header, synced only through
             * `CurriculumActionsMenu`'s `onRenamed` — see that page's own
             * docstring on why `TreeBoard` never syncs a prop into its
             * state). A rename here would silently save while the header
             * beside it kept showing the old name until the next full
             * navigation. No clean callback channel exists from this card up
             * to the page today, and adding one is a bigger change than this
             * fix warrants — so the smaller, cleaner fix is: the header's ⋯
             * menu (`CurriculumActionsMenu`) is the one rename door for a
             * course root, full stop. Every other kind keeps this item. */}
            {!isRoot && (
              <DropdownMenuItem
                data-testid="menu-rename"
                onClick={() => {
                  setDraftTitle(node.title);
                  setEditing(true);
                }}
              >
                <Pencil />
                {t("rename")}
              </DropdownMenuItem>
            )}
            {reorderable && (
              <>
                <DropdownMenuItem
                  data-testid="menu-move-up"
                  disabled={index === 0}
                  onClick={() => handleReorder("up")}
                >
                  <ChevronUp />
                  {t("moveUp")}
                </DropdownMenuItem>
                <DropdownMenuItem
                  data-testid="menu-move-down"
                  disabled={index === siblingCount - 1}
                  onClick={() => handleReorder("down")}
                >
                  <ChevronDown />
                  {t("moveDown")}
                </DropdownMenuItem>
              </>
            )}
            {/* IT ASKS FIRST NOW. This used to POST a blank lesson called
                «Νέο μάθημα» on click and leave the whole thing to be written
                by hand; it opens the dialog, which asks what the lesson should
                teach and hands that to the planner. The blank box is still one
                click away, inside. */}
            {isModule && (
              <DropdownMenuItem
                data-testid="menu-add-lesson"
                onClick={() => setAddLessonOpen(true)}
              >
                <Plus />
                {t("addLesson")}
              </DropdownMenuItem>
            )}
            {/* THE DOOR CHRIS WAS LOOKING FOR. "Extend with AI" on a module
                only ever rewrote the module's own description, because it is
                `refine_block` — a TEXT tool that never sees a module's
                children. This is the structural one: it opens the revise
                drawer already pointed at this module, so "5 lessons instead of
                3" has somewhere to be said. Rendered only inside a
                `ReviseScopeContext` provider, i.e. on the curriculum board. */}
            {isModule && reviseScope && (
              <DropdownMenuItem
                data-testid="menu-restructure-ai"
                onClick={() =>
                  reviseScope.openForModule(
                    node.id,
                    node.title,
                    // The id goes into the MESSAGE, not just into state: this
                    // drawer drives a chat, so the planner is reached through a
                    // tool call and the model can only pass an id it can see.
                    t("restructureSeed", { title: node.title, id: node.id }),
                  )
                }
              >
                <Layers />
                {t("restructureWithAi")}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem destructive data-testid="menu-delete" onClick={handleDelete}>
              <Trash2 />
              {t("delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );

  const canEditBody = isSegment || isModule || isLesson;

  const body = (
    <div className="flex flex-col gap-3">
      {editingBody ? (
        <form onSubmit={commitBody} className="flex flex-col gap-2" data-testid="body-edit-form">
          {/* THE THREE BUTTONS. `onMouseDown` is prevented on every one of them:
              without it the mousedown blurs the textarea, the selection the
              tutor just made is gone by the time the click handler reads it,
              and the blur-flush fires a PATCH for a Ctrl+B. */}
          <div className="flex items-center gap-1" data-testid="body-edit-toolbar">
            {MARK_BUTTONS.map(({ mark, testId, label, Icon }) => (
              <Button
                key={mark}
                type="button" size="icon-sm" variant="ghost"
                data-testid={testId}
                aria-label={t(label)}
                title={t(label)}
                disabled={busy}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => applyMark(mark)}
              >
                <Icon />
              </Button>
            ))}
            {saveState !== "idle" && saveState !== "error" && (
              <span
                data-testid="body-edit-status"
                data-state={saveState}
                className="ml-1 text-xs text-muted-foreground"
              >
                {saveState === "saving" ? t("saving") : t("saved")}
              </span>
            )}
          </div>
          <Textarea
            autoFocus
            ref={textareaRef}
            value={draftBody}
            onChange={(e) => {
              setDraftBody(e.target.value);
              scheduleAutosave(e.target.value);
            }}
            // Clicking away is a pause the tutor meant — flush now rather than
            // leave 1.5s of typing hanging on a timer he cannot see.
            onBlur={() => {
              clearAutosaveTimer();
              void saveQuiet(draftBody);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                void cancelBodyEdit();
                return;
              }
              if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
              const hit = MARK_BUTTONS.find((b) => b.key === e.key.toLowerCase());
              if (!hit) return;
              e.preventDefault();
              applyMark(hit.mark);
            }}
            rows={Math.min(18, Math.max(4, draftBody.split("\n").length + 1))}
            data-testid="body-edit-textarea"
            disabled={busy}
            className={cn(isSegment && "text-sm leading-relaxed")}
          />
          <div className="flex items-center gap-2 self-end">
            <Button
              type="button" size="sm" variant="outline"
              data-testid="body-edit-cancel"
              disabled={busy}
              onClick={() => void cancelBodyEdit()}
            >
              {t("cancel")}
            </Button>
            <Button type="submit" size="sm" disabled={busy} data-testid="body-edit-save">
              {busy && <Loader2 className="animate-spin" />}
              {t("editSave")}
            </Button>
          </div>
        </form>
      ) : (
        node.body && (
          <p
            data-testid="block-card-body"
            className={cn(
              "whitespace-pre-wrap",
              isSegment ? "text-sm leading-relaxed" : "text-xs text-muted-foreground",
            )}
          >
            <InlineMarks text={node.body} />
          </p>
        )
      )}

      {/* THE COUNT, WHERE IT MEANS SOMETHING. Inside the opened lesson and
          beside its target, so "2.340 λέξεις · στόχος ~2.750" is a judgement
          the tutor can make at a glance instead of a bare number on a row. It
          re-renders from `meta.word_count` on every tree refetch, so it tracks
          a deepen or a revise without a reload. Amber when the lesson is under
          its floor — the one case where the number is asking for something. */}
      {isLesson && typeof wordCount === "number" && (
        <p
          data-testid="lesson-words-line"
          className={cn(
            "text-xs text-muted-foreground",
            meta.meets_floor === false && "text-amber-600 dark:text-amber-400",
          )}
        >
          {/* NO TARGET, NO «στόχος». `meta.target_words` is written by the
              drafter, so a lesson that has never been drafted (or one imported
              from chat) has a word count and no target — and `?? 0` printed
              "στόχος ~0", a judgement that is both wrong and impossible to
              meet. The half of the line that has no data simply is not said. */}
          {typeof meta.target_words === "number" && meta.target_words > 0
            ? t("wordsLine", { count: wordCount, target: meta.target_words })
            : t("wordsLineNoTarget", { count: wordCount })}
        </p>
      )}

      {isLesson && !editingBody && (
        <LessonSources citations={meta.citations} locale={locale} lessonTitle={node.title} />
      )}

      {isLesson && meta.error && (
        <p role="alert" data-testid="lesson-error" className="text-xs text-destructive">
          {meta.error}
        </p>
      )}

      {artifacts.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="segment-artifacts">
          <span className="text-xs font-medium text-muted-foreground">
            {t("attachedArtifactsHeading")}
          </span>
          <div className="flex flex-wrap gap-3" data-testid="segment-artifact-list">
            {artifacts.map((artifact) => (
              <div key={artifact.id} data-testid="segment-artifact-item">
                <Artifact kind={artifact.kind} spec={artifact.spec} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* A LESSON'S OWN «Τι άλλαξε;», which is a different mechanism from the
          segment-level one below and needs its own control. `modify_lesson`
          does not edit text — it requeues and a worker rewrites every segment
          from scratch — so what changed is the whole SET, and the two sides
          come from `meta.prev_segments` and the live segment children rather
          than from one block's `prev_body`. This is also the only place a
          RESTORE is offered: a segment already has Undo, a lesson has nothing
          else. */}
      {isLesson && Array.isArray(meta.prev_segments) && (
        <LessonWhatChanged
          lessonId={node.id}
          prevSegments={meta.prev_segments}
          liveSegments={contentChildren.filter((c) => c.kind === "segment")}
          // Two writers, one question. The revise engine stamps
          // `revise_instruction`; the lesson panel's apply stamps
          // `ai_instruction` (display-only, deliberately a separate key — see
          // `BlockMeta`). Whichever touched this lesson last, the diff still
          // opens with what was actually asked for.
          instruction={meta.ai_instruction ?? meta.revise_instruction}
          onRestored={onChanged}
        />
      )}

      {!editingBody && (canEditBody || isSegment || isModule || (isLesson && draftStatus === "ready")) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {canEditBody && (
            <Button
              type="button" size="sm" variant="ghost"
              data-testid="body-edit-trigger"
              disabled={busy}
              onClick={startBodyEdit}
            >
              <Pencil />
              {t("editContent")}
            </Button>
          )}
          {(isSegment || isModule || (isLesson && draftStatus === "ready")) && (
            <ExtendWithChat
              blockId={node.id}
              canUndo={Boolean(meta.prev_body)}
              prevBody={meta.prev_body}
              body={node.body}
              instruction={meta.refine_instruction}
              onRefined={onChanged}
            />
          )}
          {/* «Επεξεργασμένο από σένα». The tutor's OWN edit, admitted by the
              row and diffable — the AI's rewrites have said what they changed
              since the diff panel landed, and his own hand was the one change
              on the board with no record at all. `prev_body` here is the one
              the API stamped at save time, so the answer survives the session
              in which he would still have remembered. */}
          {isSegment && meta.tutor_edited && (
            <>
              <Button
                type="button" size="sm" variant="ghost"
                data-testid="segment-tutor-edited"
                onClick={() => setTutorDiffOpen(true)}
              >
                <Pencil />
                {t("tutorEdited")}
              </Button>
              <WhatChanged
                open={tutorDiffOpen}
                onOpenChange={setTutorDiffOpen}
                before={meta.tutor_edited.prev_body}
                after={node.body ?? ""}
              />
            </>
          )}
        </div>
      )}

      {error && (
        <p role="alert" data-testid="block-card-error" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );

  /** RENDERED OUTSIDE THE COLLAPSIBLE, deliberately: a module with lessons
   * mounts collapsed, and a dialog living inside `CollapsibleContent` would be
   * unmounted at the exact moment its own ⋯ item is reachable. */
  const addLessonDialog = isModule ? (
    <AddLessonDialog
      moduleId={node.id}
      moduleTitle={node.title}
      siblings={contentChildren
        .filter((c) => c.kind === "lesson")
        .map((c) => ({ id: c.id, title: c.title }))}
      open={addLessonOpen}
      onOpenChange={setAddLessonOpen}
      onAdded={() => onRefresh?.()}
    />
  ) : null;

  const children = (
    <>
      {contentChildren.length > 0 && (
        <div
          className={cn(
            "flex flex-col",
            node.kind === "course" ? "gap-4" : node.kind === "module" ? "gap-3" : "gap-2",
            node.kind !== "course" && "border-l border-border pl-4",
          )}
          data-testid="block-card-children"
        >
          {contentChildren.map((child, i) => (
            <BlockCard
              key={child.id}
              node={child}
              locale={locale}
              parentTitle={node.title}
              index={i}
              siblingCount={contentChildren.length}
              onChanged={onChanged}
              onRemoved={onRemoved}
              onRefresh={onRefresh}
            />
          ))}
        </div>
      )}

      {deliveryChildren.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">{t("deliveryHeading")}</span>
          <div className="flex flex-col gap-2 border-l border-border pl-4" data-testid="block-card-delivery">
            {deliveryChildren.map((child, i) => (
              <BlockCard
                key={child.id}
                node={child}
                locale={locale}
                parentTitle={node.title}
                index={i}
                siblingCount={deliveryChildren.length}
                onChanged={onChanged}
                onRemoved={onRemoved}
                onRefresh={onRefresh}
              />
            ))}
          </div>
        </div>
      )}
    </>
  );

  const shell = cn(
    "flex min-w-0 flex-col gap-3",
    KIND_SHELL_CLASS[node.kind] ?? "rounded-xl border border-border/70 bg-background p-3",
  );

  if (!hasChildren) {
    return (
      <section className={shell} data-testid="block-card" data-kind={node.kind}>
        {header}
        {body}
        {addLessonDialog}
      </section>
    );
  }

  return (
    <section className={shell} data-testid="block-card" data-kind={node.kind}>
      <Collapsible open={expanded} onOpenChange={setExpanded}>
        {header}
        <CollapsibleContent className="overflow-hidden transition-[height] duration-200 ease-out">
          <div className="flex flex-col gap-4 pt-3">
            {body}
            {children}
          </div>
        </CollapsibleContent>
      </Collapsible>
      {addLessonDialog}
    </section>
  );
}
