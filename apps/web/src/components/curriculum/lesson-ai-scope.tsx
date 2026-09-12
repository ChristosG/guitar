"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

interface LessonAiScope {
  /** The lesson the «AI στο μάθημα» panel is currently pointed at, or null
   * before anything has asked for it. `moduleTitle` rides along because the
   * panel has to say WHICH lesson it is about, and a lesson title alone
   * ("Μπράτσο") is not identifying inside a course that has several. */
  lesson: { id: string; title: string; moduleTitle: string } | null;
  /** Bumped every time something asks the panel to open. A COUNTER rather than
   * a boolean, for the same reason `revise-scope.tsx` uses one: the panel owns
   * its own open/closed state (Escape, the X, the backdrop), and a boolean here
   * would fight it — a change in this number is a REQUEST to open, not a claim
   * about whether it is open. Re-clicking the SAME lesson's button therefore
   * still reopens the panel, which a boolean could not express. */
  openRequest: number;
  openForLesson: (lessonId: string, lessonTitle: string, moduleTitle: string) => void;
  close: () => void;
}

const Ctx = createContext<LessonAiScope | null>(null);

/** Shared state between a lesson row's «AI στο μάθημα» button and the panel
 * that answers it.
 *
 * IT LIVES ABOVE BOTH OF THEM, and it has to — exactly the situation
 * `revise-scope.tsx` documents for the revise drawer. The button is buried
 * inside `TreeBoard` -> the recursive `BlockCard`; the panel is a sibling of
 * the board in the page's top row. A provider inside the panel never reaches
 * the cards, and a prop would have to be drilled through `TreeBoard` and then
 * every level of `BlockCard`'s own recursion.
 *
 * Consumers get `null` outside a provider, so a `BlockCard` rendered anywhere
 * else (a test, a future screen) simply omits the button instead of crashing. */
export function LessonAiScopeProvider({ children }: { children: ReactNode }) {
  const [lesson, setLesson] = useState<LessonAiScope["lesson"]>(null);
  const [openRequest, setOpenRequest] = useState(0);

  const openForLesson = useCallback((lessonId: string, lessonTitle: string, moduleTitle: string) => {
    setLesson({ id: lessonId, title: lessonTitle, moduleTitle });
    setOpenRequest((n) => n + 1);
  }, []);

  const close = useCallback(() => {
    setLesson(null);
  }, []);

  const value = useMemo(
    () => ({ lesson, openRequest, openForLesson, close }),
    [lesson, openRequest, openForLesson, close],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLessonAiScope(): LessonAiScope | null {
  return useContext(Ctx);
}
