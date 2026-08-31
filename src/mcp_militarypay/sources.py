"""Source URLs, grade vocabularies and column layouts for the published rate tables.

Everything the ingesters need to know about where the data lives and what shape
it arrives in. Nothing here performs I/O.
"""

from __future__ import annotations

# Specials that legitimately carry no dollar amount: they point at another
# schedule rather than stating a rate, so a NULL here is correct, not a failed
# extraction.
INFORMATIONAL_SPECIAL_KEYS = frozenset({"executive_schedule_cap"})

DISCLAIMER = (
    "Unofficial. This server reads published DFAS/DTMO rate tables; it does not "
    "read anyone's Leave and Earnings Statement and it is not an authoritative "
    "source of pay. For actual pay questions contact DFAS at 1-888-332-7411 or "
    "use myPay (https://mypay.dfas.mil)."
)

# --- DFAS basic pay -------------------------------------------------------

DFAS_PAY_TABLE_INDEX = "https://www.dfas.mil/MilitaryMembers/payentitlements/Pay-Tables/"

BASE_PAY_SOURCES: dict[str, str] = {
    "enlisted": "https://www.dfas.mil/Military-Members/payentitlements/Pay-Tables/Basic-Pay/EM/",
    "officer": "https://www.dfas.mil/Military-Members/payentitlements/Pay-Tables/Basic-Pay/CO/",
    "officer_prior_enlisted": "https://www.dfas.mil/Military-Members/payentitlements/Pay-Tables/Basic-Pay/CO_FE/",
    "warrant": "https://www.dfas.mil/Military-Members/payentitlements/Pay-Tables/Basic-Pay/WO/",
}

BASE_PAY_CATEGORIES = tuple(BASE_PAY_SOURCES)

# Which pay grades each DFAS category is expected to publish. Used to fail loudly
# when a page comes back with an unexpected shape rather than writing garbage.
EXPECTED_GRADES: dict[str, tuple[str, ...]] = {
    "enlisted": tuple(f"E-{i}" for i in range(1, 10)),
    "officer": tuple(f"O-{i}" for i in range(1, 11)),
    "officer_prior_enlisted": ("O-1E", "O-2E", "O-3E"),
    "warrant": tuple(f"W-{i}" for i in range(1, 6)),
}

# The literal column headers on the DFAS pages, in order. The enlisted table is
# split across two HTML tables ('2 or less'..'Over 18', then 'Over 20'..'Over 40')
# which must be read and joined on pay grade.
YOS_COLUMN_LABELS: tuple[str, ...] = (
    "2 or less", "Over 2", "Over 3", "Over 4", "Over 6", "Over 8",
    "Over 10", "Over 12", "Over 14", "Over 16", "Over 18", "Over 20",
    "Over 22", "Over 24", "Over 26", "Over 28", "Over 30", "Over 32",
    "Over 34", "Over 36", "Over 38", "Over 40",
)

# --- BAS ------------------------------------------------------------------

BAS_SOURCE = "https://www.dfas.mil/Military-Members/payentitlements/Pay-Tables/bas/"

# --- DTMO BAH -------------------------------------------------------------

BAH_LOOKUP_PAGE = (
    "https://www.travel.dod.mil/Allowances/Basic-Allowance-for-Housing/BAH-Rate-Lookup/"
)

BAH_ASCII_URL_TEMPLATE = (
    "https://www.travel.dod.mil/Portals/119/Documents/BAH/"
    "BAH_Rates_All_Locations_All_Pay_Grades/ASCII/BAH-ASCII-{year}.zip"
)

# Inner filenames within the ASCII bundle, keyed off the two-digit year.
BAH_ZIP_MHA_FILE = "sorted_zipmha{yy}.txt"
BAH_WITH_DEPN_FILE = "bahw{yy}.txt"
BAH_WITHOUT_DEPN_FILE = "bahwo{yy}.txt"
BAH_MHA_NAMES_FILE = "mhanames{yy}.txt"


def bah_ascii_url(year: int) -> str:
    return BAH_ASCII_URL_TEMPLATE.format(year=year)


def bah_inner_filenames(year: int) -> dict[str, str]:
    """Expected member names inside BAH-ASCII-<year>.zip.

    Confirmed against the live 2026 bundle, which ships thirteen members: these
    four plus DTMO's own ASCII-FILE-FORMAT.pdf, .dat variants, and the previous
    publication under "- old" names. The reader falls back to a filename-prefix
    match when an exact name is absent, excluding the superseded members.
    """
    yy = f"{year % 100:02d}"
    return {
        "zip_mha": BAH_ZIP_MHA_FILE.format(yy=yy),
        "with_dependents": BAH_WITH_DEPN_FILE.format(yy=yy),
        "without_dependents": BAH_WITHOUT_DEPN_FILE.format(yy=yy),
        "mha_names": BAH_MHA_NAMES_FILE.format(yy=yy),
    }


# Column layout of bahw<yy>.txt / bahwo<yy>.txt.
#
# Headerless, comma-delimited files (not fixed-width). DTMO does ship a schema
# after all - ASCII-FILE-FORMAT.pdf, inside the bundle itself - which confirms
# the delimiter, the CHAR(5) MHA key, and this grade order, including the
# counterintuitive part: O1E/O2E/O3E come BEFORE O1 and onwards.
#
# One discrepancy. That PDF's field list ends at O7, giving 25 fields, and it
# runs off the bottom of its single page mid-list - so it appears truncated at
# the page break rather than deliberately stopping there. The published files
# carry 28 fields: the 2026 bundle parsed at exactly 28 across all 338 MHAs
# (18,252 rows = 338 x 27 grades x 2 dependency statuses, with no remainder), and
# a 25-field file would have failed the field-count check on line 1. The data
# wins; the extra columns are the senior officer grades that the DTMO rate
# lookup collapses into its "O-7/O-7+" bucket, and they carry the O-7 value.
#
# Field 0 is the MHA code; fields 1..27 are monthly rates in this grade order.
BAH_RATE_COLUMNS: tuple[str, ...] = (
    *(f"E-{i}" for i in range(1, 10)),      # cols 1..9
    *(f"W-{i}" for i in range(1, 6)),       # cols 10..14
    "O-1E", "O-2E", "O-3E",                 # cols 15..17
    *(f"O-{i}" for i in range(1, 11)),      # cols 18..27
)
BAH_ROW_FIELD_COUNT = 1 + len(BAH_RATE_COLUMNS)  # 28

# The DTMO lookup form collapses O-7 and above into a single "O-7/O-7+" bucket.
# The ASCII files still carry ten officer columns; O-7..O-10 simply hold the
# same value. Callers asking for O-8..O-10 get that value with this note.
BAH_SENIOR_OFFICER_GRADES = ("O-7", "O-8", "O-9", "O-10")

# MHA codes beginning 'ZZ' are nationwide "common" MHAs rather than a locality.
COMMON_MHA_PREFIX = "ZZ"


def normalize_pay_grade(grade: str) -> str:
    """Normalize user-supplied pay grade spellings to the canonical form.

    Accepts 'e5', 'E5', 'E-5', 'e-5' -> 'E-5'; 'o3e', 'O3E' -> 'O-3E'.
    Raises ValueError on anything unrecognized.
    """
    if not grade or not isinstance(grade, str):
        raise ValueError("pay_grade must be a non-empty string")
    g = grade.strip().upper().replace(" ", "").replace("-", "")
    if not g:
        raise ValueError("pay_grade must be a non-empty string")

    prior_enlisted = g.endswith("E") and len(g) > 2 and g[0] == "O"
    if prior_enlisted:
        g = g[:-1]

    branch, digits = g[0], g[1:]
    if branch not in ("E", "W", "O") or not digits.isdigit():
        raise ValueError(f"unrecognized pay grade: {grade!r}")

    number = int(digits)
    canonical = f"{branch}-{number}" + ("E" if prior_enlisted else "")

    valid = set(EXPECTED_GRADES["enlisted"]) | set(EXPECTED_GRADES["officer"]) \
        | set(EXPECTED_GRADES["officer_prior_enlisted"]) | set(EXPECTED_GRADES["warrant"])
    if canonical not in valid:
        raise ValueError(f"unrecognized pay grade: {grade!r}")
    return canonical


def category_for_grade(grade: str) -> str:
    """Map a canonical pay grade to its DFAS basic-pay table category."""
    for category, grades in EXPECTED_GRADES.items():
        if grade in grades:
            return category
    raise ValueError(f"no basic pay category for grade {grade!r}")
