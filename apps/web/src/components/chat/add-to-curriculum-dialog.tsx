"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { BookmarkPlus, Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  addLessonFromChat,
  getCurriculum,
  listCurricula,
  type BlockNode,
  type ChatCitation,
  type CurriculumListItem,
} from "@/lib/api";

const SELECT_CLASS =
  "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 text-base outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:bg-input/30";

interface AddToCurriculumDialogProps {
  content: string;
  citations?: ChatCitation[] | null;
  sessionId?: string;
  /** The user message this answer replied to — the best default lesson title
   * there is ("Ασκήσεις για δύναμη δαχτύλων" beats the answer's own first
   * line, which for library-gap answers is a general-knowledge disclaimer). */
  question?: string;
}

/** A sensible default lesson title: the tutor's own question when we have it,
 * else the answer's first real line, stripped of markdown chrome, clamped.
 * The tutor edits it in the dialog — this is a starting point, not a decision. */
function deriveTitle(content: string, question?: string): string {
  const q = (question ?? "").replace(/\s+/g, " ").trim();
  if (q) return q.replace(/[;?·]+\s*$/, "").slice(0, 120);
  const line = content
    .split("\n")
    .map((l) => l.replace(/^[#>\-*\s]+/, "").replace(/\*\*/g, "").trim())
    .find((l) => l.length > 0);
  return (line ?? "").slice(0, 120);
}

/** "ADD THIS TO A CURRICULUM" — under every assistant answer worth teaching.
 *
 * The targets are REAL rows out of the database (`listCurricula` → a `<select>`,
 * then that curriculum's own modules → a second `<select>`): the model is
 * nowhere near this path, so there is nothing to hallucinate — the exact
 * requirement. Submitting stores the answer VERBATIM as a one-segment lesson
 * (`POST /blocks/{module}/lessons/from-chat`, no LLM call), carrying the chat
 * turn's citations into the board's provenance chips. The board's Deepen button
 * is the later "write this out to full length" upgrade.
 */
export function AddToCurriculumDialog({ content, citations, sessionId, question }: AddToCurriculumDialogProps) {
  const t = useTranslations("chat.addToCurriculum");
  const locale = useLocale();

  const [open, setOpen] = useState(false);
  const [curricula, setCurricula] = useState<CurriculumListItem[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [rootId, setRootId] = useState("");
  const [modules, setModules] = useState<BlockNode[]>([]);
  const [modulesLoading, setModulesLoading] = useState(false);
  const [moduleId, setModuleId] = useState("");
  const [title, setTitle] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function loadModules(nextRootId: string) {
    setModulesLoading(true);
    setModules([]);
    setModuleId("");
    try {
      const tree = await getCurriculum(nextRootId);
      const mods = tree.children.filter((c) => c.kind === "module");
      setModules(mods);
      if (mods.length > 0) setModuleId(mods[0].id);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.detail : t("loadError"));
    } finally {
      setModulesLoading(false);
    }
  }

  async function loadCurricula() {
    setLoading(true);
    setLoadError(null);
    try {
      const rows = await listCurricula();
      setCurricula(rows);
      if (rows.length > 0) {
        setRootId(rows[0].id);
        await loadModules(rows[0].id);
      }
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.detail : t("loadError"));
    } finally {
      setLoading(false);
    }
  }

  function handleOpenChange(next: boolean) {
    if (submitting) return;
    setOpen(next);
    if (next) {
      setTitle(deriveTitle(content, question));
      setDone(false);
      setError(null);
      void loadCurricula();
    }
  }

  function handlePickCurriculum(nextRootId: string) {
    setRootId(nextRootId);
    void loadModules(nextRootId);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!moduleId || !title.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await addLessonFromChat(moduleId, {
        title: title.trim(),
        content,
        citations: citations ?? null,
        chat_session_id: sessionId ?? null,
      });
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="add-to-curriculum-trigger"
            className="text-muted-foreground hover:text-foreground"
          />
        }
      >
        <BookmarkPlus />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        {done ? (
          <DialogBody data-testid="add-to-curriculum-done">
            <div className="flex items-start gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">
              <Check className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                {t("done")}{" "}
                <Link
                  href={`/${locale}/curricula/${rootId}`}
                  className="underline underline-offset-2"
                  data-testid="add-to-curriculum-open-board"
                  onClick={() => setOpen(false)}
                >
                  {t("openBoard")}
                </Link>
              </span>
            </div>
          </DialogBody>
        ) : (
          <form onSubmit={handleSubmit} className="contents" data-testid="add-to-curriculum-form">
            <DialogBody>
              {loading && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  {t("loading")}
                </div>
              )}
              {loadError && (
                <p role="alert" className="text-sm text-destructive" data-testid="add-to-curriculum-load-error">
                  {loadError}
                </p>
              )}
              {!loading && !loadError && curricula.length === 0 && (
                <p className="text-sm text-muted-foreground" data-testid="add-to-curriculum-empty">
                  {t("empty")}
                </p>
              )}

              {curricula.length > 0 && (
                <>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="atc-curriculum">{t("curriculumLabel")}</Label>
                    <select
                      id="atc-curriculum"
                      data-testid="add-to-curriculum-select"
                      value={rootId}
                      onChange={(e) => handlePickCurriculum(e.target.value)}
                      disabled={submitting}
                      className={SELECT_CLASS}
                    >
                      {curricula.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.title}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="atc-module">{t("moduleLabel")}</Label>
                    {modulesLoading ? (
                      <div className="flex items-center gap-2 text-sm text-muted-foreground">
                        <Loader2 className="size-4 animate-spin" />
                      </div>
                    ) : modules.length === 0 ? (
                      <p className="text-sm text-muted-foreground" data-testid="add-to-curriculum-no-modules">
                        {t("noModules")}
                      </p>
                    ) : (
                      <select
                        id="atc-module"
                        data-testid="add-to-curriculum-module-select"
                        value={moduleId}
                        onChange={(e) => setModuleId(e.target.value)}
                        disabled={submitting}
                        className={SELECT_CLASS}
                      >
                        {modules.map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.title}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="atc-title">{t("titleLabel")}</Label>
                    <Input
                      id="atc-title"
                      data-testid="add-to-curriculum-title"
                      value={title}
                      onChange={(e) => setTitle(e.target.value)}
                      disabled={submitting}
                      placeholder={t("titlePlaceholder")}
                    />
                  </div>
                </>
              )}

              {error && (
                <p role="alert" className="text-sm text-destructive" data-testid="add-to-curriculum-error">
                  {error}
                </p>
              )}
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={submitting}
                onClick={() => setOpen(false)}
              >
                {t("cancel")}
              </Button>
              <Button
                type="submit"
                data-testid="add-to-curriculum-submit"
                disabled={submitting || !moduleId || !title.trim()}
              >
                {submitting && <Loader2 className="animate-spin" />}
                {t("submit")}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
