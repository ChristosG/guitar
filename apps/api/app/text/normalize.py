"""Greek-aware text folding. Small file, load-bearing in three places.

WHY THIS EXISTS. Greek is heavily inflected AND its accents MOVE under
inflection: μάθημα -> μαθήματα (the tonos jumps from the alpha to the eta).
So a regex or a `LIKE` written against one form silently fails to match the
others, and Python's `.lower()` does not help — it lowercases but keeps the
accent, so `'μάθημα'.lower() != 'μαθημα'`.

Three real bugs in this codebase come from that single fact:

  1. `agent/loop.py::_is_content_bearing` never matched a Greek question, so
     the forced-retrieval pre-hop never fired and EVERY Greek content question
     was answered ungrounded — in the tutor's DEFAULT locale.
  2. `agent/tools.py::find_lesson` uses `Block.title.ilike('%q%')`. Postgres's
     ILIKE is accent-SENSITIVE: a tutor searching `τονικοτητα` (no accents, as
     people actually type) finds nothing, because the lesson is stored as
     `Τονικότητα`.
  3. The lexical/BM25 arm of hybrid retrieval (Plan 13 Stage 4) has to match
     `κιθάρας` against `κιθάρα`.

`fold` is the one answer to all three. It is deliberately NOT a stemmer — it
does not touch inflectional endings, only the accent and case, so `κιθάρας` and
`κιθάρα` still differ after folding. That is the BM25 tokenizer's job (it runs a
Snowball Greek stemmer on top of this). Folding alone fixes the accent half of
the problem and is exactly right for regex matching, where over-stemming would
cause false positives.
"""
from __future__ import annotations

import unicodedata

# Greek final sigma. `ς` and `σ` are the SAME letter in different positions, and
# `str.casefold()` does not unify them — so `φώς`.casefold() != `φωσ`. Every
# word-final sigma in Greek is `ς`, which means a naive fold makes every Greek
# word ending in -s fail to match its own stem.
_FINAL_SIGMA = "ς"
_SIGMA = "σ"


def fold(text: str) -> str:
    """Accent-, case- and sigma-insensitive form of `text`.

    NFD decomposes `ά` into `α` + COMBINING ACUTE; stripping category `Mn`
    (nonspacing mark) removes the accent while leaving the letter. Then final
    sigma is unified, then casefold (which is `.lower()` done properly — it
    handles ς/σ/Σ and the German ß, unlike `.lower()`).

    >>> fold("Τονικότητα")
    'τονικοτητα'
    >>> fold("ΦΩΣ") == fold("φώς")
    True
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace(_FINAL_SIGMA, _SIGMA).casefold()


def has_greek(text: str) -> bool:
    """True if `text` contains any Greek letter.

    Used to decide whether a query needs normalising to the corpus language
    before it is embedded (Plan 13, Stage 4.5), and to gate `agent/guards.py`'s
    named-song detector — whose allowlist is English-only, so on Greek input it
    false-declines ordinary tab requests and makes `generate_artifact`
    unreachable in the default locale.
    """
    return any("Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿" for ch in text)
