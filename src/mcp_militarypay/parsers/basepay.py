"""Parser for the four DFAS basic pay HTML pages.

Layout is pay grade (rows) x cumulative years of service (columns). Each page
splits the grid across two HTML tables ('2 or less'..'Over 18', then
'Over 20'..'Over 40') which are joined on pay grade.

The DFAS pages get reformatted, so this parses defensively: grades are regexed
out of cell text (which carries footnote markers like 'E-9 (Notes 2 & 3)'),
columns are matched by header label rather than position, and an unexpected
table shape raises rather than writing garbage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..sources import EXPECTED_GRADES, YOS_COLUMN_LABELS

# 'E-9 (Notes 2 & 3)' -> E-9 ; 'O1E' -> O-1E ; 'W-5' -> W-5
_GRADE_RE = re.compile(r"^\s*\**\s*([EWO])\s*-?\s*(\d{1,2})\s*(E)?\b", re.IGNORECASE)

# '3,946.80' / '$3,946.80' / '11166.90'
_MONEY_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2})")

_EFFECTIVE_RE = re.compile(
    r"effective\s+(?:january\s+1,?\s*)?(\d{4})", re.IGNORECASE
)

_EMPTY_CELL_TOKENS = {"", "-", "--", "---", "n/a", "na", "—", "–"}

_NOTE_KEYWORDS = (
    "note", "basic pay", "less than", "senior enlisted", "advisor",
    "executive schedule", "limited to", "cadet", "midshipm", "rotc",
    "rate for", "pay rate",
)

# Site furniture that sits in prose-like elements and survives a keyword filter:
# breadcrumb navigation, and the table's own column-group header (which contains
# the word "Note" because it points at the footnotes).
_NOISE_RE = re.compile(
    r"payentitlements"
    r"|pay\s+tables\s+basic\s+pay"
    r"|home\s+militarymembers"
    r"|^cumulative\s+years\s+of\s+service"
    r"|^effective\s+\w+\s+\d",
    re.IGNORECASE,
)

_NOTES_LABEL_RE = re.compile(r"^\s*notes?\s*:?\s*", re.IGNORECASE)

# Split before "1. ", "2. " etc. The lookbehind stops "$1,452.90. " being read
# as a note number.
_NOTE_SPLIT_RE = re.compile(r"(?<![\d.,$])(?=\b\d{1,2}\.\s+[A-Z])")

# Dollar amounts inside footnote prose. Deliberately stricter than _MONEY_RE:
# note text is full of ordinals ('Note 2.', 'level II') that a loose number
# match would happily mistake for a pay rate. Requires either an explicit $ or
# a comma-grouped figure with cents.
_NOTE_DOLLAR_RE = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})"
)
_NOTE_GROUPED_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+\.\d{2})\b")
_NOTE_PREFIX_RE = re.compile(r"^\s*note\s*\d+\s*[.:)]?\s*", re.IGNORECASE)


def extract_note_amount(text: str) -> float | None:
    """Read a dollar figure out of footnote prose, or None if there isn't one.

    Never falls back to a bare integer: a missing rate is reported as missing
    rather than guessed from a note number.
    """
    body = _NOTE_PREFIX_RE.sub("", text or "")
    match = _NOTE_DOLLAR_RE.search(body) or _NOTE_GROUPED_RE.search(body)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


class ParseError(ValueError):
    """The page did not look like a DFAS basic pay table."""


@dataclass
class BasePayTable:
    """One parsed DFAS basic pay page."""

    category: str
    year: int | None
    source_url: str | None = None
    # (pay_grade, yos_min) -> monthly rate or None where the combination
    # does not exist (empty cell on the page; NULL, never zero).
    rates: dict[tuple[str, int], float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    specials: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def grades(self) -> list[str]:
        seen: dict[str, None] = {}
        for grade, _ in self.rates:
            seen.setdefault(grade, None)
        return list(seen)

    def row_count(self) -> int:
        return len(self.rates)


def yos_label_to_min(label: str) -> int | None:
    """'2 or less' -> 0, 'Over 18' -> 18. None if the label isn't a YOS column."""
    text = " ".join(label.split()).lower()
    if not text:
        return None
    if "or less" in text:
        match = re.search(r"(\d+)", text)
        return 0 if match else None
    if text.startswith("over"):
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None
    return None


def parse_money(text: str) -> float | None:
    """Parse a table cell into a rate. Empty cells return None, never 0.0."""
    cleaned = " ".join((text or "").split())
    if cleaned.lower() in _EMPTY_CELL_TOKENS:
        return None
    match = _MONEY_RE.search(cleaned)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def extract_grade(text: str) -> str | None:
    """Pull a canonical pay grade out of a row-label cell."""
    cleaned = " ".join((text or "").split())
    match = _GRADE_RE.match(cleaned)
    if not match:
        return None
    branch, number, prior_enlisted = match.groups()
    grade = f"{branch.upper()}-{int(number)}"
    if prior_enlisted:
        grade += "E"
    return grade


def _cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def _parse_one_table(table) -> tuple[dict[str, int], dict[tuple[str, int], float | None]]:
    """Parse a single <table> into (header label->column index, cell values).

    Returns empty dicts for tables on the page that aren't a pay grid.
    """
    rows = table.find_all("tr")
    if not rows:
        return {}, {}

    # Find the header row: the first row whose cells contain YOS labels.
    header_index = None
    columns: dict[int, int] = {}   # cell position -> yos_min
    for row_index, row in enumerate(rows[:4]):
        cells = row.find_all(["th", "td"])
        found: dict[int, int] = {}
        for position, cell in enumerate(cells):
            yos = yos_label_to_min(_cell_text(cell))
            if yos is not None:
                found[position] = yos
        if len(found) >= 2:
            header_index, columns = row_index, found
            break

    if header_index is None:
        return {}, {}

    values: dict[tuple[str, int], float | None] = {}
    for row in rows[header_index + 1:]:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        grade = extract_grade(_cell_text(cells[0]))
        if grade is None:
            continue
        for position, yos in columns.items():
            if position >= len(cells):
                continue
            values[(grade, yos)] = parse_money(_cell_text(cells[position]))

    return {str(v): k for k, v in columns.items()}, values


def _split_numbered_notes(text: str) -> list[str]:
    """Split a "NOTES: 1. ... 2. ..." block into individual notes.

    The lookbehind keeps dollar amounts intact: in "...is $1,452.90. 2. Next",
    the "90." must not be mistaken for a note number.
    """
    body = _NOTES_LABEL_RE.sub("", text).strip()
    parts = [p.strip() for p in _NOTE_SPLIT_RE.split(body) if p.strip()]
    return parts if len(parts) > 1 else ([body] if body else [])


def _is_noise(text: str) -> bool:
    """Reject site furniture that surrounds the real footnotes.

    The DFAS pages put breadcrumb navigation and the table's own column-group
    header in elements that otherwise look like prose, and both contain the word
    "Note", so a keyword filter alone lets them through.
    """
    return bool(_NOISE_RE.search(text))


def _extract_notes(soup: BeautifulSoup) -> list[str]:
    """Collect footnote text from the page.

    These footnotes are real entitlement logic (the E-1 under-4-months rate, the
    senior enlisted advisor flat rate, the cadet/ROTC rate), not trivia, so they
    are kept verbatim. A whole "NOTES:" block is captured and then split into
    individual notes: the block on some pages runs to a few thousand characters,
    which an earlier length cap silently discarded.
    """
    notes: list[str] = []
    seen: set[str] = set()

    for element in soup.find_all(["p", "li", "span", "div", "td"]):
        if element.find(["p", "li", "table"]):
            continue
        text = " ".join(element.get_text(" ", strip=True).split())
        if not (20 <= len(text) <= 6000):
            continue
        if _is_noise(text):
            continue
        lowered = text.lower()
        if not any(word in lowered for word in _NOTE_KEYWORDS):
            continue

        for note in _split_numbered_notes(text):
            if len(note) < 20 or _is_noise(note):
                continue
            if note in seen:
                continue
            seen.add(note)
            notes.append(note)

    return notes


def _extract_specials(notes: list[str], category: str) -> dict[str, dict]:
    """Pull flat rates that override the YOS grid out of the footnote text.

    Never invents a figure: if the note is present but no dollar amount can be
    read out of it, monthly_rate stays None and the note text still surfaces.
    """
    specials: dict[str, dict] = {}
    for note in notes:
        lowered = note.lower()

        if re.search(r"\be-?1\b", lowered) and re.search(
            r"less than\s*(?:4|four)\s*months", lowered
        ):
            specials["e1_under_4_months"] = {
                "category": category,
                "pay_grade": "E-1",
                "label": "E-1 with less than 4 months of active duty",
                "monthly_rate": extract_note_amount(note),
                "note": note,
            }

        if "senior enlisted" in lowered or re.search(
            r"\b(seac|sma|mcpon|cmsaf|smmc|cmssf|mcpocg)\b", lowered
        ):
            specials["senior_enlisted_advisor"] = {
                "category": category,
                "pay_grade": None,
                "label": (
                    "Senior enlisted advisor billets (SEAC, SMA, MCPON, CMSAF, "
                    "SMMC, CMSSF, MCPOCG, SEA to CNGB) - flat rate regardless of "
                    "years of service"
                ),
                "monthly_rate": extract_note_amount(note),
                "note": note,
            }

        # Service academy cadets/midshipmen and ROTC members are paid a flat
        # rate that is not on the officer grid at all.
        if re.search(r"academy cadet|midshipm|\brotc\b", lowered):
            specials["academy_cadet_rotc"] = {
                "category": category,
                "pay_grade": None,
                "label": (
                    "Service academy cadets/midshipmen and ROTC members/"
                    "applicants - flat rate, not on the pay grade table"
                ),
                "monthly_rate": extract_note_amount(note),
                "note": note,
            }

        # The statutory cap: basic pay may not exceed Executive Schedule level II.
        if "executive schedule" in lowered:
            specials["executive_schedule_cap"] = {
                "category": category,
                "pay_grade": None,
                "label": (
                    "Basic pay is capped at the rate for level II of the "
                    "Executive Schedule"
                ),
                "monthly_rate": extract_note_amount(note),
                "note": note,
            }
    return specials


def parse_base_pay(html: str, category: str, *, source_url: str | None = None) -> BasePayTable:
    """Parse one DFAS basic pay page.

    Raises ParseError when the page shape is unrecognizable, so a reformatted
    page fails loudly instead of writing a half-empty table.
    """
    if category not in EXPECTED_GRADES:
        raise ValueError(f"unknown basic pay category {category!r}")

    soup = BeautifulSoup(html, "lxml")

    year = None
    match = _EFFECTIVE_RE.search(soup.get_text(" ", strip=True))
    if match:
        year = int(match.group(1))

    result = BasePayTable(category=category, year=year, source_url=source_url)

    for table in soup.find_all("table"):
        _, values = _parse_one_table(table)
        result.rates.update(values)   # joins the two half-tables on pay grade

    if not result.rates:
        raise ParseError(
            f"no basic pay grid found on the {category} page - the layout has "
            f"probably changed (expected tables with columns like "
            f"{YOS_COLUMN_LABELS[0]!r}/{YOS_COLUMN_LABELS[1]!r})"
        )

    result.notes = _extract_notes(soup)
    result.specials = _extract_specials(result.notes, category)

    expected = set(EXPECTED_GRADES[category])
    found = set(result.grades)
    missing = expected - found
    unexpected = found - expected
    if missing:
        result.warnings.append(
            f"missing expected pay grades for {category}: {sorted(missing)}"
        )
    if unexpected:
        result.warnings.append(
            f"unexpected pay grades on the {category} page: {sorted(unexpected)}"
        )
    if year is None:
        result.warnings.append(
            "could not read an effective year off the page ('Effective January 1, YYYY')"
        )

    return result
