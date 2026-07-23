"""`curriculum/sanitize.strip_inline_citations` — the net under the style rule.

The contract cuts both ways and both halves are tested here: every shape of
inline source/page reference the models have actually produced is removed, and
the parenthetical text a lesson legitimately contains — timings the model
writes into prose, tunings, years — is never touched. A stripper that matched
on "any parens with a number" would silently corrupt ordinary teaching text.
"""
from app.curriculum.draft import _render_section
from app.curriculum.sanitize import strip_inline_citations


# ---------------------------------------------------------------------------
# What must be removed
# ---------------------------------------------------------------------------

def test_the_classic_source_page_ref_goes():
    assert strip_inline_citations(
        "η τιμή των ποτενσιόμετρων έχει σημασία (S9, p.47)."
    ) == "η τιμή των ποτενσιόμετρων έχει σημασία."


def test_every_marker_shape_the_models_write():
    cases = {
        "κείμενο (S9, p.47) συνέχεια": "κείμενο συνέχεια",
        "κείμενο (S9 p.47) συνέχεια": "κείμενο συνέχεια",
        "κείμενο [p.12] συνέχεια": "κείμενο συνέχεια",
        "κείμενο [p. 12] συνέχεια": "κείμενο συνέχεια",
        "κείμενο (pp. 12-14) συνέχεια": "κείμενο συνέχεια",
        "κείμενο (σελ. 47) συνέχεια": "κείμενο συνέχεια",
        "κείμενο (σ. 12) συνέχεια": "κείμενο συνέχεια",
        "κείμενο (page 47) συνέχεια": "κείμενο συνέχεια",
        "κείμενο (Tone Manual, p. 47) συνέχεια": "κείμενο συνέχεια",
        "κείμενο [Tone Manual, p.47] συνέχεια": "κείμενο συνέχεια",
        "κείμενο (βλ. σελ. 12) συνέχεια": "κείμενο συνέχεια",
    }
    for given, expected in cases.items():
        assert strip_inline_citations(given) == expected, given


def test_bare_source_refs_go_too():
    assert strip_inline_citations("όπως είδαμε (S9) νωρίτερα") == "όπως είδαμε νωρίτερα"
    assert strip_inline_citations("και τα δύο βιβλία (S1, S3) συμφωνούν") == (
        "και τα δύο βιβλία συμφωνούν"
    )
    assert strip_inline_citations("δες [S2] για λεπτομέρειες") == "δες για λεπτομέρειες"


def test_a_page_range_with_an_en_dash():
    assert strip_inline_citations("το κεφάλαιο (pp. 12–14) εξηγεί") == "το κεφάλαιο εξηγεί"


def test_punctuation_is_tidied_after_the_removal():
    # The marker sat before the full stop — removing it must not leave "λέξη ."
    assert strip_inline_citations("Αυτό λέει το βιβλίο (S9, p.47) .") == (
        "Αυτό λέει το βιβλίο."
    )


def test_a_citation_alone_on_its_own_line_takes_its_line_with_it():
    assert strip_inline_citations("Πρώτη γραμμή.\n(S9, p.47)") == "Πρώτη γραμμή."


# ---------------------------------------------------------------------------
# What must NEVER be removed
# ---------------------------------------------------------------------------

def test_exercise_timings_survive():
    # Rendered heads no longer print minutes (Chris, 2026-07-23), but a timing
    # the model writes into PROSE is ordinary text — never the stripper's prey.
    assert strip_inline_citations("Χρωματική άσκηση (5 min)") == "Χρωματική άσκηση (5 min)"


def test_ordinary_parentheticals_survive():
    for text in (
        "κούρδισμα (Drop D) πριν ξεκινήσουμε",
        "η Les Paul (1959) που ακούς εδώ",
        "παίξε τη συγχορδία (Am7) αργά",
        "ο ενισχυτής (Amp 5150) του βιβλίου",   # 'p' inside a word is not a page marker
        "τρίτο ριφ (Riff 3) του κομματιού",
    ):
        assert strip_inline_citations(text) == text, text


def test_newlines_between_sections_of_prose_survive():
    given = "Πρώτη παράγραφος (S9, p.47).\n\nΔεύτερη παράγραφος."
    assert strip_inline_citations(given) == "Πρώτη παράγραφος.\n\nΔεύτερη παράγραφος."


def test_empty_and_none_are_safe():
    assert strip_inline_citations("") == ""
    assert strip_inline_citations(None) == ""


# ---------------------------------------------------------------------------
# The live seam: `_render_section` strips, and only where it should
# ---------------------------------------------------------------------------

def test_render_section_strips_markers_and_prints_no_minutes_head():
    section = {
        "body": "Όπως εξηγεί το βιβλίο (S9, p.47), το ξύλο μετράει.",
        "items": [
            {"title": "Άσκηση καθαρού ήχου", "est_minutes": 5,
             "instructions": "Δούλεψε τη δεξιά παλάμη [p.12] στο palm muting."},
        ],
        "citations": [{"source_id": "S9", "page": 47}],
    }
    body = _render_section("theory", section)
    assert "(S9, p.47)" not in body
    assert "[p.12]" not in body
    # `est_minutes` stays in the schema (the model budgets with it) but the
    # page prints only the SECTION clock — three exercises stamped 10+10+8
    # under a 3′ section header read as a contradiction (Chris, 2026-07-23).
    assert "(5 min)" not in body
    assert "Άσκηση καθαρού ήχου" in body
    assert "Όπως εξηγεί το βιβλίο, το ξύλο μετράει." in body
    # The STRUCTURED citations are not this function's business — untouched.
    assert section["citations"] == [{"source_id": "S9", "page": 47}]
