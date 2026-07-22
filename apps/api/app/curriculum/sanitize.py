"""THE INLINE-CITATION SAFETY NET (2026-07-22, Chris: "sources should be only
on their component and zero references inside the curricula").

The curriculum contract has always been: provenance lives in the STRUCTURED
per-section `citations` array (validated by `draft.invalid_citations`, shown in
the "Πηγές" modal) — never in the prose. But every guard operated on the array
only; a model that ALSO wrote "(S9, p.47)" into the body sailed through
untouched, because `_render_section` joins the text raw and `block-card.tsx`
renders it verbatim. `shared.curriculum_style` now tells the model not to; this
module is what makes the promise hold when it does it anyway.

Applied at the points where model text becomes `Block.body` (draft/deepen via
`draft._render_section` and the lesson summary, surgical segments via
`segment_generate`, refines via `refine`) — NOT in the UI. Stripping at render
would leave the markers in the DB, the DOCX export, and every future surface.

WHAT IT MUST NEVER EAT: exercise timings like "(5 min)", chord/tuning parens
like "(Drop D)", and ordinary parenthetical Greek. Hence every pattern requires
an actual page marker (`p.`/`pp.`/`page`/`σ.`/`σελ.`) or a bare source ref
(`S9`) inside the parens — a number alone is never enough.
"""
from __future__ import annotations

import re

# A page marker: "p.47", "pp. 12-14", "page 47", "σελ. 47", "σ.47". The
# lookbehind keeps "Amp 5" and "Riff 3" safe: the `p`/`σ` must not be the tail
# of a word.
_PAGE = r"(?<![A-Za-zΑ-Ωα-ωά-ώ])(?:pp?\.|pages?\s|σελ\.?\s?|σ\.\s?)\s*\d+(?:\s*[-–—]\s*\d+)?"

# "(S9, p.47)", "[Tone Manual, p. 47]", "(σελ. 12–14)", "[p.12]" — any short
# bracketed run that contains a page marker. The 60-char head allows a source
# name/ref before the marker; the bracket classes forbid nesting so a whole
# legitimate sentence in parens can never match.
_PAGE_REF = re.compile(
    r"\s*[(\[]"
    r"[^()\[\]]{0,60}?"
    + _PAGE +
    r"[^()\[\]]{0,20}"
    r"[)\]]"
)

# Bare source refs with no page: "(S9)", "[S2]", "(S1, S3)".
_SOURCE_REF = re.compile(r"\s*[(\[]\s*S\d+(?:\s*[,·;]\s*S\d+)*\s*[)\]]")


def strip_inline_citations(text: str | None) -> str:
    """`text` with every inline source/page reference removed, whitespace and
    orphaned punctuation tidied. Empty-safe: None/"" come back as ""."""
    if not text:
        return ""
    out = _PAGE_REF.sub("", text)
    out = _SOURCE_REF.sub("", out)
    # A removed "(S9, p.47)" leaves "λέξη ." or doubled spaces behind — tidy,
    # but only on the space axis: newlines are section structure, never touched.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([,.;:!?···»])", r"\1", out)
    return out.strip()
