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


class TestStatedRateExtraction:
    """A rate must be stated AS a basic pay rate.

    The live prior-enlisted officer page carries a combat zone tax exclusion
    note that names "the senior enlisted member (grade E-9)" and "($225)".
    Reading the first dollar figure out of a keyword-matched note reported the
    senior enlisted advisor rate as $225 instead of $11,166.90 - a plausible
    wrong number rather than a visible failure.
    """

    COMBAT_ZONE_NOTE = (
        "1. The amount of the maximum combat zone tax exclusion in effect for a "
        "qualifying month equals the sum of the basic pay for the senior enlisted "
        "member (grade E-9) payable (Basic Pay - Enlisted, Note 3) and the amount "
        "of hostile fire or imminent danger pay ($225) payable to the officer for "
        "the qualifying month."
    )
    REAL_SEA_NOTE = (
        "2. Basic pay for senior enlisted member (grade E-9) is $11,166.90 "
        "regardless of years of service while serving as: a. Senior Enlisted "
        "Advisor of the Chairman, Joint Chiefs of Staff;"
    )

    def test_reads_a_stated_basic_pay_rate(self):
        from mcp_militarypay.parsers.basepay import extract_stated_pay_rate

        assert extract_stated_pay_rate(self.REAL_SEA_NOTE) == 11166.90

    def test_refuses_an_incidental_dollar_figure(self):
        from mcp_militarypay.parsers.basepay import extract_stated_pay_rate

        assert extract_stated_pay_rate(self.COMBAT_ZONE_NOTE) is None

    def test_combat_zone_note_yields_no_special_at_all(self):
        from mcp_militarypay.parsers.basepay import _extract_specials

        assert _extract_specials([self.COMBAT_ZONE_NOTE], "officer_prior_enlisted") == {}

    def test_specials_are_gated_to_the_publishing_category(self):
        """Each page publishes its own rates; without this the pages clobber
        one another through the shared (year, key) primary key."""
        from mcp_militarypay.parsers.basepay import _extract_specials

        cadet = ("1. Basic pay rate for Academy Cadets/Midshipmen and ROTC "
                 "members/applicants is $1,452.90.")
        assert "academy_cadet_rotc" in _extract_specials([cadet], "officer")
        assert _extract_specials([cadet], "enlisted") == {}
        assert "senior_enlisted_advisor" in _extract_specials(
            [self.REAL_SEA_NOTE], "enlisted"
        )
        assert _extract_specials([self.REAL_SEA_NOTE], "warrant") == {}

    def test_a_rateless_match_never_displaces_a_real_rate(self):
        from mcp_militarypay.parsers.basepay import _extract_specials

        specials = _extract_specials(
            [self.REAL_SEA_NOTE, self.COMBAT_ZONE_NOTE], "enlisted"
        )
        assert specials["senior_enlisted_advisor"]["monthly_rate"] == 11166.90

    def test_real_page_wording_yields_the_right_rate(self, enlisted_html):
        specials = parse_base_pay(enlisted_html, "enlisted").specials
        assert specials["senior_enlisted_advisor"]["monthly_rate"] == 11166.90
        assert specials["e1_under_4_months"]["monthly_rate"] == 2225.70


def test_table_row_labels_are_not_footnotes():
    """The officer page put "O-1 (Notes 5, 6 & 7)" through the note filter."""
    from mcp_militarypay.parsers.basepay import _is_noise

    assert _is_noise("O-1 (Notes 5, 6 & 7)")
    assert _is_noise("E-9 (Note 2)")
    assert not _is_noise("1. Basic pay for an E-1 ... is $2,225.70.")


class TestAmountsWithoutThousandsSeparators:
    """Every DFAS figure is comma-formatted today, which is the only reason
    this went unnoticed: the pattern let the comma group and the decimals both
    be optional, so "11166.90" matched its first three characters and parsed as
    111.0 - a silently wrong entitlement rate."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("3,946.80", 3946.80), ("3946.80", 3946.80), ("$3946.80", 3946.80),
            ("11,166.90", 11166.90), ("11166.90", 11166.90),
            ("1452.90", 1452.90), ("476.95", 476.95),
            ("1,234,567.89", 1234567.89), ("2225.70", 2225.70),
        ],
    )
    def test_parse_money_takes_the_whole_number(self, text, expected):
        assert parse_money(text) == expected

    def test_stated_pay_rate_without_a_comma(self):
        from mcp_militarypay.parsers.basepay import extract_stated_pay_rate

        assert extract_stated_pay_rate(
            "Basic pay for senior enlisted member (grade E-9) is $11166.90 "
            "regardless of years of service"
        ) == 11166.90

    def test_note_amount_without_a_comma(self):
        assert extract_note_amount("E-1 rate is $2225.70.") == 2225.70

    def test_a_table_of_uncommatted_cells_parses_correctly(self):
        html = """<html><body><p>Effective January 1, 2026</p><table>
        <tr><th>Pay Grade</th><th>2 or less</th><th>Over 4</th></tr>
        <tr><td>E-5</td><td>3255.30</td><td>3946.80</td></tr></table></body></html>"""
        table = parse_base_pay(html, "enlisted")
        assert table.rates[("E-5", 0)] == 3255.30
        assert table.rates[("E-5", 4)] == 3946.80
