"""The app's ONE definition of "what language is this request in", and the ONE
place that owns the language rule every prompt builder appends (Plan 13, Stage
5.1/5.3).

WHY THIS MODULE EXISTS: the UI has been bilingual since day one and the API
never knew. `useLocale()` is read by eight web components and every one of them
uses it only to build an href — nothing in `lib/api.ts` carried it. So the model
picked the language: the chat answered in whatever it felt like, `explain_concept`
defaulted to `"en"`, `generate_curriculum` exposed `language` as a *model-chosen*
required parameter, and `artifacts/generate.py` had no locale at all. A Greek
tutor, in a Greek UI, got English artifacts. The browser now sends
`X-App-Locale` on every request (`apps/web/src/lib/api.ts`) and `locale_dep`
below is how a route reads it.

`normalize_locale` NEVER raises. A locale is not user input to be validated, it
is a presentation preference: a request carrying `zz`, `xx-YY`, an empty string,
or no header at all is a request we still answer — in the default locale. Raising
a 422 on a malformed `Accept-Language`-ish header would turn a cosmetic
mismatch into a broken page.

THE DEFAULT IS GREEK, not English (`apps/web/src/i18n/routing.ts`:
`defaultLocale: "el"`). Anything in this codebase still defaulting to `"en"` is
a bug from the era when the API had no idea a locale existed.

THE LANGUAGE RULE ITSELF (`language_directive`) is the subtle part, and it is
why this is one function and not a sentence copy-pasted into five prompts:

  - The tutor writes GREEK. His library is an ENGLISH book. The model must
    therefore retrieve/read cross-lingually, ANSWER in Greek, and — when it
    quotes his book — reproduce the ENGLISH words VERBATIM. A translated
    "quotation" is not a citation; it is the model's paraphrase wearing
    quotation marks, and it destroys the one property the whole grounding
    stack exists to provide (that a cited passage can be checked against the
    page scan it came from).
  - MACHINE TOKENS ARE NEVER TRANSLATED, in either direction: alphaTex source,
    note/chord names (C, Am7, G7), tunings (Drop D, E A D G B E), fret/string
    numbers, and gear model names (Tube Screamer, TS-808, 5150). A model told
    "write in Greek" WILL otherwise transliterate a chord name or a pedal, and
    an "Ααμ7" renders as nothing — AlphaTab throws on a transliterated
    alphaTex, and a gear card that says «Σωλήνας Ουρλιαχτού» is not a Tube
    Screamer.

`answer_in` is the SHORT tail reminder, appended at the very END of a grounding/
context block rather than only in the system prompt. Recency wins: when several
thousand tokens of English passages sit immediately before the generation point,
a language instruction buried in the system prompt is the least recent thing the
model saw. The one-line reminder after the passages is what actually holds.
"""
from typing import Annotated

from fastapi import Header

# The app's default locale — GREEK. Mirrors `apps/web/src/i18n/routing.ts`'s
# `defaultLocale: "el"`. Every `"en"` default this codebase used to carry was a
# silent contradiction of that.
DEFAULT_LOCALE = "el"

# The locales the UI actually ships messages for (`apps/web/src/messages/`).
SUPPORTED_LOCALES = ("el", "en")

# The request header the web app sets in BOTH `request()` and
# `streamChatMessage`'s own raw fetch (which bypasses `request()` entirely —
# see that function's comment). Named here so `main.py`'s CORS config and the
# tests can refer to the same constant instead of three string literals.
LOCALE_HEADER = "X-App-Locale"

_LANGUAGE_NAMES = {"el": "Greek", "en": "English"}


def normalize_locale(raw: str | None) -> str:
    """`"el-GR"` -> `"el"`, `"EN"` -> `"en"`, anything unknown/absent ->
    `DEFAULT_LOCALE`. Never raises — see this module's docstring.
    """
    if not raw:
        return DEFAULT_LOCALE
    base = raw.strip().lower().replace("_", "-").split("-")[0]
    return base if base in SUPPORTED_LOCALES else DEFAULT_LOCALE


def locale_dep(
    x_app_locale: Annotated[str | None, Header()] = None,
) -> str:
    """FastAPI dependency: the request's UI locale, normalized, defaulted.

    Declared as a `Header()` param (FastAPI maps `x_app_locale` ->
    `X-App-Locale`) so it shows up in the OpenAPI schema like any other
    input, rather than being dug out of `request.headers` inside each route.
    """
    return normalize_locale(x_app_locale)


def language_name(locale: str) -> str:
    """"Greek"/"English" — the name to put IN a prompt. Models follow a
    language NAME far more reliably than a two-letter code, which they will
    occasionally mistake for something to echo rather than obey.
    """
    return _LANGUAGE_NAMES[normalize_locale(locale)]


# THE LANGUAGE RULE, lifted out of `language_directive` byte-identically so the
# tutor can rewrite it and `app/prompts/registry.py` can point at it.
#
# `{name}` and `{code}` are the LOCALE, filled in at call time — which is why ONE
# override covers both languages: what he edits is the rule, not the Greek. The
# placeholders are also why `overrides.validate` refuses an edit that drops one:
# a directive that no longer names a language is a directive that says nothing,
# and it would fail silently, in the one place this app cannot afford it
# (`prompt-cache`/`retrieval` both assume this line holds).
LANGUAGE_DIRECTIVE = (
    "LANGUAGE: write everything you produce in {name} ({code}) — every "
    "title, body, explanation and label. The tutor's library is written in "
    "ENGLISH; read it as it is and answer in "
    "{name} anyway. When you QUOTE that material, keep the quoted words in "
    "ENGLISH, verbatim — never translate a quotation; explain it in "
    "{name} around the quote instead. NEVER translate or transliterate "
    "machine tokens, in any language: alphaTex notation, note and chord "
    "names (C, Am7, G7), tunings (Drop D, E A D G B E), fret/string "
    "numbers, and gear or model names (Tube Screamer, TS-808, 5150). Those "
    "are written the same way in every language."
)
LANGUAGE_DIRECTIVE_SLICE_ID = "shared.language_directive"

ANSWER_IN = "Answer in {name} ({code})."
ANSWER_IN_SLICE_ID = "shared.answer_in"

# THE CURRICULUM REGISTER RULE (2026-07-22, Chris: "everything in Greek except
# terminology... zero citations inside... like the teacher is reading those
# blocks and teaching at the same time").
#
# WHY THIS EXISTS AS A SECOND RULE INSTEAD OF AN EDIT TO `LANGUAGE_DIRECTIVE`:
# the two rules serve different products. Chat exists to ANSWER FROM HIS
# LIBRARY, so a quotation there must stay English and verbatim — that is what
# makes it checkable against the page scan. A curriculum exists to be TAUGHT
# FROM: its text is read aloud to a Greek student, its provenance lives in the
# structured citations array (the "Πηγές" modal), and an English sentence
# spliced into Greek prose — «η κιθάρα transmits the tonal quality...» — is not
# grounding, it is a lesson the tutor cannot read out. So every curriculum
# WRITING flow appends this AFTER `language_directive` (recency wins, and the
# override is explicit), and chat never sees it.
CURRICULUM_STYLE = (
    "HOW THE COURSE TEXT MUST READ — these rules apply to everything you "
    "write for this course (titles, objectives, section text), and they "
    "OVERRIDE the quotation rule above:\n"
    "- SPOKEN, NOT WRITTEN: write the teaching text as the words the tutor "
    "actually SAYS to the student, addressing the student directly — a "
    "script he can read aloud in the lesson exactly as written, not book "
    "prose he has to paraphrase on the fly. If a sentence would sound "
    "stiff said out loud across a music stand, rewrite it until it sounds "
    "like teaching.\n"
    "- ONE LANGUAGE: every sentence is {name} from start to finish — never "
    "splice English words or half-sentences into {name} prose. English is "
    "reserved for technical guitar and audio terminology (humbucker, "
    "single coil, compressor, sustain), gear and model names, book titles "
    "and proper names. An ordinary word with an everyday {name} "
    "equivalent is written in {name}.\n"
    "- NO QUOTES, NO SOURCE TALK: never copy a sentence from the library "
    "into the text, not even in quotation marks, and never mention books, "
    "sources, pages or 'the literature' in the prose. Teach the idea "
    "directly, in your own {name} words, as the tutor's own knowledge.\n"
    "- NO CITATIONS IN THE TEXT: page references such as (S9, p.47) or "
    "[p.12] never appear inside titles or body text. Where the schema has "
    "a citations array, that array is the ONLY place a page reference "
    "belongs; the app shows the tutor his sources separately.\n"
    "- INLINE MARKERS: text you are given may carry **bold**, *italic* or "
    "<u>underline</u> markers around words; keep them exactly where they "
    "are in any text you keep or lightly edit, and do not add markers of "
    "your own."
)
CURRICULUM_STYLE_SLICE_ID = "shared.curriculum_style"


def language_directive(locale: str, source=None) -> str:
    """THE language rule, in one place, for every prompt builder in the app
    (`brain/retrieve.py`, `lessons/draft.py`, `curriculum/generate.py` ×3,
    `artifacts/generate.py`, and the agent loop's system prompt).

    See this module's docstring for why each clause is here. Written as a
    directive to the model, not as prose about the model.

    `source` is the tutor's overrides (a Session, a `snapshot()` mapping, or None
    for the code default) — see `app/prompts/overrides.py`. It is threaded rather
    than looked up globally because this module is a LEAF: eight builders call it,
    and a global would make the answer depend on which request last touched it.
    """
    from app.prompts.overrides import resolve

    return resolve(source, LANGUAGE_DIRECTIVE_SLICE_ID, LANGUAGE_DIRECTIVE).format(
        name=language_name(locale), code=normalize_locale(locale),
    )


def curriculum_style(locale: str, source=None) -> str:
    """THE CURRICULUM REGISTER RULE — see `CURRICULUM_STYLE` above for why it is
    a second rule and not an edit to `language_directive`. Appended immediately
    AFTER `language_directive` by every flow that WRITES course material
    (`curriculum/outline.py`, `curriculum/draft.py`, `curriculum/extend.py`,
    `curriculum/segment_generate.py`, `curriculum/refine.py`,
    `curriculum/revise.py` — the planner's titles/objectives land verbatim on
    the board) and by none that answers questions from the library.
    """
    from app.prompts.overrides import resolve

    return resolve(source, CURRICULUM_STYLE_SLICE_ID, CURRICULUM_STYLE).format(
        name=language_name(locale),
    )


def answer_in(locale: str, source=None) -> str:
    """The short tail reminder (see this module's docstring: recency beats the
    system prompt when a long English passage sits immediately before the
    generation point). Appended at the END of a grounding/CONTEXT block.
    """
    from app.prompts.overrides import resolve

    return resolve(source, ANSWER_IN_SLICE_ID, ANSWER_IN).format(
        name=language_name(locale), code=normalize_locale(locale),
    )
