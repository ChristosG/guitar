"""The named-song guard, in Greek. Both directions, because both are dangerous.

WHAT WAS BROKEN. `_WORD_RE` was `[a-z0-9][a-z0-9'-]*` — ASCII-only — so Greek
text was INVISIBLE to the guard:

    «δώσε μου το tab του Ζεϊμπέκικο της Ευδοκίας»   -> NOT declined

A Greek-titled song sailed through and the model fabricated a tab for it. For a
Greek tutor whose students ask for Greek songs, the guard was open in exactly
the language that matters — while working perfectly for "Smells Like Teen
Spirit", which is why every existing test passed.

WHY THE FIX IS DANGEROUS, AND WHY THE SECOND HALF OF THIS FILE IS THE IMPORTANT
HALF. The guard's whole mechanism is: *a word outside the closed generic
vocabulary is evidence of a song title.* Making Greek words visible means every
Greek word missing from that vocabulary becomes "evidence". An incomplete list
does not fail gracefully — it false-declines ordinary requests, and a decline
means `generate_artifact` is UNREACHABLE. That is strictly worse than the bug
being fixed: a fabrication is visible and correctable; an app that refuses to
make a tab is just broken.

So `test_legitimate_greek_requests_are_NOT_declined` is the real gate. Every
phrasing a tutor might plausibly use belongs in it. If a word is missing from
`_GENERIC_MUSIC_TERMS`, this file fails — not the tutor's demo.
"""
import pytest

from app.agent.guards import looks_like_named_song_request as declines

# ---------------------------------------------------------------------------
# THE GATE: legitimate requests. A failure here means the app cannot make tabs.
# ---------------------------------------------------------------------------

LEGITIMATE = [
    # plain, as a tutor would type it
    "φτιάξε μου μια ταμπλατούρα με την πεντατονική κλίμακα σε Λα",
    "φτιάξε μια ταμπλατούρα με την πεντατονική της Λα ελάσσονος",
    "δώσε μου ένα σόλο στη μπλουζ κλίμακα",
    "γράψε μου ένα ριφ σε ύφος rock",
    "θέλω μια ταμπλατούρα με άσκηση τεχνικής για αρχάριους",
    "φτιάξε ένα tab με αρπέζ σε Ντο ματζόρε",
    "δείξε μου ένα ριφ με εναλλακτική πένα",
    "κάνε μου μια ταμπλατούρα για ζέσταμα",
    "παίξε μου ένα σόλο στη δώριο κλίμακα",
    "γράψε ένα ριφ σε ρυθμό μπλουζ",
    "φτιάξε μου μια ταμπλατούρα με συγχορδίες σε Μι",
    # unaccented — people type this way
    "φτιαξε μου μια ταμπλατουρα με την πεντατονικη κλιμακα",
    # mixed script, which is extremely common
    "φτιάξε μου ένα tab με την πεντατονική κλίμακα",
    "δώσε μου ένα solo exercise στη μπλουζ κλίμακα",
]


@pytest.mark.parametrize("text", LEGITIMATE, ids=[t[:38] for t in LEGITIMATE])
def test_legitimate_greek_requests_are_NOT_declined(text):
    assert not declines(text), (
        f"FALSE DECLINE on {text!r}. Some word here is missing from "
        f"_GENERIC_MUSIC_TERMS, so the guard read it as a song title. This "
        f"makes generate_artifact unreachable for that phrasing — add the word."
    )


# ---------------------------------------------------------------------------
# THE FIX: named songs, in Greek, are now caught.
# ---------------------------------------------------------------------------

NAMED_SONGS = [
    "δώσε μου το tab του Ζεϊμπέκικο της Ευδοκίας",
    "φτιάξε μου την ταμπλατούρα του Ζεϊμπέκικο της Ευδοκίας",
    "θέλω το ριφ από το Σ' αγαπάω γιατί είσαι ωραία",
    "γράψε μου το σόλο του Χατζιδάκι",
]


@pytest.mark.parametrize("text", NAMED_SONGS, ids=[t[:38] for t in NAMED_SONGS])
def test_greek_named_songs_are_declined(text):
    assert declines(text), (
        f"{text!r} was NOT declined — the model will fabricate a tab for a "
        f"specific copyrighted recording it cannot actually recall."
    )


# ---------------------------------------------------------------------------
# English must be unchanged. This whole change is additive.
# ---------------------------------------------------------------------------

def test_english_behaviour_is_unchanged():
    assert declines("give me the tab for Smells Like Teen Spirit")
    assert declines("play the solo from Comfortably Numb")
    assert not declines("make me a tab of the A minor pentatonic scale")
    assert not declines("generate a blues riff in the style of rock")


def test_the_original_regression_case():
    """The exact case from the module docstring: a Greek-titled song behind a
    Latin trigger word. Caught now; invisible before."""
    assert declines("δώσε μου το tab του Ζεϊμπέκικο της Ευδοκίας")
