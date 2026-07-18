# Unit A — Curriculum Screen Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the single inline curricula page with an index of navigable cards + a dedicated per-curriculum detail route, following the existing detail-route convention.

**Architecture:** New client route `curricula/[rootId]/page.tsx` renders `TreeBoard` for one curriculum with a back link. The list page (`curricula/page.tsx`) becomes a searchable index whose cards are `<Link>`s to the detail route; materializing a new curriculum navigates there.

**Tech Stack:** Next.js App Router (client components), next-intl, existing `getCurriculum`/`listCurricula` API helpers, Playwright.

## Global Constraints

- CPU-only app; no backend/model changes in this unit — frontend only.
- Follow the EXISTING detail-route convention verbatim: `lessons/[lessonId]/page.tsx`, `students/[id]/page.tsx`, `library/[sourceId]/page.tsx` (dynamic `[param]/page.tsx`, `"use client"`, `useParams`, back `<Link href={`/${locale}/<section>`}>` with `<ArrowLeft/>`).
- Preserve the "read module 1 while module 5 drafts" behavior: after the interview materializes a tree, the tutor must land on the board immediately (now via navigation), with the draft progress bar (already inside `TreeBoard`).
- `TreeBoard` OWNS its tree once mounted (draft polling writes into it) — mount it with a `key={root.id}` and never sync a prop into its state via effect.
- Keep all existing `data-testid`s that tests rely on where still meaningful; add new ones for new elements.

---

### Task A1: Per-curriculum detail route

**Files:**
- Create: `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx`
- Reference: `apps/web/src/app/[locale]/(cockpit)/lessons/[lessonId]/page.tsx` (convention + back link at :123-130), `apps/web/src/app/[locale]/(cockpit)/curricula/page.tsx` (board logic to lift, :55-92 + :146-173)

**Interfaces:**
- Consumes: `getCurriculum(rootId): Promise<BlockNode>`, `TreeBoard({ root, locale, onRootDeleted })`, `ApiError` (all from existing modules).
- Produces: the route `/[locale]/curricula/[rootId]`.

- [ ] **Step 1: Write the detail page**

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft } from "lucide-react";
import { TreeBoard } from "@/components/curriculum/tree-board";
import { ApiError, getCurriculum, type BlockNode } from "@/lib/api";

export default function CurriculumDetailPage() {
  const t = useTranslations("curricula");
  const locale = useLocale();
  const router = useRouter();
  const params = useParams<{ rootId: string }>();
  const rootId = params.rootId;

  const [tree, setTree] = useState<BlockNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getCurriculum(rootId)
      .then((data) => { if (!cancelled) setTree(data); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.detail : t("boardError")); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [rootId, t]);

  const handleRootDeleted = useCallback(() => {
    router.push(`/${locale}/curricula`);
  }, [router, locale]);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link
          href={`/${locale}/curricula`}
          data-testid="curricula-back"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          {t("backToList")}
        </Link>
      </div>

      {loading && <p className="text-sm text-muted-foreground" data-testid="board-loading">{t("boardLoading")}</p>}
      {error && <p role="alert" data-testid="board-error" className="text-sm text-destructive">{error}</p>}
      {!loading && !error && tree && (
        <TreeBoard key={tree.id} root={tree} locale={locale} onRootDeleted={handleRootDeleted} />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Add the two new i18n keys** `curricula.backToList` (en: "All curricula", el: "Όλα τα curricula") in `apps/web/src/messages/en.json` and `el.json` under the existing `curricula` object. (Reuse existing `boardLoading`/`boardError`.)

- [ ] **Step 3: Verify** `npx tsc --noEmit` is clean for the web app.

- [ ] **Step 4: Commit** `feat(curricula): per-curriculum detail route`.

---

### Task A2: List page becomes a navigable, searchable index

**Files:**
- Modify: `apps/web/src/app/[locale]/(cockpit)/curricula/page.tsx`
- Test: `apps/web/tests/` (wherever curricula e2e/component tests live — search for `templates-list`/`template-item`)

**Interfaces:**
- Consumes: `listCurricula()`, `InterviewDialog({ onMaterialized })`, `next/link`, `next/navigation` `useRouter`.
- Produces: index cards linking to `/[locale]/curricula/[rootId]`; `onMaterialized` navigates to the detail route.

- [ ] **Step 1:** Remove the board state and handlers (`activeTree`, `activeId`, `boardLoading`, `boardError`, `handleSelectTemplate`, `handleRootDeleted`, the `<TreeBoard>` block, and the `getCurriculum`/`BlockNode`/`TreeBoard`/`cn` imports if now unused). Keep `templates`, `templatesLoading`, `templatesError`, `fetchTemplates`, `refreshTemplates`.

- [ ] **Step 2:** Change `handleMaterialized` to navigate:

```tsx
const router = useRouter();
const handleMaterialized = useCallback(
  (rootId: string) => { router.push(`/${locale}/curricula/${rootId}`); },
  [router, locale],
);
```

- [ ] **Step 3:** Add a title search filter above the list:

```tsx
const [query, setQuery] = useState("");
const shown = templates.filter((tm) => tm.title.toLowerCase().includes(query.trim().toLowerCase()));
```
Render an `<input data-testid="curricula-search" .../>` (placeholder `t("searchPlaceholder")`) shown only when `templates.length > 0`; map over `shown` instead of `templates`.

- [ ] **Step 4:** Turn each card from a `<button onClick=...>` into:

```tsx
<Link
  key={item.id}
  href={`/${locale}/curricula/${item.id}`}
  data-testid="template-item"
  className="flex w-56 flex-col gap-1 rounded-xl border border-border bg-card p-3 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50"
>
  <span className="truncate font-medium" data-testid="template-title">{item.title}</span>
  <span className="flex items-center gap-2 text-xs text-muted-foreground">
    <Badge variant="outline">{item.language}</Badge>
    {typeof item.target_profile?.level === "string" && <span>{item.target_profile.level}</span>}
  </span>
</Link>
```
Keep the empty state (`templates-empty`) and error (`templates-error`). Add an "no match" hint when `templates.length > 0 && shown.length === 0`.

- [ ] **Step 5:** Add i18n key `curricula.searchPlaceholder` (en: "Search curricula…", el: "Αναζήτηση curricula…") and `curricula.searchNoMatch` (en: "No curriculum matches your search.", el: "Κανένα curriculum δεν ταιριάζει.").

- [ ] **Step 6: Verify** `npx tsc --noEmit` clean.

- [ ] **Step 7: Commit** `feat(curricula): navigable searchable index page`.

---

### Task A3: Deep-link existing "view curriculum" links to the detail route

**Files:**
- Modify: `apps/web/src/components/chat/chat-panel.tsx:215` (post-generation link), `apps/web/src/components/chat/add-to-curriculum-dialog.tsx:181`, `apps/web/src/components/students/assignments-list.tsx:43` — wherever a `root_id`/curriculum id is in scope, point the link at `/${locale}/curricula/${id}` instead of `/${locale}/curricula`.

- [ ] **Step 1:** For each of the three, if the curriculum id is available in that scope, change the `href` to the detail route; if an id is NOT in scope, leave it pointing at the index (do not invent an id). Note in the commit which were changed vs left.

- [ ] **Step 2: Verify** `npx tsc --noEmit` clean.

- [ ] **Step 3: Commit** `feat(curricula): deep-link to per-curriculum detail where id is known`.

---

### Task A4: Tests

**Files:**
- Modify/Create: the curricula Playwright/e2e spec (find it by grepping tests for `template-item` / `curricula-generate-button`).

- [ ] **Step 1:** Update/add tests: (a) index renders `template-item` cards; (b) clicking a card navigates to `/<locale>/curricula/<id>` and the `TreeBoard` (or `board-loading`) is visible; (c) `curricula-back` returns to the index; (d) typing in `curricula-search` filters the visible cards; (e) any prior test that relied on inline `handleSelectTemplate`/`board-*` on the list page is moved to the detail route.

- [ ] **Step 2: Run** the web test suite for the curricula spec + `npx tsc --noEmit`. All green.

- [ ] **Step 3: Commit** `test(curricula): index navigation + detail route + search`.
