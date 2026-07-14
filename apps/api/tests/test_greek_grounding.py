"""The forced-retrieval pre-hop must behave IDENTICALLY in Greek and English.

THE BUG THIS FILE PINS. `apps/web/src/i18n/routing.ts` sets `defaultLocale:
"el"`. `agent/loop.py::_QUESTION_RE` matched `?` and the English wh-words. A
Greek question ends in `;` and begins «Τι».

So `_is_content_bearing("Τι είναι το power chord;")` returned **False**, the
pre-hop never ran, and the model answered from general knowledge with no library
search — for every question the tutor actually asks, in the language he actually
uses. That is the mechanism behind his report that the app "just uses the llm
general knowledge". It was live, and it was invisible, because the English tests
all passed.

The invariant below is the real deliverable: for a translated PAIR, EN and EL
must return the same answer. A rule that is merely "better in Greek" would still
let the two languages drift; this one cannot. If someone later fixes the
(arguably over-broad) English `scales?` exclusion, these pairs force them to fix
Greek in the same commit.
"""
import pytest

from app.agent.loop import _is_content_bearing
from app.text.normalize import fold, has_greek

# (english, greek, expected) — the SAME question, twice.
PAIRS = [
    # --- genuine content questions: MUST be grounded -----------------------
    ("What is a power chord?", "Τι είναι το power chord;", True),
    ("How does pickup height affect tone?",
     "Πώς επηρεάζει το ύψος του μαγνήτη τον ήχο;", True),
    ("Why do tube amps sound warmer?",
     "Γιατί οι λυχνιάτοι ενισχυτές ακούγονται πιο ζεστοί;", True),
    ("What does my book say about overdrive?",
     "Τι λέει το βιβλίο μου για την υπερφόρτωση;", True),
    ("Which pick thickness should a beginner use?",
     "Ποιο πάχος πένας πρέπει να χρησιμοποιεί ένας αρχάριος;", True),

    # --- small talk: MUST NOT be grounded ----------------------------------
    ("hello", "γεια", False),
    ("thanks", "ευχαριστώ", False),
    ("how are you", "τι κάνεις", False),

    # --- entity/artifact instructions: MUST NOT be grounded ----------------
    # (resolved by a tool, not by the library — see loop.py's rule)
    ("split session 2 of that lesson", "χώρισε τη συνεδρία 2 του μαθήματος", False),
    ("show me my students", "δείξε μου τους μαθητές μου", False),
    ("make a tab of the A minor pentatonic",
     "φτιάξε μια ταμπλατούρα με την πεντατονική της Λα ελάσσονος", False),
    ("what is my progress with Nikos?", "ποια είναι η πρόοδος με τον Νίκο;", False),
]


@pytest.mark.parametrize("en,el,expected", PAIRS, ids=[p[0][:34] for p in PAIRS])
def test_english_and_greek_agree(en, el, expected):
    got_en = _is_content_bearing(en)
    got_el = _is_content_bearing(el)
    assert got_en == expected, f"EN {en!r} -> {got_en}, expected {expected}"
    assert got_el == expected, f"EL {el!r} -> {got_el}, expected {expected}"
    assert got_en == got_el, (
        f"EN/EL DIVERGED on the same question: {en!r}={got_en} vs {el!r}={got_el}. "
        f"The app must not behave differently depending on which language the "
        f"tutor typed in."
    )


# ---------------------------------------------------------------------------
# The specific regression, called out by name.
# ---------------------------------------------------------------------------

def test_the_greek_question_mark_is_recognised():
    """U+037E (the real Greek question mark) and U+003B (what keyboards actually
    emit) are both 'the Greek question mark' in practice. Neither is `?`."""
    assert _is_content_bearing("Τι είναι το power chord;")       # U+003B
    assert _is_content_bearing("Τι είναι το power chord;")       # U+037E


def test_a_semicolon_in_LATIN_text_is_not_a_question():
    """A bare `;` in English is a statement separator. Treating it as a question
    would force a library search on every multi-clause command."""
    assert not _is_content_bearing("split session 2; then rename it")


def test_greek_accents_move_under_inflection_and_still_match():
    """μάθημα -> μαθήματα: the tonos JUMPS from the alpha to the eta. A pattern
    written against one form silently misses the other, which is why the rule
    matches folded text."""
    assert fold("μάθημα") == "μαθημα"
    assert fold("μαθήματα") == "μαθηματα"
    # both are entity words -> both excluded, singular and plural alike
    assert not _is_content_bearing("δείξε μου το μάθημα;")
    assert not _is_content_bearing("δείξε μου τα μαθήματα;")


def test_an_unaccented_greek_question_still_grounds():
    """People type without accents. The app must not punish them for it."""
    assert _is_content_bearing("Τι ειναι το power chord;")


def test_has_greek_does_not_fire_on_english():
    assert has_greek("Τι είναι;")
    assert not has_greek("what is a power chord?")
