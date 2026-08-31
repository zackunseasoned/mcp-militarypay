"""FastMCP server exposing the military pay rate tables as read-only tools.

Every tool is a parameterized SELECT against the local SQLite database built by
the ingest. Nothing here touches the network, which is why openWorldHint is
False: refreshing the data is a separate, deliberate step.
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import queries
from .db import connect, default_db_path
from .sources import DISCLAIMER

INSTRUCTIONS = f"""
Look up published United States military pay rates: basic pay and Basic
Allowance for Subsistence (BAS) from DFAS, and Basic Allowance for Housing (BAH)
from the DoD Defense Travel Management Office.

Use these tools for questions like "what does an E-5 with 6 years make", "what
is BAH for a sergeant with dependents in San Diego", or "estimate total monthly
compensation for an O-3 in 78234".

Answers come from a local snapshot of the published rate tables. Every response
carries the effective date and source URL of the figures used - quote them.
Rates change annually (effective 1 January), and BAH additionally changes
off-cycle for individual housing areas.

{DISCLAIMER}
""".strip()

mcp = FastMCP(name="militarypay", instructions=INSTRUCTIONS, version="0.1.0")

_READ_ONLY = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

# FastMCP dispatches tool calls on worker threads, and a SQLite connection may
# only be used from the thread that created it. Each thread therefore gets its
# own read-only connection rather than sharing one.
_local = threading.local()
_db_path_override: Path | None = None

# Every connection handed out, so they can be closed rather than left to the
# garbage collector. An unclosed sqlite3 connection raises an unraisable
# exception during finalization on Python 3.13+, and a worker thread that exits
# would otherwise leak its connection for the life of the process.
_connections: list[sqlite3.Connection] = []
_connections_lock = threading.Lock()

# Bumped on every configure(). A worker thread compares its cached connection
# against this: clearing only the calling thread's connection left every other
# thread serving the previous database indefinitely.
_config_generation = 0


def configure(db_path: str | os.PathLike[str] | None) -> None:
    """Point the server at a specific database file.

    Takes effect on every thread, not just the caller's: each worker reopens
    when it next sees a newer configuration. Without a call to this, the path
    comes from $MILITARYPAY_DB or the packaged default.
    """
    global _db_path_override, _config_generation
    _db_path_override = Path(db_path) if db_path is not None else None
    _config_generation += 1
    _close_all_connections()


def _close_all_connections() -> None:
    """Close every connection handed out, from whichever thread calls this.

    Connections are opened with check_same_thread=False purely so this is
    legal; each thread still gets its own, so none is ever shared in use.
    """
    with _connections_lock:
        for connection in _connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        _connections.clear()
    _local.connection = None
    _local.generation = None


atexit.register(_close_all_connections)


def current_db_path() -> Path:
    """The database this server is actually reading."""
    return _db_path_override if _db_path_override is not None else default_db_path()


def get_connection() -> sqlite3.Connection:
    """The calling thread's read-only connection, reopened after a configure()."""
    connection = getattr(_local, "connection", None)
    if connection is not None and getattr(_local, "generation", None) != _config_generation:
        _close_all_connections()
        connection = None
    if connection is None:
        connection = connect(_db_path_override, read_only=True, same_thread=False)
        with _connections_lock:
            _connections.append(connection)
        _local.connection = connection
        _local.generation = _config_generation
    return connection


def _run(function, *args, **kwargs) -> dict[str, Any]:
    """Call a query function, turning expected failures into structured results.

    An LLM caller gets an actionable message rather than a traceback, and a
    missing database says exactly which command builds it.
    """
    try:
        return function(get_connection(), *args, **kwargs)
    except FileNotFoundError as exc:
        return {"error": "database_not_found", "message": str(exc)}
    except (queries.LookupError_, ValueError) as exc:
        return {"error": "lookup_failed", "message": str(exc), "disclaimer": DISCLAIMER}
    except sqlite3.Error as exc:
        return {"error": "database_error", "message": str(exc)}


@mcp.tool(annotations=_READ_ONLY)
def get_base_pay(
    pay_grade: Annotated[str, Field(description="Pay grade, e.g. 'E-5', 'O-3', 'W-2', 'O-1E'.")],
    years_of_service: Annotated[float, Field(ge=0, description="Cumulative years of service.")],
    year: Annotated[int | None, Field(description="Pay table year. Defaults to the most recent loaded.")] = None,
    months_active_duty: Annotated[int | None, Field(ge=0, description="Total months of active duty. Only affects E-1: under 4 months is a different, lower rate.")] = None,
    senior_enlisted_advisor: Annotated[bool, Field(description="True for a senior enlisted advisor billet (SEAC, SMA, MCPON, CMSAF, SMMC, CMSSF, MCPOCG, SEA to CNGB), which is a flat rate regardless of service.")] = False,
) -> dict[str, Any]:
    """Monthly basic pay for a pay grade at a given years-of-service band.

    Basic pay is taxable income. Returns the rate plus any footnotes that apply
    to that grade. A grade/years-of-service combination that does not exist on
    the table (for example E-8 at 2 years) is reported as an invalid combination
    with a null rate - not as zero.
    """
    return _run(
        queries.get_base_pay, pay_grade, years_of_service, year,
        months_active_duty=months_active_duty,
        senior_enlisted_advisor=senior_enlisted_advisor,
    )


@mcp.tool(annotations=_READ_ONLY)
def get_bah(
    zip_code: Annotated[str, Field(description="5-digit US ZIP code of the duty station.")],
    pay_grade: Annotated[str, Field(description="Pay grade, e.g. 'E-5', 'O-3', 'O-2E'.")],
    has_dependents: Annotated[bool, Field(description="Whether the member has dependents.")],
    year: Annotated[int | None, Field(description="BAH year. Defaults to the most recent loaded.")] = None,
    as_of: Annotated[str | None, Field(description="ISO date (YYYY-MM-DD). Returns the rate set in effect on that date, ignoring later off-cycle adjustments.")] = None,
) -> dict[str, Any]:
    """Monthly Basic Allowance for Housing for a ZIP code and pay grade.

    BAH is a non-taxable allowance. Resolves the ZIP to its Military Housing
    Area and returns the rate together with the MHA code, the rate set used and
    its effective date. Where a housing area has an off-cycle adjustment, the
    most recent applicable rate set is used.
    """
    return _run(queries.get_bah, zip_code, pay_grade, has_dependents, year, as_of=as_of)


@mcp.tool(annotations=_READ_ONLY)
def get_bas(
    pay_grade_type: Annotated[Literal["officer", "enlisted"], Field(description="'officer' (including warrant officers) or 'enlisted'.")],
    year: Annotated[int | None, Field(description="Year. Defaults to the most recent loaded.")] = None,
    bas_ii: Annotated[bool, Field(description="Return the conditional BAS II rate instead. Only when explicitly asked for.")] = False,
) -> dict[str, Any]:
    """Monthly Basic Allowance for Subsistence for an officer or enlisted member.

    BAS is a non-taxable allowance. Warrant officers receive the officer rate.
    BAS II is a conditional rate (twice standard enlisted BAS) requiring Service
    Secretary authorization - it is never the default answer.
    """
    return _run(queries.get_bas, pay_grade_type, year, bas_ii=bas_ii)


@mcp.tool(annotations=_READ_ONLY)
def estimate_total_compensation(
    pay_grade: Annotated[str, Field(description="Pay grade, e.g. 'E-5', 'O-3'.")],
    years_of_service: Annotated[float, Field(ge=0, description="Cumulative years of service.")],
    zip_code: Annotated[str, Field(description="5-digit US ZIP code of the duty station.")],
    has_dependents: Annotated[bool, Field(description="Whether the member has dependents.")],
    year: Annotated[int | None, Field(description="Year. Defaults to the most recent loaded.")] = None,
    months_active_duty: Annotated[int | None, Field(ge=0, description="Total months of active duty. Only affects E-1.")] = None,
    senior_enlisted_advisor: Annotated[bool, Field(description="True for a senior enlisted advisor billet.")] = False,
) -> dict[str, Any]:
    """Basic pay + BAH + BAS with a per-component breakdown and a tax split.

    Returns monthly and annual totals separated into taxable (basic pay) and
    non-taxable (BAH, BAS) amounts. This is regular military compensation only:
    it excludes special and incentive pays, bonuses, and all deductions.
    """
    return _run(
        queries.estimate_total_compensation, pay_grade, years_of_service,
        zip_code, has_dependents, year,
        months_active_duty=months_active_duty,
        senior_enlisted_advisor=senior_enlisted_advisor,
    )


@mcp.tool(annotations=_READ_ONLY)
def get_database_status() -> dict[str, Any]:
    """What rate data is loaded and when each source was last fetched.

    Use this to check how current the figures are before relying on them, or to
    find out why a lookup came back empty.
    """
    result = _run(queries.database_status)
    result.setdefault("database_path", str(current_db_path()))
    return result


def main() -> None:
    """Entry point for the `militarypay-mcp` console script."""
    transport = os.environ.get("MILITARYPAY_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
