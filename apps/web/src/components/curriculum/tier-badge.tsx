"use client";

import { useTranslations } from "next-intl";
import { AlertTriangle, BookOpen, Brain, Globe } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Tier } from "@/lib/api";
import { cn } from "@/lib/utils";

/** THE ANSWER TO THE QUESTION THE TUTOR ASKED, ON EVERY MODULE.
 *
 * Chris, relaying him: "what happens with the ones saying nothing in your library
 * for this module? does it use the llm knowledge? does it search the internet?"
 * There is no way to answer that in a paragraph of documentation he will never
 * read. So it is a badge — on every module where there is something to say:
 *
 *   library            his own sources actually teach this — the segments below it
 *                      carry page citations he can click. THE RESTING STATE, AND
 *                      THE ONE TIER THAT IS SILENT ON THE BOARD: it is the normal
 *                      outcome, it was on almost every row, and a chip on almost
 *                      every row is wallpaper the eye learns to skip — which is
 *                      what made the three below it invisible. (The badge still
 *                      renders this tier wherever it IS asked for, such as the
 *                      interview's outline and confirm steps, where the tiers are
 *                      being compared against each other rather than read one row
 *                      at a time.)
 *   general_knowledge  they do not, and Claude wrote it from what it knows. Not a
 *                      failure. An UNLABELLED one would be.
 *   web                it needed current information neither of them has
 *   gap                he asked for library-only, his library does not cover it,
 *                      and NOTHING WAS WRITTEN. The most honest badge of the four.
 *
 * Those last three all say the same load-bearing thing in three different ways —
 * this module did not come from your books — so all three speak, each with its
 * coverage note on hover.
 *
 * The tier was assigned by the model AFTER READING THE WHOLE LIBRARY, not by a
 * cosine score clearing a constant — that constant's separation margin on his real
 * corpus was measured at 0.021, which is a coin flip with a decimal point.
 */

const TIER_STYLES: Record<Tier, { icon: typeof BookOpen; className: string }> = {
  library: {
    icon: BookOpen,
    className: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  },
  general_knowledge: {
    icon: Brain,
    className: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
  },
  web: {
    icon: Globe,
    className: "border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300",
  },
  gap: {
    icon: AlertTriangle,
    className: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  },
};

interface TierBadgeProps {
  tier: Tier | undefined;
  /** The model's one-sentence answer to "what in his library covers this, or what
   * is missing from it" — shown on hover. A badge that says "general knowledge"
   * with no reason attached invites exactly one question, and this is it. */
  coverageNote?: string;
  className?: string;
}

export function TierBadge({ tier, coverageNote, className }: TierBadgeProps) {
  const t = useTranslations("curricula.tier");
  if (!tier || !(tier in TIER_STYLES)) return null;

  const { icon: Icon, className: tone } = TIER_STYLES[tier];
  const badge = (
    <span
      data-testid="tier-badge"
      data-tier={tier}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        tone,
        className,
      )}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {t(tier)}
    </span>
  );

  if (!coverageNote) return badge;

  return (
    <Tooltip>
      <TooltipTrigger render={<span className="inline-flex" />}>{badge}</TooltipTrigger>
      <TooltipContent className="max-w-xs">{coverageNote}</TooltipContent>
    </Tooltip>
  );
}
