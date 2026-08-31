"""Parser for the DFAS Basic Allowance for Subsistence (BAS) page.

One small table: three rate columns (Officer / Enlisted / BAS II) and one row
per effective date, with history already on the page back to 2011. All of it is
loaded - it costs nothing and makes prior years queryable.

BAS II is a conditional rate (unaccompanied government quarters without food
preparation facilities, no government mess available, requires Service Secretary
authorization). It is 2x the standard enlisted rate and is never a default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

_MONEY_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})")

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_MONTH_DAY_YEAR_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})"
)
_YEAR_ONLY_RE = re.compile(r"\b(19|20)(\d{2})\b")


class ParseError(ValueError):
    """The page did not look like the DFAS BAS table."""


@dataclass
class BasTable:
    source_url: str | None = None
    # effective_date -> (officer, enlisted, bas_ii)
    rows: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_effective_date(text: str) -> str | None:
    """'January 1, 2026' -> '2026-01-01'. A bare year becomes Jan 1 of it.

    BAS changes take effect on 1 January, so a year-only cell is unambiguous.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None

    match = _MONTH_DAY_YEAR_RE.search(cleaned)
    if match:
        month_name, day, year = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            try:
                return date(int(year), month, int(day)).isoformat()
            except ValueError:
                return None

    match = _YEAR_ONLY_RE.search(cleaned)
    if match:
        return f"{match.group(0)}-01-01"
    return None


def _cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def _money(text: str) -> float | None:
    match = _MONEY_RE.search(" ".join((text or "").split()))
    return float(match.group(1).replace(",", "")) if match else None


def parse_bas(html: str, *, source_url: str | None = None) -> BasTable:
    """Parse the BAS page into one row per effective date.

    Raises ParseError if no table on the page yields a usable row, so a
    reformatted page fails loudly rather than writing an empty history.
    """
    soup = BeautifulSoup(html, "lxml")
    result = BasTable(source_url=source_url)

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        # Locate the three rate columns by header label where a header exists.
        officer_col = enlisted_col = bas_ii_col = None
        header_index = -1
        for row_index, row in enumerate(rows[:3]):
            cells = row.find_all(["th", "td"])
            labels = [_cell_text(c).lower() for c in cells]
            for position, label in enumerate(labels):
                if "bas ii" in label or "bas-ii" in label:
                    bas_ii_col = position
                elif "officer" in label:
                    officer_col = position
                elif "enlisted" in label:
                    enlisted_col = position
            if officer_col is not None and enlisted_col is not None:
                header_index = row_index
                break

        for row in rows[header_index + 1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < 3:
                continue
            effective = parse_effective_date(_cell_text(cells[0]))
            if not effective:
                continue

            if officer_col is not None and enlisted_col is not None:
                officer = _money(_cell_text(cells[officer_col])) if officer_col < len(cells) else None
                enlisted = _money(_cell_text(cells[enlisted_col])) if enlisted_col < len(cells) else None
                bas_ii = (
                    _money(_cell_text(cells[bas_ii_col]))
                    if bas_ii_col is not None and bas_ii_col < len(cells)
                    else None
                )
            else:
                # No usable header: fall back to positional order, which the page
                # has always used (date, officer, enlisted, BAS II).
                amounts = [_money(_cell_text(c)) for c in cells[1:]]
                amounts = [a for a in amounts if a is not None]
                if len(amounts) < 2:
                    continue
                officer, enlisted = amounts[0], amounts[1]
                bas_ii = amounts[2] if len(amounts) > 2 else None

            if officer is None or enlisted is None:
                continue
            if bas_ii is None:
                # BAS II is defined as twice the standard enlisted rate.
                bas_ii = round(enlisted * 2, 2)
                result.warnings.append(
                    f"BAS II rate missing for {effective}; derived as 2x enlisted"
                )
            result.rows[effective] = (officer, enlisted, bas_ii)

    if not result.rows:
        raise ParseError(
            "no BAS rows found - the page layout has probably changed "
            "(expected a table of effective date, officer, enlisted, BAS II)"
        )
    return result
