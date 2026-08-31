"""Source URLs, grade vocabularies and column layouts for the published rate tables.

Everything the ingesters need to know about where the data lives and what shape
it arrives in. Nothing here performs I/O.
"""

from __future__ import annotations

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


def bah_ascii_url(year: int) -> str:
    return BAH_ASCII_URL_TEMPLATE.format(year=year)


def bah_inner_filenames(year: int) -> dict[str, str]:
    """Expected member names inside BAH-ASCII-<year>.zip.

    Verified for 2023 against the Makefile of mpyne-navy/bah-rate-map (MIT).
    The two-digit-year substitution for other years is inferred, so the reader
    falls back to matching by filename prefix when an exact name is absent.
    """
    yy = f"{year % 100:02d}"
    return {
        "zip_mha": BAH_ZIP_MHA_FILE.format(yy=yy),
        "with_dependents": BAH_WITH_DEPN_FILE.format(yy=yy),
        "without_dependents": BAH_WITHOUT_DEPN_FILE.format(yy=yy),
    }


# Column layout of bahw<yy>.txt / bahwo<yy>.txt.
#
# These are headerless CSV files (not fixed-width). DTMO publishes no schema for
# them; this layout is derived from the working reference consumer
# mpyne-navy/bah-rate-map (MIT, CDR Mike Pyne USN), whose index.html documents a
# sample row and slices it as E1-E9, W1-W5, O1E-O3E, O1-O10.
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
