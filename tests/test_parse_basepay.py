import pytest

from mcp_militarypay.parsers.basepay import (
    ParseError,
    extract_grade,
    extract_note_amount,
    parse_base_pay,
    parse_money,
    yos_label_to_min,
)


@pytest.mark.parametrize(
    "label,expected",
    [("2 or less", 0), ("Over 2", 2), ("Over 18", 18), ("Over 40", 40),
     ("Pay Grade", None), ("", None)],
)
def test_yos_label_to_min(label, expected):
    assert yos_label_to_min(label) == expected


def test_parse_money_treats_empty_cells_as_none_not_zero():
    """Blank cells mean the combination does not exist. Zero would be a lie."""
    assert parse_money("3,946.80") == 3946.80
    assert parse_money("$3,946.80") == 3946.80
    assert parse_money("") is None
    assert parse_money("   ") is None
    assert parse_money("--") is None


@pytest.mark.parametrize(
    "cell,expected",
    [("E-9 (Notes 2 & 3)", "E-9"), ("E-1 (Notes 4 & 5)", "E-1"),
     ("O-10", "O-10"), ("O1E", "O-1E"), ("W-5", "W-5"), ("Pay Grade", None)],
)
def test_extract_grade_ignores_footnote_markers(cell, expected):
    assert extract_grade(cell) == expected


def test_extract_note_amount_ignores_note_numbers():
    """'Note 2.' must not be read as a $2.00 pay rate."""
    assert extract_note_amount("Note 2. Basic pay is limited to level II.") is None
    assert extract_note_amount("Note 4. E-1 under 4 months: $2,225.70.") == 2225.70
    assert extract_note_amount("flat rate of $11,166.90 regardless of service") == 11166.90


class TestParseEnlistedPage:
    def test_reads_effective_year_and_all_grades(self, enlisted_html):
        table = parse_base_pay(enlisted_html, "enlisted")
        assert table.year == 2026
        assert set(table.grades) == {f"E-{i}" for i in range(1, 10)}
        assert table.warnings == []

    def test_known_published_value(self, enlisted_html):
        table = parse_base_pay(enlisted_html, "enlisted")
        assert table.rates[("E-5", 4)] == 3946.80

    def test_joins_the_two_half_tables_on_pay_grade(self, enlisted_html):
        """The grid is split across two <table> elements; both must be read."""
        table = parse_base_pay(enlisted_html, "enlisted")
        assert table.rates[("E-5", 0)] is not None    # first half-table
        assert table.rates[("E-5", 26)] is not None   # second half-table

    def test_nonexistent_combinations_are_null(self, enlisted_html):
        table = parse_base_pay(enlisted_html, "enlisted")
        assert table.rates[("E-9", 0)] is None
        assert table.rates[("E-8", 0)] is None
        assert table.rates[("E-8", 8)] == 1100.10

    def test_extracts_footnote_entitlement_rules(self, enlisted_html):
        table = parse_base_pay(enlisted_html, "enlisted")
        assert table.specials["e1_under_4_months"]["monthly_rate"] == 2225.70
        assert table.specials["senior_enlisted_advisor"]["monthly_rate"] == 11166.90

    def test_unrecognizable_page_raises(self):
        with pytest.raises(ParseError):
            parse_base_pay("<html><body><p>nothing here</p></body></html>", "enlisted")

    def test_missing_grades_are_warned_not_swallowed(self):
        html = """<html><body><p>Effective January 1, 2026</p><table>
        <tr><th>Pay Grade</th><th>2 or less</th><th>Over 2</th></tr>
        <tr><td>E-5</td><td>1.00</td><td>2.00</td></tr></table></body></html>"""
        table = parse_base_pay(html, "enlisted")
        assert any("missing expected pay grades" in w for w in table.warnings)


class TestNoteExtraction:
    """The live DFAS pages surround the real footnotes with site furniture, and
    put several notes inside one block. Both broke the first implementation."""

    def test_filters_breadcrumb_and_column_header_noise(self, enlisted_html):
        """Both contain the word "Note", so keywords alone let them through."""
        notes = parse_base_pay(enlisted_html, "enlisted").notes
        assert not any("payentitlements" in n.lower() for n in notes)
        # Only as a leading column header - the phrase legitimately appears
        # inside the senior advisor note ("regardless of cumulative years...").
        assert not any(
            n.lower().startswith("cumulative years of service") for n in notes
        )

    def test_splits_a_multi_note_block_into_individual_notes(self, enlisted_html):
        notes = parse_base_pay(enlisted_html, "enlisted").notes
        assert any(n.startswith("1.") for n in notes)
        assert any(n.startswith("2.") for n in notes)
        assert any(n.startswith("3.") for n in notes)

    def test_captures_senior_enlisted_advisor_inside_a_notes_block(self, enlisted_html):
        """This note lives inside a NOTES block that an earlier 600-character
        cap discarded, so the flat rate was silently never extracted."""
        specials = parse_base_pay(enlisted_html, "enlisted").specials
        assert specials["senior_enlisted_advisor"]["monthly_rate"] == 11166.90

    def test_executive_schedule_cap_has_no_rate_by_design(self, enlisted_html):
        """The cap references another schedule; a NULL rate is correct here."""
        from mcp_militarypay.sources import INFORMATIONAL_SPECIAL_KEYS

        specials = parse_base_pay(enlisted_html, "enlisted").specials
        assert specials["executive_schedule_cap"]["monthly_rate"] is None
        assert "executive_schedule_cap" in INFORMATIONAL_SPECIAL_KEYS


def test_split_numbered_notes_keeps_dollar_amounts_intact():
    """'$1,452.90. 2. Next' must not read '90.' as a note number."""
    from mcp_militarypay.parsers.basepay import _split_numbered_notes

    parts = _split_numbered_notes(
        "NOTES: 1. Cadet pay is $1,452.90. 2. Something else entirely."
    )
    assert len(parts) == 2
    assert parts[0] == "1. Cadet pay is $1,452.90."
    assert parts[1] == "2. Something else entirely."


def test_academy_cadet_rate_is_extracted():
    """The officer page carries a cadet/ROTC rate that is not on the grid."""
    from mcp_militarypay.parsers.basepay import _extract_specials

    specials = _extract_specials(
        ["1. Basic pay rate for Academy Cadets/Midshipmen and ROTC "
         "members/applicants is $1,452.90."],
        "officer",
    )
    assert specials["academy_cadet_rotc"]["monthly_rate"] == 1452.90
