"use client";

import { useCallback, useMemo, useState, useSyncExternalStore, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, BookOpen, ChevronDown, ChevronUp, Loader2, Plus, RefreshCw, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { BlockCard } from "@/components/curriculum/block-card";
import { DraftProgressBar } from "@/components/curriculum/draft-progress-bar";
import {
  ApiError,
  addModule,
  generateModule,
  getCurriculum,
  getJob,
  redraftCurriculum,
  type BlockNode,
} from "@/lib/api";

interface TreeBoardProps {
  root: BlockNode;
  locale: string;
  onRootDeleted: () => void;
}

/** Replace one node anywhere in the tree, immutably. Returns the SAME object when
 * nothing matched, so React skips re-rendering every untouched branch — which
 * matters here: a 20-lesson curriculum is ~120 blocks and a draft poll lands every
 * 2 seconds. */
function replaceNode(node: BlockNode, next: BlockNode): BlockNode {
  if (node.id === next.id) return next;
  let changed = false;
  const children = node.children.map((child) => {
    const updated = replaceNode(child, next);
    if (updated !== child) changed = true;
    return updated;
  });
  return changed ? { ...node, children } : node;
}

function removeNode(node: BlockNode, id: string): BlockNode {
  const kept = node.children.filter((c) => c.id !== id);
  const children = kept.map((c) => removeNode(c, id));
  const changed =
    kept.length !== node.children.length || children.some((c, i) => c !== kept[i]);
  return changed ? { ...node, children } : node;
}

function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

// The add-module planning call reads the whole library — 20-60s in the normal
// case. Six minutes is the same ceiling the interview's outline poll uses.
const MODULE_POLL_INTERVAL_MS = 2000;
const MODULE_POLL_DEADLINE_MS = 6 * 60_000;

/** Where «Λεπτομέρειες» remembers itself. Per-browser, not per-curriculum: the
 * question it answers ("do I want to see numbers on a board?") is about the
 * tutor, not about one course. */
const DETAILS_STORAGE_KEY = "curricula.board.detailsOpen";

/** localStorage read as an EXTERNAL STORE rather than as `useState` + an
 * effect that seeds it. Both are hydration-safe — the server snapshot is
 * `false`, the same closed board the server rendered — but `useSyncExternalStore`
 * is the one that does not schedule a synchronous setState inside an effect,
 * which is a cascading render and which this repo's lint rules reject outright
 * (`react-hooks/set-state-in-effect`). The subscription also means every board
 * on the page agrees about the toggle without any of them owning it. */
const detailsListeners = new Set<() => void>();

function subscribeDetailsOpen(onChange: () => void) {
  detailsListeners.add(onChange);
  return () => {
    detailsListeners.delete(onChange);
  };
}

function readDetailsOpen(): boolean {
  try {
    return window.localStorage.getItem(DETAILS_STORAGE_KEY) === "true";
  } catch {
    // A browser with site data blocked. The board is not the place to complain
    // about it — it stays closed, which is the default anyway.
    return false;
  }
}

function writeDetailsOpen(next: boolean) {
  try {
    window.localStorage.setItem(DETAILS_STORAGE_KEY, next ? "true" : "false");
  } catch {
    // Same as above: the toggle still works for this session.
  }
  detailsListeners.forEach((fn) => fn());
}

/** THE BOARD. A `max-w-4xl` reading column that OWNS the tree; every mutation
 * flows back up to it, and the draft-progress poll refetches into it.
 *
 * It was `max-w-3xl`, sized for 12-14px body text. The type inside it is 16px
 * on a 17px root now (readability round, 2026-09-15), so the same column was
 * holding noticeably fewer words per line and every lesson row wrapped harder.
 * `4xl` puts the line length back where it was — a wider column carrying
 * bigger type, not more of it.
 *
 * "ADD A MODULE" IS AN AI ACTION NOW. The button opens a one-line form: an
 * optional topic ("πετάλια και εφέ"), and Generate. The API plans ONE module that
 * fits the existing course (same full-library call the outline used), lands it
 * with its lessons `queued`, and chains the ordinary draft fan-out — so the new
 * module fills in exactly the way the original curriculum did, progress bar and
 * all. The old create-an-empty-box behaviour survives as the quiet secondary
 * button, because sometimes the tutor just wants a container.
 */
export function TreeBoard({ root, locale, onRootDeleted }: TreeBoardProps) {
  const t = useTranslations("curricula.tree");
  const confirm = useConfirm();

  const [tree, setTree] = useState<BlockNode>(root);
  const [addOpen, setAddOpen] = useState(false);
  const [topic, setTopic] = useState("");
  const [adding, setAdding] = useState(false);       // the plain empty-module POST
  const [generating, setGenerating] = useState(false); // the AI job, enqueue → done
  const [redrafting, setRedrafting] = useState(false);
  const [redraftError, setRedraftError] = useState<string | null>(null);
  // Closed on the server, and closed on the hydrating render too (that is what
  // the `false` server snapshot is for) — then React swaps in what this browser
  // actually remembers. Reading localStorage during render instead would make
  // the server's HTML and the client's first pass disagree, which React calls
  // a hydration error.
  const detailsOpen = useSyncExternalStore(subscribeDetailsOpen, readDetailsOpen, () => false);
  const [error, setError] = useState<string | null>(null);

  function toggleDetails() {
    writeDetailsOpen(!detailsOpen);
  }

  /** Refetch the whole tree. Returns whether it landed — the progress bar's poll
   * uses that to decide if its baseline may advance (a failed refetch is retried
   * on the next tick rather than silently skipped, which mattered most on the
   * FINAL tick of a draft run). */
  const refresh = useCallback(async (): Promise<boolean> => {
    try {
      setTree(await getCurriculum(root.id));
      return true;
    } catch {
      return false;
    }
  }, [root.id]);

  const handleChanged = useCallback((next: BlockNode) => {
    setTree((prev) => replaceNode(prev, next));
  }, []);

  const handleRemoved = useCallback(
    (id: string) => {
      if (id === root.id) {
        onRootDeleted();
        return;
      }
      setTree((prev) => removeNode(prev, id));
    },
    [root.id, onRootDeleted],
  );

  async function handleGenerateModule(e: FormEvent) {
    e.preventDefault();
    setGenerating(true);
    setError(null);
    try {
      const accepted = await generateModule(tree.id, { topic: topic.trim() || null });
      const deadline = performance.now() + MODULE_POLL_DEADLINE_MS;
      while (performance.now() < deadline) {
        await sleep(MODULE_POLL_INTERVAL_MS);
        const job = await getJob(accepted.job_id);
        if (job.status === "succeeded") {
          // The module + its queued lessons exist; the chained draft fan-out is
          // already writing them. The refetched tree's rising `queued` count
          // re-arms the progress bar's poll loop.
          await refresh();
          setTopic("");
          setAddOpen(false);
          return;
        }
        if (job.status === "failed") {
          setError(job.error ?? t("addModuleError"));
          return;
        }
      }
      setError(t("addModuleTimeout"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("addModuleError"));
    } finally {
      setGenerating(false);
    }
  }

  async function handleAddEmptyModule() {
    setAdding(true);
    setError(null);
    try {
      await addModule(tree.id, { title: t("newModuleTitle") });
      await refresh();
      setAddOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("addModuleError"));
    } finally {
      setAdding(false);
    }
  }

  // What the board is ALREADY showing — the progress bar's baseline (ready) and
  // its wake-up signals (queued/drafting/failed: ANY of these changing re-arms
  // a parked poll loop — see `draft-progress-bar.tsx`'s own docstring on why
  // `ready` rising alone used to be the only trigger, and why that left a
  // Resume click (failed -> queued, never touching `ready`) invisible until a
  // reload).
  const readyInTree = tree.children.reduce(
    (n, module) =>
      n + module.children.filter((lesson) => lesson.meta?.draft_status === "ready").length,
    0,
  );
  const queuedInTree = tree.children.reduce(
    (n, module) =>
      n + module.children.filter((lesson) => lesson.meta?.draft_status === "queued").length,
    0,
  );
  const draftingInTree = tree.children.reduce(
    (n, module) =>
      n + module.children.filter((lesson) => lesson.meta?.draft_status === "drafting").length,
    0,
  );
  const failedInTree = tree.children.reduce(
    (n, module) =>
      n + module.children.filter((lesson) => lesson.meta?.draft_status === "failed").length,
    0,
  );

  // NON-GAP lessons only — a gap module has none by construction (`outline.py`:
  // "nothing to draft, no call is made"), so this is naturally every lesson a
  // redraft would actually touch. Computed here, client-side, purely so the
  // confirm dialog can NAME the count before he commits to it — the server is
  // the one that actually decides who gets requeued.
  const redraftableCount = useMemo(
    () =>
      tree.children.reduce(
        (n, module) => (module.meta?.tier === "gap" ? n : n + module.children.length),
        0,
      ),
    [tree],
  );

  async function handleRedraft() {
    const ok = await confirm({
      title: t("redraftConfirmTitle"),
      body: t("redraftConfirmBody", { count: redraftableCount }),
      confirmLabel: t("redraftConfirm"),
      destructive: true,
    });
    if (!ok) return;

    setRedrafting(true);
    setRedraftError(null);
    try {
      await redraftCurriculum(tree.id);
      await refresh();
    } catch (err) {
      setRedraftError(err instanceof ApiError ? err.detail : t("redraftError"));
    } finally {
      setRedrafting(false);
    }
  }

  const library = tree.meta?.library;
  const shape = tree.meta?.shape;
  const degraded = library != null && library.full_context === false;

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6" data-testid="tree-board">
      <header className="flex flex-col gap-3">
        {library && (
          <div
            data-testid="library-banner"
            data-degraded={degraded ? "true" : "false"}
            className={
              degraded
                ? "flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300"
                : "flex items-start gap-2 rounded-xl border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
            }
          >
            {degraded ? (
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            ) : (
              <BookOpen className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            )}
            <span>
              {degraded
                ? t("libraryDegraded", { tokens: library.token_count ?? 0 })
                : t("libraryWhole", {
                    sources: library.sources?.length ?? 0,
                    tokens: library.token_count ?? 0,
                  })}
            </span>
          </div>
        )}

        {/* WHERE THE TEXT CAME FROM, ALWAYS. THE NUMBERS, ON REQUEST.
            The library line above is the one sentence on this header worth
            reading every time — it is the answer to "is this mine or did the
            machine make it up", so it stays, and at `text-sm` rather than the
            squint-size it used to be. Everything else here is arithmetic: the
            per-lesson word target, and the draft tallies once the draft is
            over. They were appended to that sentence and bolted under it,
            which turned the answer into a dashboard. They live behind this
            toggle now, and the toggle remembers itself. */}
        <div className="flex items-center">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="board-details-toggle"
            aria-expanded={detailsOpen}
            onClick={toggleDetails}
          >
            {detailsOpen ? <ChevronUp /> : <ChevronDown />}
            {t("details")}
          </Button>
        </div>

        {detailsOpen && shape?.target_words_per_lesson ? (
          <p data-testid="board-shape-words" className="text-sm text-muted-foreground">
            {t("shapeWords", { words: shape.target_words_per_lesson })}
          </p>
        ) : null}

        <DraftProgressBar
          rootId={tree.id}
          readyInTree={readyInTree}
          queuedInTree={queuedInTree}
          draftingInTree={draftingInTree}
          failedInTree={failedInTree}
          onLessonReady={refresh}
          detailsOpen={detailsOpen}
        />
      </header>

      <BlockCard
        key={tree.id}
        node={tree}
        locale={locale}
        isRoot
        onChanged={handleChanged}
        onRemoved={handleRemoved}
        onRefresh={refresh}
      />

      <footer className="flex flex-col gap-2">
        {!addOpen ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="self-start"
            data-testid="board-add-module"
            onClick={() => {
              setAddOpen(true);
              setError(null);
            }}
          >
            <Sparkles />
            {t("addModule")}
          </Button>
        ) : (
          <form
            onSubmit={handleGenerateModule}
            className="flex flex-col gap-2 rounded-2xl border border-border bg-card p-3 ring-1 ring-foreground/5"
            data-testid="board-add-module-form"
          >
            <Input
              autoFocus
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder={t("addModuleTopicPlaceholder")}
              data-testid="add-module-topic"
              disabled={generating || adding}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit" size="sm" disabled={generating || adding} data-testid="add-module-generate">
                {generating ? <Loader2 className="animate-spin" /> : <Sparkles />}
                {t("addModuleGenerate")}
              </Button>
              <Button
                type="button" size="sm" variant="ghost"
                data-testid="add-module-empty"
                disabled={generating || adding}
                onClick={handleAddEmptyModule}
              >
                {adding ? <Loader2 className="animate-spin" /> : <Plus />}
                {t("addModuleEmpty")}
              </Button>
              <Button
                type="button" size="sm" variant="ghost"
                className="ml-auto"
                disabled={generating}
                onClick={() => setAddOpen(false)}
              >
                {t("cancel")}
              </Button>
            </div>
            {generating && (
              <p className="text-xs text-muted-foreground" data-testid="add-module-working">
                {t("addModuleWorking")}
              </p>
            )}
          </form>
        )}
        {error && (
          <p role="alert" data-testid="board-add-error" className="text-xs text-destructive">
            {error}
          </p>
        )}

        {/* OPT-IN, EXPLICIT, CONFIRM-GATED (Plan C, Task 8). This is the ONLY
            path that rewrites lessons that already drafted — it never fires as
            a side effect of a blueprint edit (invariant #8). Hidden entirely
            once there is nothing it could touch (every lesson lives under a
            gap module, or the curriculum has none yet). */}
        {redraftableCount > 0 && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="self-start"
            data-testid="board-redraft"
            disabled={redrafting}
            onClick={handleRedraft}
          >
            {redrafting ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t("redraft")}
          </Button>
        )}
        {redraftError && (
          <p role="alert" data-testid="board-redraft-error" className="text-xs text-destructive">
            {redraftError}
          </p>
        )}
      </footer>
    </div>
  );
}
