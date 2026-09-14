import { Fragment, type ReactNode } from "react";

import { parseInlineMarks, type InlineNode } from "@/lib/inline-marks";

/**
 * Render lesson text with its three inline marks: `**έντονο**`, `*πλάγιο*`
 * and `<u>υπογράμμιση</u>`.
 *
 * NEVER `dangerouslySetInnerHTML`. The text is the tutor's own typing (and the
 * model's), it contains `<u>` as a literal marker, and one day it will contain
 * a `<script>` the tutor pasted out of a forum. The tree comes from
 * `parseInlineMarks` — the SAME grammar the API's `inline_marks.py` uses for
 * the Word export and the word count — and every leaf is inserted as a React
 * text child, so nothing in the string is ever interpreted as markup.
 *
 * NO WRAPPER ELEMENT: the component returns a fragment of nodes so that the
 * caller's own `<p>` (with its `whitespace-pre-wrap` and its testid) stays the
 * one box around the prose. A wrapper `<span>` here would be a second box that
 * every caller then has to style around.
 */
function renderNodes(nodes: InlineNode[]): ReactNode {
  return nodes.map((node, i) => {
    if (node.type === "text") return <Fragment key={i}>{node.value}</Fragment>;
    const children = renderNodes(node.children);
    if (node.type === "strong")
      return (
        <strong key={i} data-slot="inline-strong">
          {children}
        </strong>
      );
    if (node.type === "em")
      return (
        <em key={i} data-slot="inline-em">
          {children}
        </em>
      );
    return (
      <u key={i} data-slot="inline-u">
        {children}
      </u>
    );
  });
}

export function InlineMarks({ text }: { text: string }) {
  return <>{renderNodes(parseInlineMarks(text))}</>;
}
