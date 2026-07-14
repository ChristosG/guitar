"use client"

import { Tooltip as TooltipPrimitive } from "@base-ui/react/tooltip"

import { cn } from "@/lib/utils"

/** Thin wrapper over Base UI's Tooltip, in the same shape as `dialog.tsx`/
 * `collapsible.tsx` (no second popup library — the app already depends on
 * `@base-ui/react`). It exists for one concrete reason: text that is
 * `truncate`d or `line-clamp`ed is text the tutor CANNOT READ, and this app
 * clamps titles in several places (library source rows, the interview's
 * source picker). A clamped title needs a way to reveal itself; a native
 * `title=` attribute is a ~1s-delayed OS tooltip that never fires on
 * keyboard focus, so this uses the real thing — which Base UI shows on
 * hover AND on focus-visible, for free.
 */
function Tooltip({ ...props }: TooltipPrimitive.Root.Props) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />
}

/** `delay` lives on the TRIGGER in this version of Base UI (not on Root, and
 * not only on Provider) — 150ms is short enough to feel like a reveal rather
 * than a wait, without firing on every stray mouse transit. */
function TooltipTrigger({ delay = 150, ...props }: TooltipPrimitive.Trigger.Props) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" delay={delay} {...props} />
}

function TooltipContent({
  className,
  sideOffset = 6,
  side = "top",
  children,
  ...props
}: TooltipPrimitive.Popup.Props & {
  sideOffset?: number
  side?: TooltipPrimitive.Positioner.Props["side"]
}) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Positioner sideOffset={sideOffset} side={side} className="z-50">
        <TooltipPrimitive.Popup
          data-slot="tooltip-content"
          // `max-w-xs` + `break-words`: the whole point is long content, and a
          // tooltip that itself overflows the viewport would just move the bug.
          className={cn(
            "max-w-xs rounded-lg bg-popover px-2.5 py-1.5 text-xs leading-relaxed break-words text-popover-foreground ring-1 ring-foreground/10 shadow-md duration-100 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
            className
          )}
          {...props}
        >
          {children}
        </TooltipPrimitive.Popup>
      </TooltipPrimitive.Positioner>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipContent, TooltipTrigger }
