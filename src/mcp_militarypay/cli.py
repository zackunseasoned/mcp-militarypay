"""Command line interface for building and inspecting the rate database.

    python -m mcp_militarypay.cli ingest --all --year 2026
    python -m mcp_militarypay.cli ingest --bah-offcycle 2026-abilene-temp \
        --effective-date 2026-05-16 --mha TX270 --url <bundle-url>
    python -m mcp_militarypay.cli status
    python -m mcp_militarypay.cli verify
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import ingest as ingest_module
from . import queries, sources
from .db import default_db_path, open_for_ingest, connect


def _current_year() -> int:
    return date.today().year


def _read(path: str | None) -> str | None:
    return Path(path).read_text(encoding="utf-8", errors="replace") if path else None


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = open_for_ingest(args.db)
    year = args.year or _current_year()
    results = []

    want_all = args.all or not any(
        [args.base_pay, args.bas, args.bah, args.bah_offcycle]
    )

    if want_all or args.base_pay:
        categories = (
            [args.base_pay] if isinstance(args.base_pay, str) and args.base_pay != "all"
            else list(sources.BASE_PAY_CATEGORIES)
        )
        for category in categories:
            results.append(ingest_module.ingest_base_pay(
                conn, category, html=_read(args.from_file) if len(categories) == 1 else None,
                year=args.year, refresh=args.refresh,
            ))

    if want_all or args.bas:
        results.append(ingest_module.ingest_bas(
            conn, html=_read(args.from_file) if args.bas else None, refresh=args.refresh))

    if want_all or args.bah:
        zip_bytes = Path(args.from_file).read_bytes() if (args.bah and args.from_file) else None
        results.append(ingest_module.ingest_bah(
            conn, year, zip_bytes=zip_bytes, url=args.url, refresh=args.refresh))

    if args.bah_offcycle:
        if not args.effective_date:
            print("--bah-offcycle requires --effective-date", file=sys.stderr)
            return 2
        zip_bytes = Path(args.from_file).read_bytes() if args.from_file else None
        results.append(ingest_module.ingest_bah(
            conn, year, zip_bytes=zip_bytes, url=args.url,
            rate_set_id=args.bah_offcycle, effective_date=args.effective_date,
            label=args.label, is_annual_baseline=False,
            restrict_to_mha=args.mha, refresh=args.refresh,
        ))

    print()
    for result in results:
        print(result.describe())

    failures = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} sources ingested.")
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        conn = connect(args.db, read_only=True)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(queries.database_status(conn), indent=2))
    return 0


# Values published by DFAS for 2026 and quoted in the build spec. The point of
# checking them is to catch a parser that silently read the wrong column.
KNOWN_VALUES = [
    ("2026 basic pay E-5 over 4", 3946.80,
     lambda conn: queries.get_base_pay(conn, "E-5", 4, 2026).get("monthly_rate")),
    ("2026 BAS enlisted", 476.95,
     lambda conn: queries.get_bas(conn, "enlisted", 2026).get("monthly_rate")),
    ("2026 BAS officer", 328.48,
     lambda conn: queries.get_bas(conn, "officer", 2026).get("monthly_rate")),
    ("2026 BAS II", 953.90,
     lambda conn: queries.get_bas(conn, "enlisted", 2026, bas_ii=True).get("monthly_rate")),
]


def cmd_verify(args: argparse.Namespace) -> int:
    """Check the loaded data against independently known published figures."""
    try:
        conn = connect(args.db, read_only=True)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    failures = 0
    for label, expected, getter in KNOWN_VALUES:
        try:
            actual = getter(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {label}: {exc}")
            failures += 1
            continue
        if actual is None:
            print(f"[FAIL] {label}: no value loaded (expected {expected})")
            failures += 1
        elif abs(actual - expected) < 0.005:
            print(f"[ ok ] {label} = {actual}")
        else:
            print(f"[FAIL] {label}: expected {expected}, got {actual}")
            failures += 1

    print(f"\n{len(KNOWN_VALUES) - failures}/{len(KNOWN_VALUES)} known values matched.")
    if failures:
        print(
            "A mismatch means either the published rates changed or a parser is "
            "reading the wrong column. Compare raw_bah_lines and the DFAS page "
            "before trusting the data.", file=sys.stderr,
        )
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_militarypay.cli",
        description="Build and inspect the military pay rate database.",
    )
    parser.add_argument("--db", default=None,
                        help=f"Database path (default: {default_db_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Fetch and load rate tables.")
    p_ingest.add_argument("--all", action="store_true", help="Ingest every source (default).")
    p_ingest.add_argument("--base-pay", nargs="?", const="all",
                          choices=["all", *sources.BASE_PAY_CATEGORIES],
                          help="Ingest DFAS basic pay (optionally one category).")
    p_ingest.add_argument("--bas", action="store_true", help="Ingest the BAS table.")
    p_ingest.add_argument("--bah", action="store_true", help="Ingest the annual BAH bundle.")
    p_ingest.add_argument("--bah-offcycle", metavar="RATE_SET_ID",
                          help="Ingest an off-cycle BAH set, e.g. 2026-abilene-temp.")
    p_ingest.add_argument("--effective-date", help="ISO date for an off-cycle rate set.")
    p_ingest.add_argument("--label", help="Human-readable label for an off-cycle rate set.")
    p_ingest.add_argument("--mha", nargs="+", help="Restrict an off-cycle set to these MHA codes.")
    p_ingest.add_argument("--year", type=int, help="Rate year (default: current year).")
    p_ingest.add_argument("--url", help="Override the source URL.")
    p_ingest.add_argument("--from-file", help="Load from a local file instead of the network.")
    p_ingest.add_argument("--refresh", action="store_true", help="Bypass the HTTP cache.")
    p_ingest.set_defaults(func=cmd_ingest)

    p_status = sub.add_parser("status", help="Show what data is loaded.")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="Check loaded data against known published values.")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
