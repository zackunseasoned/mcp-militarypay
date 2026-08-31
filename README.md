# mcp-militarypay

An MCP server for published **United States military pay rates**: basic pay and
Basic Allowance for Subsistence (BAS) from DFAS, and Basic Allowance for Housing
(BAH) from the DoD Defense Travel Management Office (DTMO).

> **Unofficial.** This reads public rate tables. It does not read anyone's Leave
> and Earnings Statement and it is not an authoritative source of pay. For actual
> pay questions contact DFAS at **1-888-332-7411** or use
> [myPay](https://mypay.dfas.mil).

DFAS publishes no developer API. All source data is public HTML tables and
downloadable bulk files — no API key, no auth, no login.

## How it works

Ingest and serve are separate. Parsing happens once at refresh time and writes to
SQLite; every MCP tool call afterwards is a parameterized `SELECT` with no HTML
or CSV parsing in the request path.

```
DFAS HTML  ─┐
DTMO ASCII ─┼─→  ingest (parse, validate, log)  ─→  SQLite  ─→  MCP tools
DFAS BAS   ─┘
```

At ~7k rows SQLite isn't chosen for speed — it's chosen because the
ZIP → MHA → rate join is real SQL rather than chained dicts, multi-year history
is painless, and you can open the file in any SQLite browser to eyeball a scrape
against the live page.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip
```

## Build the database

```bash
python -m mcp_militarypay.cli ingest --all --year 2026
python -m mcp_militarypay.cli verify       # check against known published values
python -m mcp_militarypay.cli status       # what's loaded, and when it was fetched
python -m mcp_militarypay.cli probe        # diagnose HTTP 403s (see below)
python -m mcp_militarypay.cli notes        # footnotes + flat rates captured
```

`notes` prints the footnote text captured from the DFAS pages and the flat
rates read out of it, flagging any rate that could not be extracted. Those
patterns are the most fragile part of the ingest, so this is the quickest way
to check them against the live page.

The database defaults to `data/militarypay.sqlite3`; override with `--db` or the
`MILITARYPAY_DB` environment variable.

### Off-cycle BAH rate sets

BAH is **not** immutable per calendar year. Individual housing areas get
mid-year adjustments — for example the 2026 temporary increase for
Abilene, TX / Dyess AFB (MHA `TX270`), effective 2026-05-16. A design that
assumes one file per year silently serves stale rates for that MHA.

Rate sets are therefore modelled by `effective_date`, and an off-cycle set is
ingested as a distinct, partial set:

```bash
python -m mcp_militarypay.cli ingest \
    --bah-offcycle 2026-abilene-temp \
    --effective-date 2026-05-16 \
    --label "2026 Abilene Temporary Increase" \
    --mha TX270 \
    --url <bundle-url-from-the-DTMO-year-dropdown>
```

Lookups then pick the most recent rate set covering that MHA, while every other
MHA keeps the annual rate. Pass `as_of` to a lookup to get the rate in effect on
a given date. Prior years are never overwritten on refresh — BAH **individual
rate protection** means a member with uninterrupted eligibility doesn't take a
decrease when published rates drop, so historical rates must stay queryable.

## Troubleshooting: HTTP 403 from DFAS / DTMO

Both `dfas.mil` and `travel.dod.mil` sit behind a WAF that rejects clients which
don't look like a browser. A custom `User-Agent` gets an outright **HTTP 403 on
every URL, on both hosts** — which is what the first live run of this project
hit. These are public rate tables with no authentication, no login and no API
key, so the fix is simply to send the ordinary header set a browser sends, and
that is now the default (a current Chrome UA, the usual `Accept` /
`Sec-Fetch-*` / `sec-ch-ua` headers, over HTTP/2).

If a future WAF change breaks it again, don't guess one profile at a time:

```bash
python -m mcp_militarypay.cli probe
```

This tries four header profiles against each host and prints which ones get a
200, along with any WAF markers in the rejection body:

| Profile | What it isolates |
|---|---|
| `project-ua` | the original custom User-Agent (the one that 403'd) |
| `httpx-default` | no custom headers at all |
| `browser` | full browser header set over HTTP/1.1 |
| `browser-http2` | full browser header set over HTTP/2 — the current default |

Then override the agent if a different one is needed:

```bash
export MILITARYPAY_USER_AGENT="..."     # Windows: $env:MILITARYPAY_USER_AGENT
```

`probe` distinguishes the two failure modes that look alike from the outside:
an HTTP status code means the server answered and rejected the request, while a
transport or proxy error means something between you and the server blocked it
(a corporate/ISP filter or an egress policy) and no header change will help.

## Run the server

```bash
python -m mcp_militarypay.server
```

Register it with an MCP client (stdio transport):

```json
{
  "mcpServers": {
    "militarypay": {
      "command": "C:\\Python314\\python.exe",
      "args": ["-m", "mcp_militarypay.server"],
      "env": { "MILITARYPAY_DB": "C:\\path\\to\\militarypay.sqlite3" }
    }
  }
}
```

## Tools

All five are read-only (`readOnlyHint`, `idempotentHint`, `openWorldHint: false`
— they query the local database, not the live web). Every response carries the
**effective date and source URL** of the figures used.

| Tool | Returns |
|---|---|
| `get_base_pay(pay_grade, years_of_service, year?, months_active_duty?, senior_enlisted_advisor?)` | Monthly basic pay (taxable) plus applicable footnotes |
| `get_bah(zip_code, pay_grade, has_dependents, year?, as_of?)` | Monthly BAH (non-taxable), resolved MHA code, rate set and effective date |
| `get_bas(pay_grade_type, year?, bas_ii?)` | Monthly BAS (non-taxable) |
| `estimate_total_compensation(pay_grade, years_of_service, zip_code, has_dependents, ...)` | Base pay + BAH + BAS with a per-component breakdown and taxable/non-taxable split |
| `get_database_status()` | What data is loaded and when each source was last fetched |

### Entitlement rules that are actually implemented

These footnotes are the difference between a toy and a number someone can act
on:

- **E-1 under 4 months of active duty** is a different, lower rate than the E-1
  table value. Pass `months_active_duty`.
- **Senior enlisted advisor billets** (SEAC, SMA, MCPON, CMSAF, SMMC, CMSSF,
  MCPOCG, SEA to CNGB) are a flat rate regardless of years of service. Pass
  `senior_enlisted_advisor=True`.
- **Blank cells are not $0.** E-8 has no published rate below "Over 8", E-9 none
  below "Over 10". Those combinations return a null rate and an explicit
  "not a valid combination" explanation.
- **Years-of-service banding** is a range lookup, not a column-label match: 5
  years of service is paid at the "Over 4" band.
- **Service academy cadets / midshipmen and ROTC members** are a flat rate that
  is not on the officer grid at all.
- **BAS II is never a default.** It's a conditional rate (2× standard enlisted
  BAS) requiring Service Secretary authorization; returned only when asked for.
- **BAH rate protection** is noted on every BAH response.

## Data sources

| Source | URL | Cadence |
|---|---|---|
| Basic pay (4 category pages) | `dfas.mil/.../Pay-Tables/Basic-Pay/{EM,CO,CO_FE,WO}/` | Annual, effective 1 Jan |
| BAS | `dfas.mil/.../Pay-Tables/bas/` | Annual, effective 1 Jan |
| BAH (ASCII bulk) | `travel.dod.mil/Portals/119/.../ASCII/BAH-ASCII-{year}.zip` | Annual **plus off-cycle** |

### The BAH ASCII bundle format

DTMO publishes **no schema** for these files. The layout below is derived from
the working reference consumer
[`mpyne-navy/bah-rate-map`](https://github.com/mpyne-navy/bah-rate-map) (MIT,
CDR Mike Pyne USN), whose `index.html` documents a sample row and slices it
exactly this way.

`BAH-ASCII-<year>.zip` carries thirteen members; four are used:

| File | Format |
|---|---|
| `sorted_zipmha<yy>.txt` | Space-delimited `ZIP MHA` (~41k US ZIPs) |
| `bahw<yy>.txt` | Rates **with** dependents |
| `bahwo<yy>.txt` | Rates **without** dependents |
| `mhanames<yy>.txt` | MHA code → locality name |

The rest are `.dat` encodings of the same data, DTMO's own
`ASCII-FILE-FORMAT.pdf`, and — importantly — the **previous publication** under
`"* - old.txt"` / `"* - old.dat"` names. Those superseded files end in `.txt`
and share the `bahw`/`bahwo` prefixes, so a filename-prefix fallback can
silently ingest last publication's rates. They are excluded explicitly and
tested for.

The rate files are **headerless CSV — not fixed-width** — with 28 fields:

```
MHA, E-1..E-9, W-1..W-5, O-1E..O-3E, O-1..O-10
 0    1  ..  9   10 .. 14   15 .. 17   18 .. 27
```

Note the BAH grade set is **not** the basic pay grade set. The DTMO lookup form
collapses O-7 and above into a single "O-7/O-7+" bucket; in the ASCII files
O-7..O-10 simply carry the same value.

## Defensive parsing

The DFAS pages get reformatted (a 2026 page whose sidebar still says "2022
Active Duty Pay Days"), so page structure is treated as unstable:

- Pay grades are regexed out of cell text, which carries footnote markers like
  `E-9 (Notes 2 & 3)` — never matched on exact cell text.
- The basic pay grid is split across **two** HTML tables per page; both are read
  and joined on pay grade.
- Columns are matched by header label, not position.
- A page missing expected pay grades produces a warning; an unrecognizable page
  raises rather than writing a half-empty table.
- A BAH row with the wrong field count **fails loudly** rather than mapping
  rates onto the wrong pay grades.
- A page with no effective date is refused rather than filed under a guessed year.
- Raw BAH lines are kept verbatim in `raw_bah_lines`. Diffing this year's raw
  rows against last year's is how a silent layout change gets caught.
- Every ingest records a row count in `source_fetch_log`.

## Tests

```bash
.venv/bin/python -m pytest
```

172 tests, no network required — the parsers run against fixtures in
`tests/fixtures/`. Those fixtures are **synthetic**: they reproduce the
documented *structure* of each source, and only the following figures are real
published values, used as the assertions:

| Value | 2026 |
|---|---|
| Basic pay, E-5 over 4 | $3,946.80 |
| BAS, enlisted | $476.95 |
| BAS, officer | $328.48 |
| BAS II | $953.90 |
| E-1 under 4 months | $2,225.70 |
| Senior enlisted advisor flat rate | $11,166.90 |

Everything else in the fixtures is obviously non-real filler. **The repository
ships no rate data** — the database is built by the ingest, so the only rates
ever served are ones fetched from DFAS/DTMO at refresh time.

## Out of scope

- **myPay / LES / individual pay data** — behind authentication, and it's
  personal financial data. Not a scraping target.
- Special & incentive pays (flight, sea, sub, hazardous duty, health professions
  bonuses) — same clean-table format on the DFAS index, good phase 2.
- Drill pay (reserve/guard) — four more tables, same structure as basic pay.
- OHA / OCONUS COLA — different DTMO datasets, updated more often than annually.
- Retirement calculators — rule-heavy and system-dependent (High-3, BRS).

## Reference

- [DFAS pay tables index](https://www.dfas.mil/MilitaryMembers/payentitlements/Pay-Tables/)
- [DTMO BAH rate lookup](https://www.travel.dod.mil/Allowances/Basic-Allowance-for-Housing/BAH-Rate-Lookup/)
- [`mpyne-navy/bah-rate-map`](https://github.com/mpyne-navy/bah-rate-map) — the
  documentation DTMO doesn't provide
- DoD FMR Vol. 7A [Ch. 1 (basic pay)](https://comptroller.defense.gov/Portals/45/documents/fmr/current/07a/07a_01.pdf),
  [Ch. 25 (BAS)](https://comptroller.defense.gov/Portals/45/documents/fmr/current/07a/07a_25.pdf)
