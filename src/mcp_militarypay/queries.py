"""Read-only lookups behind the MCP tools.

Every function here is a parameterized SELECT against the local database. No
HTML, CSV or HTTP in this path. Every return value carries the effective date
and source URL of the numbers used: these are entitlement figures and someone
will act on them.
"""

from __future__ import annotations

import sqlite3

import re

from .sources import (
    BAH_SENIOR_OFFICER_GRADES,
    COMMON_MHA_PREFIX,
    DISCLAIMER,
    category_for_grade,
    normalize_pay_grade,
)


_MHA_CODE_RE = re.compile(r"^[A-Z]{2}\d{3}$")


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a query of '100%' searches for that literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class LookupError_(ValueError):
    """A lookup could not be answered from the data present."""


# --- helpers --------------------------------------------------------------

def latest_base_pay_year(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(year) AS y FROM base_pay").fetchone()
    return row["y"] if row and row["y"] is not None else None


def latest_bah_year(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(year) AS y FROM bah_rate_set").fetchone()
    return row["y"] if row and row["y"] is not None else None


def available_base_pay_years(conn: sqlite3.Connection) -> list[int]:
    return [r["year"] for r in conn.execute(
        "SELECT DISTINCT year FROM base_pay ORDER BY year DESC")]


def _base_pay_source_url(conn: sqlite3.Connection, category: str) -> str | None:
    row = conn.execute(
        "SELECT url FROM source_fetch_log WHERE source = ? AND ok = 1 "
        "ORDER BY id DESC LIMIT 1", (f"base_pay:{category}",)
    ).fetchone()
    return row["url"] if row else None


# --- basic pay ------------------------------------------------------------

def get_base_pay(
    conn: sqlite3.Connection,
    pay_grade: str,
    years_of_service: float,
    year: int | None = None,
    *,
    months_active_duty: int | None = None,
    senior_enlisted_advisor: bool = False,
) -> dict:
    """Monthly basic pay for a grade at a years-of-service band.

    Handles the two footnote rules that make the difference between a toy and a
    number someone can act on:
      * an E-1 with less than 4 months of active duty is paid a different (lower)
        rate than the E-1 table value;
      * senior enlisted advisor billets are a flat rate regardless of service.

    A grade/YOS combination that does not exist (e.g. E-8 at 2 years) is reported
    as invalid rather than as $0.
    """
    grade = normalize_pay_grade(pay_grade)
    category = category_for_grade(grade)

    if years_of_service is None or years_of_service < 0:
        raise LookupError_("years_of_service must be zero or greater")

    year = year or latest_base_pay_year(conn)
    if year is None:
        raise LookupError_(
            "no basic pay data loaded. Run: python -m mcp_militarypay.cli ingest --all"
        )

    notes = [
        row["note"] for row in conn.execute(
            "SELECT note FROM base_pay_note WHERE year = ? AND category = ? "
            "AND (pay_grade IS NULL OR pay_grade = ?)",
            (year, category, grade),
        )
    ]
    source_url = _base_pay_source_url(conn, category)

    result = {
        "pay_grade": grade,
        "category": category,
        "year": year,
        "years_of_service": years_of_service,
        "source_url": source_url,
        "effective_date": f"{year}-01-01",
        "notes": notes,
        "disclaimer": DISCLAIMER,
    }

    # Flat rates that override the years-of-service grid entirely.
    if senior_enlisted_advisor and grade != "E-9":
        # DFAS publishes this as the rate for "senior enlisted member (grade
        # E-9)". Applying it to any other grade would return an E-9 figure
        # under someone else's pay grade.
        result["warning"] = (
            f"senior_enlisted_advisor applies only to E-9; ignoring it for "
            f"{grade} and using the published table rate"
        )
        senior_enlisted_advisor = False

    if senior_enlisted_advisor:
        special = conn.execute(
            "SELECT * FROM base_pay_special WHERE year = ? AND key = ?",
            (year, "senior_enlisted_advisor"),
        ).fetchone()
        if special and special["monthly_rate"] is not None:
            result.update(
                monthly_rate=special["monthly_rate"],
                rate_basis="senior_enlisted_advisor_flat_rate",
                applied_note=special["note"],
            )
            return result
        result["warning"] = (
            "senior_enlisted_advisor was requested but no flat rate is stored "
            f"for {year}; falling back to the standard table value"
        )

    if grade == "E-1" and months_active_duty is not None and months_active_duty < 4:
        special = conn.execute(
            "SELECT * FROM base_pay_special WHERE year = ? AND key = ?",
            (year, "e1_under_4_months"),
        ).fetchone()
        if special and special["monthly_rate"] is not None:
            result.update(
                monthly_rate=special["monthly_rate"],
                rate_basis="e1_under_4_months",
                applied_note=special["note"],
                months_active_duty=months_active_duty,
            )
            return result
        result["warning"] = (
            "an E-1 with under 4 months of active duty is paid a lower rate, but "
            f"no such rate is stored for {year}; the table value below is the "
            "4-months-and-over rate"
        )

    # Banding: the highest published YOS threshold at or below the member's
    # service. Storing yos_min as an integer makes this correct for free.
    row = conn.execute(
        "SELECT yos_min, monthly_rate FROM base_pay "
        "WHERE year = ? AND category = ? AND pay_grade = ? AND yos_min <= ? "
        "ORDER BY yos_min DESC LIMIT 1",
        (year, category, grade, years_of_service),
    ).fetchone()

    if row is None:
        exists = conn.execute(
            "SELECT 1 FROM base_pay WHERE year = ? AND pay_grade = ? LIMIT 1",
            (year, grade),
        ).fetchone()
        if not exists:
            raise LookupError_(
                f"no {year} basic pay data for pay grade {grade}"
            )
        raise LookupError_(
            f"{grade} has no published basic pay rate at {years_of_service} "
            f"years of service for {year}"
        )

    if row["monthly_rate"] is None:
        result.update(
            monthly_rate=None,
            rate_basis="not_a_valid_combination",
            yos_band_min=row["yos_min"],
            explanation=(
                f"{grade} at {years_of_service} years of service is not a "
                f"published combination on the {year} basic pay table - the cell "
                f"is blank, which means the combination does not exist, not that "
                f"the rate is zero."
            ),
        )
        return result

    result.update(
        monthly_rate=row["monthly_rate"],
        annual_rate=round(row["monthly_rate"] * 12, 2),
        rate_basis="table",
        yos_band_min=row["yos_min"],
    )
    return result


# --- BAH ------------------------------------------------------------------

def resolve_zip(conn: sqlite3.Connection, zip_code: str, year: int) -> str:
    """ZIP -> MHA using the annual baseline crosswalk for that year.

    Off-cycle rate sets ship no crosswalk of their own, so resolution always
    goes through the year's annual publication.
    """
    zip_norm = str(zip_code).strip()
    if not zip_norm.isdigit() or len(zip_norm) > 5:
        raise LookupError_(f"{zip_code!r} is not a 5-digit US ZIP code")
    zip_norm = zip_norm.zfill(5)

    row = conn.execute(
        "SELECT z.mha_code FROM zip_to_mha z "
        "JOIN bah_rate_set s ON s.id = z.rate_set "
        "WHERE z.zip_code = ? AND s.year = ? AND s.is_annual_baseline = 1 "
        "ORDER BY s.effective_date DESC LIMIT 1",
        (zip_norm, year),
    ).fetchone()
    if row is None:
        raise LookupError_(
            f"ZIP {zip_norm} is not in the {year} BAH ZIP-to-MHA crosswalk. "
            f"Not every US ZIP maps to a military housing area."
        )
    return row["mha_code"]


def get_bah(
    conn: sqlite3.Connection,
    zip_code: str,
    pay_grade: str,
    has_dependents: bool,
    year: int | None = None,
    *,
    as_of: str | None = None,
) -> dict:
    """Monthly BAH for a ZIP/grade/dependency combination.

    Picks the most recent rate set covering that MHA, so an off-cycle increase
    (e.g. Abilene TX270 effective 2026-05-16) wins over the January publication
    for that MHA while every other MHA keeps the annual rate.
    """
    grade = normalize_pay_grade(pay_grade)
    year = year or latest_bah_year(conn)
    if year is None:
        raise LookupError_(
            "no BAH data loaded. Run: python -m mcp_militarypay.cli ingest --all"
        )

    mha = resolve_zip(conn, zip_code, year)
    dependents_flag = 1 if has_dependents else 0

    sql = (
        "SELECT r.monthly_rate, r.mha_name, s.id AS rate_set, s.effective_date, "
        "       s.label, s.source_url, s.is_annual_baseline "
        "FROM bah_rates r JOIN bah_rate_set s ON s.id = r.rate_set "
        "WHERE s.year = ? AND r.mha_code = ? AND r.pay_grade = ? "
        "      AND r.with_dependents = ? "
    )
    params: list = [year, mha, grade, dependents_flag]
    if as_of:
        sql += "AND s.effective_date <= ? "
        params.append(as_of)
    sql += "ORDER BY s.effective_date DESC LIMIT 1"

    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LookupError_(
            f"no {year} BAH rate for pay grade {grade} in MHA {mha} "
            f"({'with' if has_dependents else 'without'} dependents)"
        )

    result = {
        "zip_code": str(zip_code).strip().zfill(5),
        "mha_code": mha,
        "mha_name": row["mha_name"],
        "pay_grade": grade,
        "with_dependents": bool(has_dependents),
        "monthly_rate": row["monthly_rate"],
        "annual_rate": round(row["monthly_rate"] * 12, 2),
        "year": year,
        "rate_set": row["rate_set"],
        "effective_date": row["effective_date"],
        "rate_set_label": row["label"],
        "source_url": row["source_url"],
        "taxable": False,
        "notes": [],
        "disclaimer": DISCLAIMER,
    }

    if not row["is_annual_baseline"]:
        result["notes"].append(
            f"This is an off-cycle rate set ({row['label'] or row['rate_set']}) "
            f"effective {row['effective_date']}, not the January annual publication."
        )
    if grade in BAH_SENIOR_OFFICER_GRADES:
        result["notes"].append(
            "The DTMO rate lookup collapses O-7 and above into a single "
            "'O-7/O-7+' bucket; O-7 through O-10 share the same BAH rate."
        )
    if mha.startswith(COMMON_MHA_PREFIX):
        result["notes"].append(
            f"MHA {mha} is a nationwide 'common' housing area rather than a locality."
        )
    result["notes"].append(DUTY_STATION_RULE)
    result["notes"].append(
        "BAH rate protection: a member with uninterrupted eligibility at a "
        "location does not take a decrease when published rates drop, so an "
        "individual's actual rate may be a prior year's higher rate."
    )
    return result


# The rule that decides which ZIP to ask about at all. A member living outside
# their duty station's housing area still draws that station's rate, so using a
# home ZIP silently returns a different, real, wrong figure.
DUTY_STATION_RULE = (
    "BAH is based on the ZIP code of the member's permanent duty station, not "
    "where they live. Someone assigned to a station but residing in another "
    "housing area still draws the duty station's rate. Limited exceptions "
    "exist (for example BAH based on the dependents' location for certain "
    "unaccompanied or dependent-restricted tours); confirm an unusual case "
    "with the servicing finance office."
)


def find_housing_areas(
    conn: sqlite3.Connection,
    query: str,
    year: int | None = None,
    *,
    limit: int = 25,
) -> dict:
    """Find Military Housing Areas by name, MHA code or ZIP code.

    BAH is published per housing area, and get_bah takes a ZIP. Without this a
    caller asking about a place has to supply a ZIP from its own knowledge, and
    a wrong guess resolves to some other real area and returns a confident
    answer for the wrong locality. This lets the area be looked up instead.

    DTMO names areas for localities ("VALLEJO/TRAVIS AFB, CA", "HUNTSVILLE,
    AL"), so a name search matches the locality; an installation only matches
    where DTMO happened to name the area after it.
    """
    text = (query or "").strip()
    if not text:
        raise LookupError_("give a housing area name, MHA code or ZIP code to search for")

    year = year or latest_bah_year(conn)
    if year is None:
        raise LookupError_(
            "no BAH data loaded. Run: python -m mcp_militarypay.cli ingest --all"
        )

    areas = "SELECT DISTINCT r.mha_code, r.mha_name FROM bah_rates r " \
            "JOIN bah_rate_set s ON s.id = r.rate_set WHERE s.year = ?"
    matched: list[sqlite3.Row] = []
    matched_by = None

    if _MHA_CODE_RE.match(text.upper()):
        matched = list(conn.execute(
            areas + " AND r.mha_code = ?", (year, text.upper())))
        matched_by = "mha_code"

    if not matched and text.isdigit() and len(text) <= 5:
        zip_code = text.zfill(5)
        matched = list(conn.execute(
            areas + " AND r.mha_code = ("
            "  SELECT z.mha_code FROM zip_to_mha z JOIN bah_rate_set zs "
            "  ON zs.id = z.rate_set WHERE z.zip_code = ? AND zs.year = ? "
            "  AND zs.is_annual_baseline = 1 LIMIT 1)",
            (year, zip_code, year)))
        matched_by = "zip_code"

    if not matched:
        matched = list(conn.execute(
            areas + " AND r.mha_name LIKE ? ESCAPE '\\' "
            "ORDER BY LENGTH(r.mha_name), r.mha_name LIMIT ?",
            (year, f"%{_escape_like(text)}%", limit + 1)))
        matched_by = "name"

    truncated = len(matched) > limit
    matched = matched[:limit]

    results = []
    for row in matched:
        zips = list(conn.execute(
            "SELECT z.zip_code FROM zip_to_mha z JOIN bah_rate_set s "
            "ON s.id = z.rate_set WHERE s.year = ? AND s.is_annual_baseline = 1 "
            "AND z.mha_code = ? ORDER BY z.zip_code",
            (year, row["mha_code"])))
        codes = [z["zip_code"] for z in zips]
        results.append({
            "mha_code": row["mha_code"],
            "mha_name": row["mha_name"],
            "zip_code_count": len(codes),
            # Enough to pass to get_bah, without returning thousands.
            "example_zip_codes": codes[:5],
            "is_common_mha": row["mha_code"].startswith(COMMON_MHA_PREFIX),
        })

    return {
        "query": text,
        "matched_by": matched_by,
        "year": year,
        "count": len(results),
        "truncated": truncated,
        "housing_areas": results,
        "notes": [
            "Pass any of the example ZIP codes to get_bah to get rates for that "
            "area.",
            DUTY_STATION_RULE,
            "Areas are named for localities, so an installation matches only "
            "where the published name includes it.",
        ],
        "disclaimer": DISCLAIMER,
    }


# --- BAS ------------------------------------------------------------------

def get_bas(
    conn: sqlite3.Connection,
    pay_grade_type: str,
    year: int | None = None,
    *,
    bas_ii: bool = False,
) -> dict:
    """Monthly BAS for an officer or enlisted member.

    BAS II is returned only when explicitly requested: it is a conditional rate
    requiring Service Secretary authorization, not a default.
    """
    kind = (pay_grade_type or "").strip().lower()
    if kind in ("o", "officer", "commissioned", "warrant", "w", "warrant officer"):
        kind = "officer"
    elif kind in ("e", "enlisted"):
        kind = "enlisted"
    else:
        raise LookupError_(
            f"pay_grade_type must be 'officer' or 'enlisted', got {pay_grade_type!r}"
        )

    if year is None:
        row = conn.execute(
            "SELECT * FROM bas_rates ORDER BY effective_date DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM bas_rates WHERE effective_date <= ? "
            "ORDER BY effective_date DESC LIMIT 1",
            (f"{year}-12-31",),
        ).fetchone()

    if row is None:
        raise LookupError_(
            "no BAS data loaded for that period. Run: "
            "python -m mcp_militarypay.cli ingest --all"
        )

    rate = row["bas_ii_rate"] if bas_ii else (
        row["officer_rate"] if kind == "officer" else row["enlisted_rate"]
    )

    notes = []
    if bas_ii:
        notes.append(
            "BAS II is a conditional rate (twice the standard enlisted BAS) for "
            "members in unaccompanied government quarters without food "
            "preparation facilities and with no government mess available. It "
            "requires Service Secretary authorization and is not a default rate."
        )
    if kind == "officer":
        notes.append("Warrant officers receive the officer BAS rate.")

    return {
        "pay_grade_type": kind,
        "bas_ii": bas_ii,
        "monthly_rate": rate,
        "annual_rate": round(rate * 12, 2),
        "effective_date": row["effective_date"],
        "year": year or int(row["effective_date"][:4]),
        "source_url": row["source_url"],
        "taxable": False,
        "notes": notes,
        "disclaimer": DISCLAIMER,
    }


# --- combined -------------------------------------------------------------

def estimate_total_compensation(
    conn: sqlite3.Connection,
    pay_grade: str,
    years_of_service: float,
    zip_code: str,
    has_dependents: bool,
    year: int | None = None,
    *,
    months_active_duty: int | None = None,
    senior_enlisted_advisor: bool = False,
) -> dict:
    """Base pay + BAH + BAS with a taxable/non-taxable split.

    The split is most of the point of asking: basic pay is taxable income, while
    BAH and BAS are tax-free allowances.
    """
    grade = normalize_pay_grade(pay_grade)
    category = category_for_grade(grade)
    bas_kind = "enlisted" if category == "enlisted" else "officer"

    components: dict[str, dict] = {}
    errors: dict[str, str] = {}

    try:
        components["base_pay"] = get_base_pay(
            conn, grade, years_of_service, year,
            months_active_duty=months_active_duty,
            senior_enlisted_advisor=senior_enlisted_advisor,
        )
    except LookupError_ as exc:
        errors["base_pay"] = str(exc)

    try:
        components["bah"] = get_bah(conn, zip_code, grade, has_dependents, year)
    except LookupError_ as exc:
        errors["bah"] = str(exc)

    try:
        components["bas"] = get_bas(conn, bas_kind, year)
    except LookupError_ as exc:
        errors["bas"] = str(exc)

    base_rate = (components.get("base_pay") or {}).get("monthly_rate")
    bah_rate = (components.get("bah") or {}).get("monthly_rate")
    bas_rate = (components.get("bas") or {}).get("monthly_rate")

    # A grade/YOS combination that does not exist resolves without raising but
    # carries a null rate. Left alone it would drop out of the totals silently,
    # so gross_total would read as BAH + BAS and look like a real answer.
    base_component = components.get("base_pay") or {}
    if base_rate is None and base_component.get("rate_basis") == "not_a_valid_combination":
        errors["base_pay"] = base_component.get("explanation") or (
            f"{grade} at {years_of_service} years of service is not a published "
            f"combination"
        )

    taxable = base_rate or 0.0
    non_taxable = (bah_rate or 0.0) + (bas_rate or 0.0)
    complete = not errors and base_rate is not None

    result = {
        "pay_grade": grade,
        "years_of_service": years_of_service,
        "zip_code": str(zip_code).strip().zfill(5),
        "has_dependents": bool(has_dependents),
        "year": year or latest_base_pay_year(conn),
        "components": components,
        "monthly": {
            "base_pay_taxable": base_rate,
            "bah_non_taxable": bah_rate,
            "bas_non_taxable": bas_rate,
            "taxable_total": round(taxable, 2),
            "non_taxable_total": round(non_taxable, 2),
            "gross_total": round(taxable + non_taxable, 2),
        },
        "annual": {
            "taxable_total": round(taxable * 12, 2),
            "non_taxable_total": round(non_taxable * 12, 2),
            "gross_total": round((taxable + non_taxable) * 12, 2),
        },
        "complete": complete,
        "bah_note": DUTY_STATION_RULE,
        "tax_note": (
            "Basic pay is taxable income. BAH and BAS are non-taxable "
            "allowances. This is regular military compensation only: it excludes "
            "special and incentive pays, bonuses, and any deductions "
            "(federal/state tax withholding, FICA, SGLI, TSP, TRICARE dental)."
        ),
        "disclaimer": DISCLAIMER,
    }
    if errors:
        result["errors"] = errors
        result["warning"] = (
            "Some components could not be resolved; the totals below cover only "
            f"the components that were: {sorted(components)}"
        )
    return result


def database_status(conn: sqlite3.Connection) -> dict:
    """What data is loaded, and when it was last fetched."""
    def scalar(sql: str, params: tuple = ()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    rate_sets = [dict(r) for r in conn.execute(
        "SELECT id, year, effective_date, label, is_annual_baseline "
        "FROM bah_rate_set ORDER BY year DESC, effective_date DESC")]
    recent = [dict(r) for r in conn.execute(
        "SELECT source, url, fetched_at, row_count, ok, notes "
        "FROM source_fetch_log ORDER BY id DESC LIMIT 12")]

    return {
        "base_pay_years": available_base_pay_years(conn),
        "base_pay_rows": scalar("SELECT COUNT(*) FROM base_pay"),
        "bas_rows": scalar("SELECT COUNT(*) FROM bas_rates"),
        "bas_latest_effective_date": scalar(
            "SELECT MAX(effective_date) FROM bas_rates"),
        "bah_rate_sets": rate_sets,
        "bah_rate_rows": scalar("SELECT COUNT(*) FROM bah_rates"),
        "zip_crosswalk_rows": scalar("SELECT COUNT(*) FROM zip_to_mha"),
        "recent_fetches": recent,
        "disclaimer": DISCLAIMER,
    }
