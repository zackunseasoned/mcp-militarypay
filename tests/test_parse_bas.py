import pytest

from mcp_militarypay.parsers.bas import ParseError, parse_bas, parse_effective_date


@pytest.mark.parametrize(
    "text,expected",
    [("January 1, 2026", "2026-01-01"), ("Jan. 1, 2026", "2026-01-01"),
     ("2019", "2019-01-01"), ("", None), ("not a date", None)],
)
def test_parse_effective_date(text, expected):
    assert parse_effective_date(text) == expected


def test_parses_full_history(bas_html):
    table = parse_bas(bas_html)
    assert set(table.rows) == {"2026-01-01", "2025-01-01", "2011-01-01"}
    assert table.warnings == []


def test_known_published_2026_values(bas_html):
    officer, enlisted, bas_ii = parse_bas(bas_html).rows["2026-01-01"]
    assert (officer, enlisted, bas_ii) == (328.48, 476.95, 953.90)


def test_bas_ii_is_twice_enlisted(bas_html):
    _, enlisted, bas_ii = parse_bas(bas_html).rows["2026-01-01"]
    assert bas_ii == pytest.approx(enlisted * 2)


def test_derives_bas_ii_when_column_absent():
    html = """<html><body><table>
      <tr><th>Effective Date</th><th>Officer</th><th>Enlisted</th></tr>
      <tr><td>January 1, 2026</td><td>$328.48</td><td>$476.95</td></tr>
    </table></body></html>"""
    table = parse_bas(html)
    assert table.rows["2026-01-01"][2] == pytest.approx(953.90)
    assert any("derived as 2x enlisted" in w for w in table.warnings)


def test_unrecognizable_page_raises():
    with pytest.raises(ParseError):
        parse_bas("<html><body><p>no table</p></body></html>")
