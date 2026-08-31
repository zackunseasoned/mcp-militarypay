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
