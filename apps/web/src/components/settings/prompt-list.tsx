"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Check, ChevronDown, ChevronRight, Languages, Loader2, Lock, TriangleAlert } from "lucide-react";

import {
  ApiError,
  getPrompt,
  getPromptSliceHistory,
  listPrompts,
  resetPromptSlice,
  savePromptSlice,
  type PromptDetail,
  type PromptSlice,
  type PromptSliceHistoryEntry,
  type PromptSpan,
  type PromptSummary,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Textarea } from "@/components/ui/textarea";
import { useConfirm } from "@/components/ui/confirm";
import { cn } from "@/lib/utils";

/** READING IS THE FEATURE. EDITING IS THE FOOTNOTE.
 *
 * Chris asked for this on day one and then narrowed it himself: *"They will
 * actually be for the teacher, but we cant degrade their quality so the teacher
 * understands them better. He wont tweak them himself, but he just needs to
 * watch them. he might tweak only some text explaining stuff."*
 *
 * Everything below follows from that sentence and from `settings/page.tsx:22-37`
 * (the tutor is a total beginner with computers):
 *
 *  - **The English is never touched.** We render `detail.text` verbatim, and we
 *    put the Greek BESIDE it, never instead of it. A translated prompt would be
 *    a prompt this app does not send — the viewer would be lying with a friendly
 *    face. He is never asked to understand the English; he is given a Greek
 *    account of it, and the English is there because transparency means showing
 *    the real thing.
 *  - **Collapsed by default, twice.** 31 prompts is a wall. Flow groups shut,
 *    prompts inside them shut, and `GET /prompts/{id}` fires only when he opens
 *    one — `tools.system_claude_cli` alone is ~14,000 characters, and the list
 *    route omits `text` precisely so this page can exist.
 *  - **An interpolated variable is a chip, not a hole.** He must see WHERE his
 *    student brief goes and what it looks like when it lands there. The chip's
 *    LABEL is `select-none`, so selecting the prompt and copying it gives him the
 *    verbatim English and none of our annotations.
 *  - **`cache_cost_warning` is shown BEFORE the save**, in words, with no dollar
 *    figure: the honest number depends on the live library and the sources a
 *    given curriculum selects, and an invented one is worse than none. It costs
 *    once. That is the true and useful part.
 *  - Every failure is a server `code` turned into exactly one Greek sentence,
 *    behind `t.has(...)` so an unknown future code can never render
 *    `prompts.errors.some_new_code` at him.
 *
 * WHAT CHANGED AFTER HE USED IT, AND WHY IT IS NOT A REVERSAL OF THE ABOVE.
 *
 * This card shipped with one textarea in it, over one sentence. Chris: *"bro almost
 * every prompt is uneditable! for example the tutor might have core teaching ideas
 * which claude cannot even imagine ... right now he cannot inject those ideas in his
 * creating curriculum prompts."*
 *
 * He is right, and the spec said so before he did: *"'Locked' must mean 'an editor
 * can't break it by accident', not 'Chris can't change it'."* The padlock was
 * protecting him from an ACCIDENT; it was never an argument that the owner of the app
 * may not put his own teaching into it. "Reading is the feature, editing is the
 * footnote" is still true of how this card is SHAPED — the Greek comes first, the
 * English is verbatim, the textarea is below both. It was never a reason for the
 * footnote to be empty.
 *
 * So, now:
 *  - **30 of 32 prompts have a textarea holding their whole text.** The two that do
 *    not are GENERATED (`tools.*` — the tool list, serialised), and they say so. A
 *    textarea that silently does nothing is worse than no textarea.
 *  - **Restore asks first** (`ui/confirm.tsx`, the app's one dialog). When every
 *    prompt is editable, Restore is the only button that can destroy a paragraph he
 *    wrote and cannot retype.
 *  - **No source path.** Chris: *"i dont think this should be seen by the tutor"* —
 *    same category as a stack trace. It stays on the API, for developers.
 *  - **A course-language prompt says where its language comes from.** The preview used
 *    to claim Greek while the model was told English; see `LanguageOrigin`.
 */
export function PromptList({ provider }: { provider: string | null }) {
  const t = useTranslations("prompts");
  const searchParams = useSearchParams();
  const [prompts, setPrompts] = useState<PromptSummary[] | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [openFlows, setOpenFlows] = useState<string[]>([]);
  const [curriculumGroupOpen, setCurriculumGroupOpen] = useState(false);
  const curriculumGroupRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listPrompts()
      .then(setPrompts)
      .catch((e) => setErrorCode(codeOf(e, "load_failed")));
  }, []);

  /** THE STEP-3 DEEP-LINK LANDS HERE (`interview-scope-step.tsx` ->
   * `/{locale}/settings?promptGroup=curriculum`). Opens the synthetic Curriculum
   * group and scrolls to it — but only once `prompts` has loaded, since the
   * group's DOM node does not exist before then. */
  useEffect(() => {
    if (!prompts || searchParams.get("promptGroup") !== "curriculum") return;
    setCurriculumGroupOpen(true);
    curriculumGroupRef.current?.scrollIntoView({ block: "start" });
  }, [prompts, searchParams]);

  /** The C1 subset (`curriculum_group=True` — exactly ten of them), IN
   * REGISTRATION ORDER, same provider-drop rule as the flow groups below: a
   * prompt only the inactive provider sends must not appear here either. This is
   * a SHORTCUT into the groups below, not a second copy of the mechanism — the
   * same `PromptRow` renders it, against the same `/prompts/{id}` route. */
  const curriculumItems = useMemo(
    () => (prompts ?? []).filter((p) => p.curriculum_group && (!p.provider || p.provider === provider)),
    [prompts, provider],
  );

  /** Grouped in REGISTRATION ORDER (`registry.by_flow` — chat first, because it
   * is the thing he uses every day), never sorted alphabetically: the order the
   * API sends is a decision, not an accident.
   *
   * A prompt only one provider sends is DROPPED when that provider is not the
   * active one. The viewer must show what the app actually sends today; a card
   * for `tools.system_claude_cli` while the app talks to the real API would be
   * exactly the drift this whole feature exists to prevent. */
  const groups = useMemo(() => {
    const out: { flow: string; items: PromptSummary[] }[] = [];
    for (const p of prompts ?? []) {
      if (p.provider && p.provider !== provider) continue;
      const group = out.find((g) => g.flow === p.flow);
      if (group) group.items.push(p);
      else out.push({ flow: p.flow, items: [p] });
    }
    return out;
  }, [prompts, provider]);

  const flowLabel = useCallback(
    (flow: string) => (t.has(`flows.${flow}`) ? t(`flows.${flow}`) : t("flows.other")),
    [t],
  );

  function toggleFlow(flow: string) {
    setOpenFlows((open) => (open.includes(flow) ? open.filter((f) => f !== flow) : [...open, flow]));
  }

  return (
    <Card data-testid="settings-prompts-card">
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">{t("help")}</p>
        <p className="text-sm text-muted-foreground">{t("englishNote")}</p>

        {errorCode && (
          <ErrorNote
            testId="prompts-error"
            message={t.has(`errors.${errorCode}`) ? t(`errors.${errorCode}`) : t("errors.load_failed")}
          />
        )}

        {!prompts && !errorCode && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("loading")}
          </p>
        )}

        <div className="flex flex-col gap-1.5">
          {curriculumItems.length > 0 && (
            <div ref={curriculumGroupRef}>
              <Collapsible open={curriculumGroupOpen} onOpenChange={setCurriculumGroupOpen}>
                <CollapsibleTrigger
                  data-testid="prompt-flow-curriculum-group"
                  aria-label={curriculumGroupOpen ? t("collapse") : t("expand")}
                  className="flex w-full cursor-pointer items-center gap-2 rounded-lg p-2 text-left transition-colors hover:bg-muted"
                >
                  {curriculumGroupOpen ? (
                    <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                  )}
                  <span className="font-medium">{t("curriculumGroup.title")}</span>
                  <span className="ms-auto text-xs text-muted-foreground">
                    {t("count", { count: curriculumItems.length })}
                  </span>
                </CollapsibleTrigger>
                <CollapsibleContent className="overflow-hidden transition-[height] duration-200 ease-out">
                  <div className="flex flex-col gap-1.5 ps-6 pt-1.5">
                    <p className="text-xs text-muted-foreground">{t("curriculumGroup.help")}</p>
                    {curriculumItems.map((p) => (
                      <PromptRow key={`curriculum-group-${p.id}`} summary={p} groupPrefix="curriculum-group-" />
                    ))}
                  </div>
                </CollapsibleContent>
              </Collapsible>
            </div>
          )}
          {groups.map(({ flow, items }) => {
            const open = openFlows.includes(flow);
            return (
              <Collapsible key={flow} open={open} onOpenChange={() => toggleFlow(flow)}>
                <CollapsibleTrigger
                  data-testid={`prompt-flow-${flow}`}
                  aria-label={open ? t("collapse") : t("expand")}
                  className="flex w-full cursor-pointer items-center gap-2 rounded-lg p-2 text-left transition-colors hover:bg-muted"
                >
                  {open ? (
                    <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                  )}
                  <span className="font-medium">{flowLabel(flow)}</span>
                  <span className="ms-auto text-xs text-muted-foreground">
                    {t("count", { count: items.length })}
                  </span>
                </CollapsibleTrigger>
                <CollapsibleContent className="overflow-hidden transition-[height] duration-200 ease-out">
                  <div className="flex flex-col gap-1.5 ps-6 pt-1.5">
                    {items.map((p) => (
                      <PromptRow key={p.id} summary={p} />
                    ))}
                  </div>
                </CollapsibleContent>
              </Collapsible>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

/** One prompt: shut, it is a Greek title and nothing else. Open, it fetches
 * itself and shows the real thing.
 *
 * `groupPrefix` exists ONLY so the synthetic "Curriculum" shortcut group
 * (`PromptList` above) can render the SAME ten prompts a second time — at the
 * top of the page — without a duplicate `data-testid` fighting its "home" flow
 * group for Playwright's strict-mode uniqueness. It namespaces exactly the
 * handful of ids that are unconditionally in the DOM the moment `prompts`
 * loads (the section, its toggle, the "you changed this" badge, the cache
 * note) — not a new rendering path, the same component, same fetch, same
 * slice/span machinery underneath. Defaulting to `""` leaves every existing
 * id byte-identical for the flow groups below. */
function PromptRow({ summary, groupPrefix = "" }: { summary: PromptSummary; groupPrefix?: string }) {
  const t = useTranslations("prompts");
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  /** Seeded from the summary so a shut card can say "you've changed this", then
   * owned by the slices themselves once they are on screen — a badge that
   * survives the Reset it just watched happen is a badge that lies. */
  const [overrides, setOverrides] = useState<Record<string, boolean> | null>(null);
  /** Which language he is LOOKING at, for a `language_from_course` prompt. `null` =
   * whatever the course itself decides, which is the honest default: the point of the
   * fix is that this screen does not get to choose. */
  const [courseLanguage, setCourseLanguage] = useState<string | null>(null);
  const overridden = overrides
    ? Object.values(overrides).some(Boolean)
    : summary.has_override;

  const load = useCallback(
    async (lang: string | null) => {
      setLoading(true);
      setErrorCode(null);
      try {
        const loaded = await getPrompt(summary.id, lang ?? undefined);
        setDetail(loaded);
        setOverrides(Object.fromEntries(loaded.slices.map((s) => [s.id, s.has_override])));
      } catch (e) {
        setErrorCode(codeOf(e, "load_failed"));
      } finally {
        setLoading(false);
      }
    },
    [summary.id],
  );

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (!next || detail || loading) return;
    await load(courseLanguage);
  }

  /** He asked to see the other language. Re-fetches rather than guessing: the
   * directive is built by the API from the live `i18n` module, and a client-side
   * swap of "Greek"->"English" would be this card inventing prompt text — which is
   * the entire class of bug this screen exists to end. */
  async function showLanguage(lang: string) {
    if (lang === (detail?.course_language ?? courseLanguage)) return;
    setCourseLanguage(lang);
    await load(lang);
  }

  return (
    <section
      className="rounded-xl ring-1 ring-foreground/10"
      data-testid={`${groupPrefix}prompt-${summary.id}`}
      data-flow={summary.flow}
    >
      <Collapsible open={open} onOpenChange={toggle}>
        <CollapsibleTrigger
          data-testid={`${groupPrefix}prompt-toggle-${summary.id}`}
          aria-label={open ? t("collapse") : t("expand")}
          className="flex w-full cursor-pointer items-start gap-2 rounded-xl p-3 text-left transition-colors hover:bg-muted/50"
        >
          {open ? (
            <ChevronDown className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          )}
          <span className="font-medium">{summary.title_el}</span>
          {overridden && (
            <span
              data-testid={`${groupPrefix}prompt-overridden-${summary.id}`}
              className="ms-auto shrink-0 rounded-md bg-primary/10 px-1.5 py-0.5 text-xs text-primary"
            >
              {t("overridden")}
            </span>
          )}
        </CollapsibleTrigger>

        <CollapsibleContent className="overflow-hidden transition-[height] duration-200 ease-out">
          <div className="flex flex-col gap-3 p-3 pt-0">
            {/* --- ours, in Greek, ALONGSIDE the prompt — never instead of it --- */}
            <dl className="flex flex-col gap-2 text-sm">
              <div className="flex flex-col gap-0.5">
                <dt className="font-medium">{t("whatItDoes")}</dt>
                <dd className="text-muted-foreground">{summary.what_it_does_el}</dd>
              </div>
              <div className="flex flex-col gap-0.5">
                <dt className="font-medium">{t("whenItRuns")}</dt>
                <dd className="text-muted-foreground">{summary.when_it_runs_el}</dd>
              </div>
            </dl>

            {summary.kind === "fragment" && (
              <p className="text-sm text-muted-foreground">{t("fragmentNote")}</p>
            )}

            {summary.cache_cost_warning && (
              <p
                data-testid={`${groupPrefix}prompt-cache-${summary.id}`}
                className="rounded-lg bg-muted p-2.5 text-sm text-muted-foreground"
              >
                {t("cacheNote")}
              </p>
            )}

            {loading && (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                {t("loading")}
              </p>
            )}

            {errorCode && (
              <ErrorNote
                testId={`prompt-error-${summary.id}`}
                message={t.has(`errors.${errorCode}`) ? t(`errors.${errorCode}`) : t("errors.load_failed")}
              />
            )}

            {detail && (
              <>
                {/* --- the prompt, verbatim, read-only, selectable ------------ */}
                <div className="flex flex-col gap-1.5">
                  <p className="text-sm font-medium">{t("textLabel")}</p>
                  {detail.spans.length > 0 && (
                    <p className="text-xs text-muted-foreground">{t("spanNote")}</p>
                  )}
                  {summary.language_from_course && (
                    <LanguageOrigin
                      promptId={summary.id}
                      current={detail.course_language}
                      onPick={showLanguage}
                    />
                  )}
                  <pre
                    data-testid={`prompt-text-${summary.id}`}
                    data-selectable="true"
                    className="max-h-96 overflow-auto rounded-lg bg-muted/50 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap select-text"
                  >
                    {renderWithSpans(detail.text, detail.spans)}
                  </pre>
                  {/* `source_ref` is NOT rendered. Chris: *"on each prompt i also see
                      where they are inside the code e.g. 'In the code:
                      app/curriculum/corpus.py:289', i dont think this should be seen
                      by the tutor."* A file path is the same category as a stack
                      trace or a status code — `settings/page.tsx:22-37` says he sees
                      none of those, and it is a fact about our repository, which he
                      does not have. It stays on the API for the people it is for. */}
                </div>

                {detail.slices.length === 0 ? (
                  <div
                    data-testid={`prompt-locked-${summary.id}`}
                    className="flex items-start gap-2 rounded-lg bg-muted p-3 text-sm text-muted-foreground"
                  >
                    <Lock className="mt-0.5 size-4 shrink-0" />
                    <div className="flex flex-col gap-1">
                      <span className="font-medium text-foreground">{t("generatedTitle")}</span>
                      <span>{t("generatedWhy")}</span>
                    </div>
                  </div>
                ) : (
                  detail.slices.map((s) => (
                    <SliceEditor
                      key={s.id}
                      slice={s}
                      onOverrideChange={(id, has) =>
                        setOverrides((o) => ({ ...(o ?? {}), [id]: has }))
                      }
                    />
                  ))
                )}
              </>
            )}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}

/** WHERE THIS PROMPT'S LANGUAGE ACTUALLY COMES FROM — and the answer is not this
 * screen.
 *
 * Chris spotted this from the card itself, and it was real:
 *
 *     PREVIEW   (X-App-Locale: el):  "LANGUAGE: write everything you produce in Greek (el)"
 *     REAL CALL (a course whose language is 'en'): "...in English (en)"
 *
 * A curriculum or lesson prompt takes its language from the COURSE, and a course takes
 * it from the STUDENT (`interview.py:311` — `normalize_locale(student.preferred_language)`).
 * The cockpit locale — the only language control he can see — does not enter into it.
 * In his live database, 5 of his 6 courses are English and 131 of 155 lessons are
 * English, because his student Giannis prefers English. THE ENGINE IS RIGHT. The
 * viewer was what lied, and it lied in the most damaging place available: about the
 * one prompt fact he is most likely to care about.
 *
 * So this says the origin out loud, and then does one better than saying it: the
 * toggle re-renders the prompt at the other language, from the API, so he can look at
 * the thing rather than trust a sentence about it.
 */
function LanguageOrigin({
  promptId,
  current,
  onPick,
}: {
  promptId: string;
  current: string | null;
  onPick: (lang: string) => void;
}) {
  const t = useTranslations("prompts");
  return (
    <div
      data-testid={`prompt-language-origin-${promptId}`}
      className="flex flex-col gap-2 rounded-lg bg-muted p-2.5 text-xs text-muted-foreground"
    >
      <p className="flex items-start gap-2">
        <Languages className="mt-0.5 size-4 shrink-0" />
        <span>{t("languageOrigin")}</span>
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        <span>{t("languageShow")}</span>
        {(["el", "en"] as const).map((lang) => (
          <button
            key={lang}
            type="button"
            data-testid={`prompt-language-${lang}-${promptId}`}
            aria-pressed={current === lang}
            onClick={() => onPick(lang)}
            className={cn(
              "cursor-pointer rounded-md px-2 py-0.5 ring-1 transition-colors",
              current === lang
                ? "bg-primary/10 text-primary ring-primary/20"
                : "ring-foreground/10 hover:bg-background",
            )}
          >
            {t(`languageName.${lang}`)}
          </button>
        ))}
      </div>
    </div>
  );
}

/** One prompt's text, his to rewrite. */
function SliceEditor({
  slice: initial,
  onOverrideChange,
}: {
  slice: PromptSlice;
  onOverrideChange: (sliceId: string, hasOverride: boolean) => void;
}) {
  const t = useTranslations("prompts");
  const confirm = useConfirm();
  const [slice, setSlice] = useState(initial);
  const [draft, setDraft] = useState(initial.effective);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<PromptSliceHistoryEntry[] | null>(null);

  /** `history === null` means "unknown", not "empty" — so this fetches both on
   * the first open AND after a save/reset invalidates it. Doing it here rather
   * than in the toggle is what stops an open History list from sitting on a
   * spinner forever after he saves: the save nulls it, and this notices. */
  useEffect(() => {
    if (!historyOpen || history !== null) return;
    let cancelled = false;
    getPromptSliceHistory(slice.id)
      .then((h) => !cancelled && setHistory(h))
      .catch((e) => !cancelled && setErrorCode(codeOf(e, "load_failed")));
    return () => {
      cancelled = true;
    };
  }, [historyOpen, history, slice.id]);

  /** Any interaction invalidates a previous verdict — `settings/page.tsx:63`. A
   * green "Saved." next to text he has since edited is a lie with a tick on it. */
  function clearVerdict() {
    setSaved(false);
    setErrorCode(null);
  }

  function apply(next: PromptSlice) {
    setSlice(next);
    onOverrideChange(next.id, next.has_override);
  }

  async function onSave() {
    setSaving(true);
    clearVerdict();
    const previous = slice;
    // Optimistic: the badge moves under the finger. A failed PUT puts it back —
    // and never touches `draft`, because losing what he typed is a worse outcome
    // than the rejection he is being told about.
    apply({ ...slice, effective: draft, has_override: true });
    try {
      apply(await savePromptSlice(slice.id, draft));
      setSaved(true);
      // What he'd see if he opened History now is one save out of date.
      setHistory(null);
    } catch (e) {
      apply(previous);
      setErrorCode(codeOf(e, "save_failed"));
    } finally {
      setSaving(false);
    }
  }

  /** Back to the text in the code — BEHIND A CONFIRMATION, which Chris asked for by
   * name: *"until i hit the restore default prompt (which also needs a confirmation
   * modal too)"*.
   *
   * P3 argued against a modal here, and the argument was sound at the time: Reset was
   * recoverable (the DELETE snapshots into history first), History was right there,
   * and "a modal over a recoverable action trains him to click through modals". What
   * changed is the size of the thing being destroyed. When the slice was one sentence
   * he had typed a minute ago, an accidental Reset cost him a minute. Now it is the
   * whole of a prompt he may have spent an evening shaping — and the button sits
   * beside Save, where his hand already is. The dialog says what is lost AND that
   * History has it, so it informs rather than merely interrupts.
   *
   * The DELETE is idempotent by design (P2), so this is also the plain "undo what I
   * typed" button — no override needed, and no second code path to keep honest.
   */
  async function onReset() {
    const ok = await confirm({
      title: t("resetConfirmTitle"),
      body: t("resetConfirmBody"),
      confirmLabel: t("reset"),
      destructive: true,
    });
    if (!ok) return;
    clearVerdict();
    const previous = slice;
    const previousDraft = draft;
    apply({ ...slice, effective: slice.default, has_override: false });
    setDraft(slice.default);
    try {
      const next = await resetPromptSlice(slice.id);
      apply(next);
      setDraft(next.effective);
      setHistory(null);
    } catch (e) {
      apply(previous);
      setDraft(previousDraft);
      setErrorCode(codeOf(e, "save_failed"));
    }
  }

  function onToggleHistory() {
    setHistoryOpen((open) => !open);
  }

  const dirty = draft !== slice.effective;

  return (
    <div className="flex flex-col gap-2 rounded-lg bg-primary/5 p-3" data-testid={`slice-${slice.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{t("editableTitle")}</span>
        {slice.has_override && (
          <span
            data-testid={`slice-overridden-${slice.id}`}
            className="rounded-md bg-primary/10 px-1.5 py-0.5 text-xs text-primary"
          >
            {t("overridden")}
          </span>
        )}
      </div>

      <p className="text-xs text-muted-foreground">{t("editableHelp")}</p>

      {/* The label is the API's — it lives beside the text it names, in the
          registry, not in a message file that could drift from it. */}
      <label className="text-sm text-muted-foreground" htmlFor={`slice-input-${slice.id}`}>
        {slice.label_el}
      </label>

      {slice.cache_cost_warning && (
        <p
          data-testid={`slice-cache-warning-${slice.id}`}
          className="flex items-start gap-2 rounded-lg bg-amber-500/10 p-2.5 text-sm text-amber-700 dark:text-amber-400"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0" />
          {t("sliceCacheWarning")}
        </p>
      )}

      {/* Prose, not a key: he is writing a sentence in his own language, so this
          gets a normal font and the browser's spellchecker — unlike the API key
          field upstairs (`settings/page.tsx:32`), which is a paste target. */}
      <Textarea
        id={`slice-input-${slice.id}`}
        data-testid={`slice-input-${slice.id}`}
        value={draft}
        // A whole prompt, not the one sentence this started as. 12 rows is about the
        // longest of them (`ocr.transcribe`, ~1,850 chars) without the card becoming
        // a page of its own; it scrolls past that.
        rows={12}
        onChange={(e) => {
          setDraft(e.target.value);
          clearVerdict();
        }}
        className="bg-background"
      />

      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={onSave} disabled={saving || !dirty} data-testid={`slice-save-${slice.id}`}>
          {saving && <Loader2 className="size-4 animate-spin" />}
          {saving ? t("saving") : t("save")}
        </Button>
        <Button
          variant="outline"
          onClick={onReset}
          disabled={saving || (!slice.has_override && draft === slice.default)}
          title={t("resetHint")}
          data-testid={`slice-reset-${slice.id}`}
        >
          {t("reset")}
        </Button>
        <Button
          variant="ghost"
          onClick={onToggleHistory}
          data-testid={`slice-history-toggle-${slice.id}`}
        >
          {historyOpen ? t("historyHide") : t("history")}
        </Button>

        <span className="ms-auto text-xs text-muted-foreground">
          {t("chars", { count: draft.length, max: slice.max_chars })}
        </span>
      </div>

      {saved && (
        <span
          className="flex items-center gap-1.5 text-sm font-medium text-emerald-600 dark:text-emerald-400"
          data-testid={`slice-saved-${slice.id}`}
        >
          <Check className="size-4" />
          {t("saved")}
        </span>
      )}

      {errorCode && (
        <ErrorNote
          testId={`slice-error-${slice.id}`}
          message={
            // `t.has` keeps an unknown future server code from rendering the raw
            // key path (`prompts.errors.some_new_code`) at the tutor —
            // `settings/page.tsx:213` makes the same guard for the same reason.
            t.has(`errors.${errorCode}`)
              ? t(`errors.${errorCode}`, { max: slice.max_chars })
              : t("errors.save_failed")
          }
        />
      )}

      {historyOpen &&
        (history === null ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("loading")}
          </p>
        ) : history.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("historyEmpty")}</p>
        ) : (
          /* Newest first, as the API sends it — the question this list answers is
             "give me back what I just lost", and the answer is the top row. */
          <ul className="flex flex-col gap-2" data-testid={`slice-history-${slice.id}`}>
            {history.map((h) => (
              <li key={h.id} className="flex flex-col gap-1 rounded-lg bg-background p-2.5">
                <span className="text-xs text-muted-foreground">
                  {t("historyReplacedAt", { date: formatWhen(h.replaced_at) })}
                </span>
                <span className="font-mono text-xs whitespace-pre-wrap">{h.text}</span>
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}

/** The prompt, with its interpolated variables marked in place.
 *
 * The chip renders `text.slice(start, end)` rather than `span.value`: the API
 * guarantees they are equal (it is a test there), and slicing the text is what
 * makes this function structurally incapable of showing anything the prompt does
 * not actually say. The label is `select-none` so that selecting the block and
 * copying it yields the verbatim English — he may want to paste it somewhere,
 * and our annotations must not travel with it.
 */
function renderWithSpans(text: string, spans: PromptSpan[]): ReactNode[] {
  const out: ReactNode[] = [];
  let at = 0;
  // Defensive on both counts: a span that starts inside the previous one, or
  // reaches past the end, means the API and this render disagree — drop it and
  // show the prompt whole rather than render it twice or slice it to pieces.
  const ordered = [...spans]
    .sort((a, b) => a.start - b.start)
    .filter((s) => s.start >= 0 && s.end <= text.length && s.start < s.end);

  for (const s of ordered) {
    if (s.start < at) continue;
    if (s.start > at) out.push(text.slice(at, s.start));
    out.push(
      <span
        key={`${s.name}-${s.start}`}
        data-testid={`prompt-span-${s.name}`}
        className="rounded bg-primary/10 px-1 py-0.5 ring-1 ring-primary/20"
      >
        <span className="me-1 rounded bg-primary/15 px-1 text-[10px] text-primary select-none">
          {s.label_el}
        </span>
        {text.slice(s.start, s.end)}
      </span>,
    );
    at = s.end;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

function ErrorNote({ testId, message }: { testId: string; message: string }) {
  return (
    <div
      role="alert"
      data-testid={testId}
      className="flex items-start gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

/** The server's machine-readable `code`, or a fallback — never `detail`, which
 * is prose written on the server, in one language, that the tutor must never
 * actually read (`lib/api.ts`'s `ApiError`). */
function codeOf(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.code ? e.code : fallback;
}

/** A date he can read, in his own locale, with no library. `replaced_at` is an
 * ISO string from the API; an unparseable one is shown raw rather than as
 * "Invalid Date". */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
