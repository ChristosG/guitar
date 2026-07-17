from app.canon.compile import strip_printed_folio

def test_trailing_folio_stripped():
    # Gallagher physical p.86 shape: real prose, then a lone "62" folio
    t = "Wiring Options\n\nThough the coils of wire and magnets are the primary components...\n\n62"
    out = strip_printed_folio(t)
    assert "Wiring Options" in out
    assert not out.rstrip().endswith("62"), out[-30:]

def test_leading_folio_stripped():
    t = "62\n\nWiring Options\nThough the coils..."
    out = strip_printed_folio(t)
    assert out.lstrip().startswith("Wiring"), out[:30]

def test_number_in_prose_is_kept():
    t = "Use a 250k pot for a brighter tone; the 24th fret sits at the sweet spot."
    assert strip_printed_folio(t) == t

def test_page_ending_in_a_real_number_line_is_the_accepted_cost():
    # a rare table value alone on the last line WOULD be stripped — documented tradeoff
    t = "Resistance table\n47\n100\n250"
    # only the LAST bare-number line is removed, not the whole table
    out = strip_printed_folio(t)
    assert "47" in out and "100" in out
