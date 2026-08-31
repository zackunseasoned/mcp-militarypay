"""Fetch -> parse -> write. Run at build/refresh time, never in the request path.

Every ingest records a row in source_fetch_log with the row count it wrote, so a
page that quietly changes shape shows up as a count that dropped rather than as
silently wrong answers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import sources
from .db import log_fetch, utcnow
from .fetch import fetch_bytes, fetch_text
from .parsers import bah as bah_parser
from .parsers import bas as bas_parser
from .parsers import basepay as basepay_parser


@dataclass
class IngestResult:
    source: str
    ok: bool
    rows: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def describe(self) -> str:
        status = "ok" if self.ok else "FAILED"
        line = f"[{status}] {self.source}: {self.rows} rows"
        if self.error:
            line += f"\n    error: {self.error}"
        for warning in self.warnings:
            line += f"\n    warning: {warning}"
        return line


# --- basic pay ------------------------------------------------------------

def ingest_base_pay(
    conn: sqlite3.Connection,
    category: str,
    *,
    html: str | None = None,
    year: int | None = None,
    refresh: bool = False,
) -> IngestResult:
    """Ingest one DFAS basic pay category page.

    Pass html= to ingest from a fixture or a saved copy instead of the network.
    """
    url = sources.BASE_PAY_SOURCES[category]
    fetched_at = utcnow()

    try:
        if html is None:
            html = fetch_text(url, refresh=refresh)
        table = basepay_parser.parse_base_pay(html, category, source_url=url)
    except Exception as exc:  # noqa: BLE001 - recorded and reported, not swallowed
        log_fetch(conn, source=f"base_pay:{category}", url=url, ok=False,
                  notes=str(exc), fetched_at=fetched_at)
        conn.commit()
        return IngestResult(f"base_pay:{category}", ok=False, error=str(exc))

    effective_year = year or table.year
    if effective_year is None:
        message = (
            "no effective year on the page and none supplied; refusing to guess "
            "which year these rates belong to"
        )
        log_fetch(conn, source=f"base_pay:{category}", url=url, ok=False,
                  notes=message, fetched_at=fetched_at)
        conn.commit()
        return IngestResult(f"base_pay:{category}", ok=False, error=message)

    # Replace this (year, category) wholesale; prior years are left untouched.
    conn.execute("DELETE FROM base_pay WHERE year = ? AND category = ?",
                 (effective_year, category))
    conn.execute("DELETE FROM base_pay_note WHERE year = ? AND category = ?",
                 (effective_year, category))
    conn.execute("DELETE FROM base_pay_special WHERE year = ? AND category = ?",
                 (effective_year, category))

    conn.executemany(
        "INSERT INTO base_pay(year, category, pay_grade, yos_min, monthly_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        [(effective_year, category, grade, yos, rate)
         for (grade, yos), rate in sorted(table.rates.items())],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO base_pay_note(year, category, pay_grade, note) "
        "VALUES (?, ?, ?, ?)",
        [(effective_year, category, None, note) for note in table.notes],
    )
    conn.executemany(
        "INSERT INTO base_pay_special(year, key, category, pay_grade, label, "
        "monthly_rate, note) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(year, key) DO UPDATE SET "
        "  category=excluded.category, pay_grade=excluded.pay_grade, "
        "  label=excluded.label, monthly_rate=excluded.monthly_rate, note=excluded.note",
        [(effective_year, key, s["category"], s["pay_grade"], s["label"],
          s["monthly_rate"], s["note"]) for key, s in table.specials.items()],
    )

    warnings = list(table.warnings)
    for key, special in table.specials.items():
        if (special["monthly_rate"] is None
                and key not in sources.INFORMATIONAL_SPECIAL_KEYS):
            warnings.append(
                f"footnote for {key} found but no dollar amount could be read "
                f"from it; the note text is stored, the rate is NULL"
            )

    log_fetch(conn, source=f"base_pay:{category}", url=url, ok=True,
              row_count=len(table.rates),
              notes="; ".join(warnings) or None, fetched_at=fetched_at)
    conn.commit()
    return IngestResult(f"base_pay:{category}", ok=True,
                        rows=len(table.rates), warnings=warnings)


# --- BAS ------------------------------------------------------------------

def ingest_bas(
    conn: sqlite3.Connection, *, html: str | None = None, refresh: bool = False
) -> IngestResult:
    """Ingest the whole BAS history from the DFAS page."""
    url = sources.BAS_SOURCE
    fetched_at = utcnow()

    try:
        if html is None:
            html = fetch_text(url, refresh=refresh)
        table = bas_parser.parse_bas(html, source_url=url)
    except Exception as exc:  # noqa: BLE001
        log_fetch(conn, source="bas", url=url, ok=False, notes=str(exc),
                  fetched_at=fetched_at)
        conn.commit()
        return IngestResult("bas", ok=False, error=str(exc))

    conn.executemany(
        "INSERT INTO bas_rates(effective_date, officer_rate, enlisted_rate, "
        "bas_ii_rate, source_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(effective_date) DO UPDATE SET "
        "  officer_rate=excluded.officer_rate, enlisted_rate=excluded.enlisted_rate, "
        "  bas_ii_rate=excluded.bas_ii_rate, source_url=excluded.source_url, "
        "  fetched_at=excluded.fetched_at",
        [(date, officer, enlisted, bas_ii, url, fetched_at)
         for date, (officer, enlisted, bas_ii) in sorted(table.rows.items())],
    )

    log_fetch(conn, source="bas", url=url, ok=True, row_count=len(table.rows),
              notes="; ".join(table.warnings) or None, fetched_at=fetched_at)
    conn.commit()
    return IngestResult("bas", ok=True, rows=len(table.rows),
                        warnings=list(table.warnings))


# --- BAH ------------------------------------------------------------------

def ingest_bah(
    conn: sqlite3.Connection,
    year: int,
    *,
    zip_bytes: bytes | None = None,
    url: str | None = None,
    rate_set_id: str | None = None,
    effective_date: str | None = None,
    label: str | None = None,
    is_annual_baseline: bool = True,
    restrict_to_mha: list[str] | None = None,
    keep_raw: bool = True,
    refresh: bool = False,
) -> IngestResult:
    """Ingest one BAH rate set from an ASCII bundle.

    A rate set is a year OR an off-cycle adjustment. For an off-cycle set (e.g.
    the 2026 Abilene/Dyess AFB temporary increase effective 2026-05-16, MHA
    TX270) pass a distinct rate_set_id, its real effective_date,
    is_annual_baseline=False and restrict_to_mha=['TX270']. Prior sets are never
    overwritten: BAH individual rate protection means historical rates have to
    stay queryable.
    """
    url = url or sources.bah_ascii_url(year)
    rate_set_id = rate_set_id or str(year)
    effective_date = effective_date or f"{year}-01-01"
    fetched_at = utcnow()
    source_name = f"bah:{rate_set_id}"

    try:
        if zip_bytes is None:
            zip_bytes = fetch_bytes(url, refresh=refresh, suffix=".zip")
        bundle = bah_parser.parse_bah_bundle(zip_bytes, year, keep_raw=keep_raw)
    except Exception as exc:  # noqa: BLE001
        log_fetch(conn, source=source_name, url=url, ok=False, notes=str(exc),
                  fetched_at=fetched_at)
        conn.commit()
        return IngestResult(source_name, ok=False, error=str(exc))

    keep = {m.upper() for m in restrict_to_mha} if restrict_to_mha else None
    if keep:
        available = bundle.with_dependents.mha_codes | bundle.without_dependents.mha_codes
        missing = keep - available
        if missing:
            message = f"restrict_to_mha names MHAs absent from the bundle: {sorted(missing)}"
            log_fetch(conn, source=source_name, url=url, ok=False, notes=message,
                      fetched_at=fetched_at)
            conn.commit()
            return IngestResult(source_name, ok=False, error=message)

    # Replace this rate set only.
    conn.execute("DELETE FROM bah_rates WHERE rate_set = ?", (rate_set_id,))
    conn.execute("DELETE FROM zip_to_mha WHERE rate_set = ?", (rate_set_id,))
    conn.execute("DELETE FROM raw_bah_lines WHERE rate_set = ?", (rate_set_id,))
    conn.execute(
        "INSERT INTO bah_rate_set(id, year, effective_date, label, "
        "is_annual_baseline, source_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  year=excluded.year, effective_date=excluded.effective_date, "
        "  label=excluded.label, is_annual_baseline=excluded.is_annual_baseline, "
        "  source_url=excluded.source_url, fetched_at=excluded.fetched_at",
        (rate_set_id, year, effective_date, label,
         1 if is_annual_baseline else 0, url, fetched_at),
    )

    rows = 0
    for rate_file in (bundle.with_dependents, bundle.without_dependents):
        payload = [
            (rate_set_id, mha, bundle.mha_names.get(mha), grade,
             1 if rate_file.with_dependents else 0, rate)
            for (mha, grade), rate in rate_file.rates.items()
            if keep is None or mha in keep
        ]
        conn.executemany(
            "INSERT INTO bah_rates(rate_set, mha_code, mha_name, pay_grade, "
            "with_dependents, monthly_rate) VALUES (?, ?, ?, ?, ?, ?)",
            payload,
        )
        rows += len(payload)

        if keep_raw:
            conn.executemany(
                "INSERT OR REPLACE INTO raw_bah_lines(source_file, rate_set, "
                "fetched_at, line_no, raw_text) VALUES (?, ?, ?, ?, ?)",
                [(rate_file.source_file, rate_set_id, fetched_at, line_no, text)
                 for line_no, text in rate_file.raw_lines],
            )

    # An off-cycle set ships no crosswalk of its own; ZIP resolution falls back
    # to the annual baseline for that year (see queries.resolve_zip).
    if is_annual_baseline:
        conn.executemany(
            "INSERT OR REPLACE INTO zip_to_mha(rate_set, zip_code, mha_code) "
            "VALUES (?, ?, ?)",
            [(rate_set_id, zip_code, mha) for zip_code, mha in bundle.zip_to_mha.items()],
        )

    warnings = list(bundle.warnings)
    warnings.extend(bundle.with_dependents.warnings[:5])
    warnings.extend(bundle.without_dependents.warnings[:5])

    log_fetch(conn, source=source_name, url=url, ok=True, row_count=rows,
              notes="; ".join(warnings) or None, fetched_at=fetched_at)
    conn.commit()
    return IngestResult(source_name, ok=True, rows=rows, warnings=warnings)


def ingest_all(conn: sqlite3.Connection, year: int, *, refresh: bool = False) -> list[IngestResult]:
    """Refresh every source for a year. Continues past a failed source so one
    broken page does not block the rest."""
    results = [
        ingest_base_pay(conn, category, year=year, refresh=refresh)
        for category in sources.BASE_PAY_CATEGORIES
    ]
    results.append(ingest_bas(conn, refresh=refresh))
    results.append(ingest_bah(conn, year, refresh=refresh))
    return results
