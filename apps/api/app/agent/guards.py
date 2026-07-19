"""Post-turn guard (Plan 11 Task 2, C3): detect a hand-typed ASCII tab in
assistant prose so `app/agent/loop.py` can intercept it before it ever
reaches the tutor as if it were a real answer.

THE BUG THIS EXISTS FOR: Chris asked for a G major scale tab. The model
typed six lines of ASCII into a code fence — every string reading the
identical `0-2-4-5-7-8-10` fret sequence (not actually a G major scale) —
instead of calling `generate_artifact`, the schema-validated, AlphaTab-
rendered, ACTUALLY PLAYABLE artifact Plan 4 already built and shipped. A
hand-typed tab is unverifiable prose bluffing as a structured answer. This
module exists to CATCH that, never to correct it — silently "fixing" a wrong
tab by eye would just be a second, quieter bluff; the only acceptable fix is
"call the real tool", enforced by `loop.py`'s post-turn guard (see that
module for the suspend/re-prompt/fallback recovery this feeds into) and by
`SYSTEM_PROMPT`'s explicit ban (`app/agent/prompts.py`).

TWO independent shapes are detected by `looks_like_tablature`:

  1. THE STRING-LINE SHAPE — a line that is (ignoring leading/trailing
     whitespace) a single standard-tuning string letter — `e` (lowercase,
     the high string) or `E`/`A`/`D`/`G`/`B` (the other five) — immediately
     followed by `|` and then a run of tab-notation characters (digits,
     dashes, and the handful of technique symbols real ASCII tabs use:
     `~`/`h`/`p`/`b`/`/`/`\\` for slides/hammer-ons/pull-offs/bends).  This
     is Chris's EXACT bluff shape (`e|-----0-2-4-5-7-8-10-`), checked
     ANYWHERE in the text (not just inside a fence — a model could just as
     easily type it as bare prose), and requires >=2 matching lines (one
     alone could be coincidence; every real bluff, including Chris's, has
     several string lines).
  2. THE FENCED DASH/DIGIT BLOCK — >=2 lines *inside a ``` fence* that are
     ENTIRELY tab-notation characters with no letters/words/punctuation
     outside that set (and at least one digit). Catches a tab typed without
     the leading string-letter (e.g. mid-tab, or the letter dropped) while
     staying narrow: an ordinary code fence always has letters, `=`, `[`,
     `]`, or spaces this row shape rejects.

FALSE-POSITIVE DEFENCE (the brief's own explicit warning: "a FALSE POSITIVE
that suppresses a legitimate answer is its own bug") — see
`tests/test_agent_guards.py` for the pinned cases:
  - Shape 1 requires the letter IMMEDIATELY followed by `|`, nothing before
    it on that side. A markdown table row (`| E | A | D |`) always has the
    `|` FIRST (or a space before the letter), never `letter|` glued
    together, so it can never match. A table's separator row (`|---|---|`)
    starts with `|`, not a letter, for the same reason.
  - Shape 2 only ever looks INSIDE a ``` fence, and only counts a row that
    is PURELY tab-notation characters — one stray letter, a bracket, or a
    comma-and-space (`0, 2, 4`) disqualifies that row. `x = [0, 2, 4]` fails
    on every count (`x`, `=`, `[`, `]`, and the spaces around them).
  - Both shapes require at least one DIGIT in the tab-notation content — a
    line of bare dashes alone (e.g. a markdown `---` horizontal rule) is not
    a tab and does not count toward either shape's >=2 threshold.
  - Ordinary prose (no `|`, no fence) never reaches either regex at all.

PLAN 12 TASK 5 (G5) adds a SECOND, independent guard: `looks_like_named_song_
request`, for Chris's OTHER live bug — he asked for the "Smells Like Teen
Spirit" riff and got a real `generate_artifact` tab back (so `looks_like_
tablature` above never even fired), but its `alphaTex` was one note repeated
seven times. The model cannot actually recall a specific copyrighted
recording note-for-note; asked to produce one anyway, it invents — and a
confidently WRONG tab is worse than an honest "I can't do that". This is also
the right call on copyright: fabricating a transcription of a real, specific
recording is not something to paper over either.

THE DETECTOR, AND WHY IT'S NOT A SONG LIST: a hardcoded list of song titles
would be useless — it misses every song not already on it, which is to say
nearly every song. Instead this INVERTS the problem: `_GENERIC_MUSIC_TERMS`
is a closed, curated vocabulary of GENERIC musical language — note names,
scale/mode names, chord/progression words, technique words, generic genre
words, plus ordinary stopwords/connectives. Every one of the brief's WORKS
examples ("give me a G major scale tab", "a blues shuffle in E", "a 12-bar
blues progression", "an exercise for alternate picking") is made ENTIRELY of
words from this vocabulary. A request only trips the detector when it BOTH:
  1. names a `_TRIGGER_WORDS` term (riff/solo/lick/tab(s)/intro/outro/
     chorus/bridge/verse) — i.e. it's actually asking for a piece of music,
     not e.g. a bare theory/technique question that never reaches this guard
     at all; AND
  2. contains at least one word NOT in `_GENERIC_MUSIC_TERMS` — a word that
     can only be naming something SPECIFIC (a song title, a band), since
     every legitimate generic music-theory/style word one could genuinely
     need for an artifact request is already in that vocabulary.

This is deliberately biased toward over-declining (the brief's own words:
"an occasional over-decline that offers a real alternative is far less
harmful than a confidently fabricated tab") — a request naming an artist by
name ("in the style of Hendrix") also trips it, since "Hendrix" is no more
in the generic vocabulary than "Nirvana" is. That is an intentional,
accepted false-positive: an honest decline with a real alternative offered
is still a useful answer, unlike a confidently wrong one.

Checked against the FULL message text, not just the words immediately
around the trigger — these requests are short (a sentence, not a paragraph),
so there is no meaningful difference in practice, and scanning the whole
message avoids having to define "immediately around" as its own fuzzy rule.
"""
import re

from app.text.normalize import fold

# Shape 1: the string-line shape (Chris's exact bluff). Standard-tuning
# string letters ONLY (e/E/A/D/G/B) — not every letter of the alphabet —
# immediately followed by `|`, so a markdown table cell (always separated by
# whitespace/another `|`, never glued to a letter) can never match.
_STRING_LINE_RE = re.compile(
    r"(?m)^[ \t]*[eEADGB]\|[0-9\-~hpb/\\]*[0-9][0-9\-~hpb/\\]*[ \t]*$"
)

# Shape 2: rows inside a ``` fence. A row must be PURELY tab-notation
# characters (digits/dashes/technique symbols) with at least one digit —
# a stray letter, bracket, comma, or space disqualifies it.
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_DASH_DIGIT_ROW_RE = re.compile(r"^[0-9\-~hpb/\\]*[0-9][0-9\-~hpb/\\]*$")

_MIN_MATCHING_LINES = 2


def looks_like_tablature(text: str) -> bool:
    """True iff `text` contains a hand-typed ASCII guitar tab — either shape
    described in this module's docstring. Used by `app/agent/loop.py` as a
    post-turn guard: a positive here means the content must NOT be shown to
    the tutor as a final answer (see that module for the recovery).
    """
    if not text:
        return False

    if len(_STRING_LINE_RE.findall(text)) >= _MIN_MATCHING_LINES:
        return True

    for fence_body in _FENCE_RE.findall(text):
        rows = [ln.strip() for ln in fence_body.splitlines() if ln.strip()]
        matching = [ln for ln in rows if _DASH_DIGIT_ROW_RE.match(ln)]
        if len(matching) >= _MIN_MATCHING_LINES:
            return True

    return False


# ---------------------------------------------------------------------------
# Plan 12 Task 5 (G5): looks_like_named_song_request
# ---------------------------------------------------------------------------

# A request only trips the detector if it names one of these — i.e. it's
# actually asking for a piece of music (a riff/solo/lick/tab, or a named
# song SECTION), not a bare theory/technique question. These are also
# included in `_GENERIC_MUSIC_TERMS` below so the trigger word itself is
# never counted as the "specific" word that flags a request.
_TRIGGER_WORDS = {
    "riff", "riffs", "solo", "solos", "lick", "licks", "tab", "tabs",
    "intro", "outro", "chorus", "bridge", "verse",
    # Greek (folded — see app/text/normalize.py). Without these, a fully-Greek
    # request («φτιάξε μου την ταμπλατούρα του ...») contains no trigger at all
    # and the guard never even looks at it.
    "ταμπλατουρα", "ταμπλατουρες", "ταμπλατουρα", "ριφ", "σολο",
    "εισαγωγη", "ρεφρεν",
}

# The closed, curated GENERIC vocabulary — see this module's own top-level
# docstring for the full "why not a song list" rationale. Anything a
# legitimate generic musical-object request needs (a scale, a chord
# progression, a technique exercise, a generic style/genre) is a word in
# this set; anything else surviving in the request text is, definitionally,
# naming something SPECIFIC (a song, a band).
_GENERIC_MUSIC_TERMS = {
    # stopwords / connectives / imperative verbs
    "a", "an", "the", "of", "in", "on", "to", "for", "me", "my", "please",
    "give", "generate", "create", "make", "write", "play", "some", "this",
    "that", "out", "up", "with", "style", "styled", "like", "sounding",
    "one", "song", "piece", "tune", "using", "based", "and", "or",
    "from", "by", "as", "is", "it", "its", "you", "your",
    # note letters and qualities (single-letter note names are handled
    # separately below, but the words are listed here too for clarity)
    "sharp", "flat", "natural", "major", "minor",
    # scales / modes
    "scale", "scales", "pentatonic", "blues", "mode", "modes", "dorian",
    "mixolydian", "lydian", "phrygian", "locrian", "ionian", "aeolian",
    "harmonic", "melodic", "chromatic", "diminished", "augmented",
    # chords / progressions
    "chord", "chords", "progression", "progressions", "arpeggio",
    "arpeggios", "triad", "triads", "bar", "bars", "12-bar", "8-bar",
    "16-bar", "turnaround", "cadence", "shuffle",
    # techniques
    "exercise", "exercises", "technique", "techniques", "alternate",
    "picking", "hammer-on", "hammer", "pull-off", "pull", "slide",
    "sliding", "bend", "bends", "bending", "vibrato", "sweep", "tapping",
    "legato", "warm-up", "warmup", "fingerstyle", "strumming", "rhythm",
    "lead", "improvisation", "improv", "riffing",
    # generic genres/styles — NOT artist/band/song names
    "rock", "jazz", "funk", "country", "metal", "folk", "pop", "reggae",
    "punk", "ambient", "acoustic", "electric",
    # the trigger words themselves are neutral in context
    *_TRIGGER_WORDS,

    # -- GREEK (Plan 13, Task 5.2) -----------------------------------------
    #
    # `_WORD_RE` used to be `[a-z0-9]...` — ASCII-ONLY. Greek text was
    # therefore INVISIBLE to this guard, with a consequence nobody noticed:
    #
    #   «δώσε μου το tab του Ζεϊμπέκικο της Ευδοκίας»  -> NOT declined.
    #
    # A Greek-titled song sailed straight through and the model fabricated a
    # tab for it. For a GREEK tutor whose students ask for GREEK songs, the
    # guard was wide open in exactly the language that matters most — while
    # working perfectly for "Smells Like Teen Spirit", which is why every test
    # passed.
    #
    # Making `_WORD_RE` Unicode-aware fixes that, but it inverts the risk: with
    # Greek words suddenly visible, EVERY Greek word missing from this list
    # reads as "evidence of a song title", and the guard would false-decline
    # ordinary requests — making `generate_artifact` unreachable in the default
    # locale, which is far worse than the bug we are fixing.
    #
    # So this list is the closed generic Greek vocabulary, folded (unaccented,
    # lowercase — see `app/text/normalize.py`, because the accent MOVES under
    # inflection). `tests/test_greek_guards.py` asserts a broad set of
    # legitimate Greek requests are NOT declined; if a word is missing, that
    # test fails rather than the tutor discovering it.
    #
    # stopwords / articles / pronouns / prepositions
    "ο", "η", "το", "οι", "τα", "του", "της", "των", "τον", "την", "τους", "τις",
    "ενα", "μια", "ενας", "μιας", "ενος", "μου", "σου", "μας", "σας",
    "με", "σε", "για", "απο", "και", "στη", "στην", "στο", "στον", "στους",
    "στις", "στα", "που", "ως", "ειναι", "θελω", "θα", "να", "μπορεις",
    "παρακαλω", "καποιο", "καποια", "λιγο", "πιο", "οπως", "σαν", "χωρις",
    # imperative verbs — how a tutor actually asks
    "φτιαξε", "δωσε", "γραψε", "δειξε", "παιξε", "κανε", "δημιουργησε",
    "ετοιμασε", "βαλε", "εξηγησε", "μαθε",
    # note names (solfège — Greek uses these, not letters)
    "ντο", "ρε", "μι", "φα", "σολ", "λα", "σι",
    # qualities
    "διεση", "υφεση", "ματζορε", "μινορε", "μειζονα", "ελασσονα", "μειζων",
    "ελασσων",
    # scales / modes
    "κλιμακα", "κλιμακες", "πεντατονικη", "πεντατονικης", "μπλουζ",
    "τροπος", "τροποι", "δωριος", "μιξολυδιος", "λυδιος", "φρυγιος",
    "αρμονικη", "μελωδικη", "χρωματικη", "γκαμα",
    # chords / progressions
    "συγχορδια", "συγχορδιες", "συγχορδιων", "αρπεζ", "αρπισμος",
    "ακολουθια", "προοδος",
    # techniques
    "ασκηση", "ασκησεις", "τεχνικη", "τεχνικες", "ρυθμος", "ρυθμο",
    "μοτιβο", "ζεσταμα", "προθερμανση", "δαχτυλα", "πενα", "χορδη", "χορδες",
    "ταστο", "νοτα", "νοτες", "μελωδια", "αρμονια",
    # generic musical objects / levels
    "τραγουδι", "κομματι", "υφος", "στυλ", "μουσικη", "αρχαριος", "αρχαριο",
    "αρχαριους", "μεσαιο", "προχωρημενο", "επιπεδο",
    # inflected forms the tutor actually types (Greek declines everything)
    "ελασσονος", "μειζονος", "τεχνικης", "ασκησης", "κλιμακας", "πεντατονικης",
    "συγχορδιας", "χορδης", "ρυθμου", "υφους", "τραγουδιου", "κομματιου",
    "εναλλακτικη", "εναλλακτικο", "δωριο", "μιξολυδιο", "λυδιο", "φρυγιο",
    "αργο", "γρηγορο", "απλο", "ευκολο", "δυσκολο",
    # genre names as the tutor actually writes them — Greek transliterations.
    # "ενα riff δωδεκαμετρου μπλουζ" is as generic as "a 12-bar blues riff",
    # and "μπλουζ" not being in this set made it read as a song title.
    "μπλουζ", "ροκ", "τζαζ", "μεταλ", "φανκ", "ποπ", "ρεγκε", "σουινγκ",
    "δωδεκαμετρο", "δωδεκαμετρου", "οκταμετρο", "οκταμετρου",
    "σαφλ", "μπαλαντα", "μπαλαντας",
}

# FOLD THE VOCABULARY ITSELF, at import.
#
# The sets above are written the way a human writes Greek — with accents, and
# with the final sigma (ς). The INPUT, however, is folded before it is matched
# (`fold()` strips accents, unifies ς->σ, casefolds), because Greek accents move
# under inflection. So a literal `"της"` in the set could NEVER match the folded
# input `"τησ"`, and a literal `"ύφος"` could never match `"υφοσ"`.
#
# That is not a hypothetical: the first version of this list was hand-written
# and six legitimate Greek requests were false-declined by exactly this
# mismatch. Folding the vocabulary at load time is the fix, and it also means
# nobody adding a word later has to remember to hand-fold it — which they would
# not, and the failure would be a silent false decline.
_TRIGGER_WORDS = {fold(w) for w in _TRIGGER_WORDS}
_GENERIC_MUSIC_TERMS = {fold(w) for w in _GENERIC_MUSIC_TERMS}

# Word tokens: letters plus internal hyphens/apostrophes (so "hammer-on",
# "12-bar", "warm-up" tokenize as one word each, matching the multi-word
# entries above once digits are allowed too).
#
# `\w` with `re.UNICODE` (Python's default for str) matches GREEK letters too.
# It used to be `[a-z0-9]`, which made every Greek word invisible to this guard
# — see the long note in `_GENERIC_MUSIC_TERMS` above for what that cost.
_WORD_RE = re.compile(r"[^\W_][\w'-]*", re.UNICODE)



# CLAUSE boundaries: a compound instruction ("list students and make a tab")
# can legitimately contain words far from the trigger that are irrelevant to
# music entirely (e.g. "students") rather than either generic-music-term OR
# song-name evidence — the whole-message word scan would otherwise treat
# "students" as "specific" and wrongly decline a request that has nothing to
# do with a named song at all. Splitting on clause boundaries first and only
# scanning the clause that actually contains the trigger word keeps the
# check scoped to the part of the message that's actually naming a piece of
# music, not an unrelated command chained onto the same turn.
#
# GREEK CONJUNCTIONS SPLIT TOO — this ran on English ones only, so the exact
# Greek equivalent of the pinned English regression («δείξε μου τους μαθητές
# ΚΑΙ φτιάξε μια ταμπλατούρα...») scanned as ONE clause and the unrelated
# first half's words ("μαθητές") counted as song-title evidence: falsely
# declined in the default locale, fine in English. The input is folded before
# splitting, so the alternatives are folded forms (και, αλλα, μετα, ...); '·'
# (ano teleia) is Greek's clause-level punctuation and belongs with [.,;].
_CLAUSE_SPLIT_RE = re.compile(
    r"[.,;·]+"
    r"|\band\b|\bbut\b|\bthen\b"
    r"|\bκαι\b|\bαλλα\b|\bμετα\b|\bεπειτα\b|\bυστερα\b|\bοποτε\b",
    re.IGNORECASE,
)


def looks_like_named_song_request(text: str) -> bool:
    """True iff `text` plausibly asks for a NAMED SONG's tab/riff/solo — see
    this module's top-level docstring for the full detection rationale.
    Used by `app/agent/loop.py` as a PRE-model short-circuit (mirrors Plan 11
    Task 1's forced-retrieval pre-hop shape): a positive here means the turn
    is answered with `NAMED_SONG_DECLINE_MESSAGE` WITHOUT ever calling the
    model — there is no reliable way to make the model itself decline (it is
    the very thing that fabricates when asked), so the interception has to
    happen before it ever sees the request.
    """
    if not text:
        return False
    # FOLD, don't `.lower()`. Greek accents move under inflection (ταμπλατούρα
    # -> ταμπλατούρας) and `.lower()` keeps the accent, so an accented word
    # never matches an unaccented vocabulary entry. `fold` also unifies the
    # final sigma (ς/σ), without which every Greek word ending in -s misses.
    for clause in _CLAUSE_SPLIT_RE.split(fold(text)):
        words = _WORD_RE.findall(clause)
        if not any(w in _TRIGGER_WORDS for w in words):
            continue
        # Single-character words (almost always a bare note name — "E",
        # "A", ...) are never treated as "specific" on their own; only
        # length>=2 tokens outside the generic vocabulary count as evidence
        # of a named song.
        specific_words = [w for w in words if len(w) > 1 and w not in _GENERIC_MUSIC_TERMS]
        if specific_words:
            return True
    return False


NAMED_SONG_DECLINE_MESSAGE = (
    "I can't reproduce a specific recording's tab note-for-note — I don't "
    "actually have it memorized, and guessing would just invent a "
    "confidently wrong transcription instead of an honest answer. What I CAN "
    "generate for you: the chord progression in that style, a scale or "
    "technique exercise it draws on, or the riff's rhythmic shape as a generic "
    "pattern — just ask for one of those and I'll generate it as a real "
    "artifact."
)

# `app/routers/chat.py`'s `_inject_curriculum_context` appends a transient
# "[CURRICULUM CONTEXT — ...]" block (this exact prefix) onto the LAST user
# message so the model can reason about the bound curriculum. The G5 guard
# above must NEVER scan that block: a guitar course tree reliably contains
# trigger words ("Intro to Tone", "Solo riffs"), so scanning it declines
# EVERY revise-drawer turn with NAMED_SONG_DECLINE_MESSAGE, deterministically,
# no matter what the tutor typed — the 2026-07-19 "remove the inline
# citations" bug. The loop passes the raw tutor text explicitly
# (`raw_user_text`); this strip is the defense-in-depth fallback for any
# caller that doesn't.
CURRICULUM_CONTEXT_SENTINEL = "[CURRICULUM CONTEXT"


def strip_curriculum_context(text: str) -> str:
    """Return `text` without the trailing injected curriculum-context block.

    The block is always APPENDED (never prepended/interleaved — see
    `_inject_curriculum_context`), so everything from the first sentinel
    occurrence onward is injection, not tutor words.
    """
    idx = text.find(CURRICULUM_CONTEXT_SENTINEL)
    if idx == -1:
        return text
    return text[:idx].rstrip()
