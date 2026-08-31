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
python -m mcp_militarypay.cli lookup --grade E-5 --years 6 --zip 92101 --dependents
```

`lookup` is an ad-hoc query for spot-checking against the published DFAS and
DTMO lookups. It calls the same functions the MCP tools call, so a spot-check
exercises the real path rather than a parallel one.

`notes` prints the footnote text captured from the DFAS pages and the flat
rates read out of it, flagging any rate that could not be extracted. Those
patterns are the most fragile part of the ingest, so this is the quickest way
to check them against the live page.

The database defaults to `data/militarypay.sqlite3` inside the checkout, or
`~/.mcp-militarypay/` when installed as a normal (non-editable) package.
Override with `--db` or the `MILITARYPAY_DB` environment variable.

An explicit `--year` that disagrees with the page's own "Effective January 1,
YYYY" stamp is **refused** rather than stored, since filing one year's rates
under another is precisely the silent staleness this is meant to avoid. Pass
`--allow-year-mismatch` when that is deliberate.

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
    --year 2026 \
    --from-file "2026_BAH_Rates__Updated_with_TX270_Temporary_Increase.xlsx" \
    --baseline-file "2026_BAH_Rates.xlsx"
```

Both workbooks come from the [BAH rate lookup
page](https://www.travel.dod.mil/Allowances/Basic-Allowance-for-Housing/BAH-Rate-Lookup/).
`--baseline-file` does two jobs.

First, it derives the affected areas by **diffing the two workbooks** rather
than trusting the filename: an off-cycle publication is the
full annual table with a handful of areas changed, so ingesting all of it would
duplicate 337 unchanged MHAs and blur which rates actually moved. (For the 2026
TX270 increase the diff returns exactly `TX270`.) Use `--mha` instead to name
the areas explicitly.

Second, it **restores the pre-change rates into the annual set** for those
areas. This matters more than it sounds: DTMO republishes the annual ASCII
bundle *in place* when a mid-year adjustment lands, so once TX270 rises in May
the January bundle no longer exists anywhere — a freshly downloaded
`BAH-ASCII-2026.zip` already carries the increased figure under an effective
date of 1 January. Without the restore, `as_of=2026-03-01` returns the May rate
for a member who was actually drawing the January one, which is precisely the
back-pay and rate-protection question `as_of` exists to answer. The baseline
workbook is the only surviving record of the original rates. Only rows that
already exist are updated, only where the value differs, and the ingest reports
how many were corrected. `--no-restore-annual` opts out.

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

The suite includes an integration test that launches this as a subprocess and
speaks MCP to it over stdio, so the entry point, negotiation and error handling
are covered on the same path a real client uses — including on Windows in CI.

### Installing as an extension (recommended on Windows)

The simplest route, and the one that avoids config-file trouble entirely:

```bash
python packaging/build_mcpb.py      # -> dist/militarypay-<version>.mcpb
```

Then in Claude: **Settings → Extensions → Install Extension**, pick the
`.mcpb`, and point it at your `militarypay.sqlite3` when it asks.

This matters on the Microsoft Store build of Claude, which runs in an MSIX
container with a **virtualised `%APPDATA%`** — a hand-edited
`claude_desktop_config.json` under `%APPDATA%\Claude\` may not be the file the
app actually reads, and the server then never loads with nothing obvious to
show for it. Installing a bundle goes through the app's own flow instead.

Build it with the interpreter you want it to run under: dependencies are
vendored from the running environment, so build on Windows to get Windows
wheels. The bundle carries only what the *server* needs — it serves from an
already-built database and never fetches, so the ingest's dependencies are left
out. It needs no virtualenv at run time.

**The manifest pins an absolute interpreter path.** A host may launch `python3`
rather than the `python` a manifest asks for, and on a machine with several
Pythons installed that can be a different version than the vendored wheels were
built against — `pydantic_core` then fails to import its compiled extension, or
`python3` isn't on `PATH` at all and the spawn fails outright. The build pins
the interpreter matching the vendored ABI (`sys.base_prefix`, not the
virtualenv, so the bundle doesn't depend on the checkout staying put) and
**refuses to pack if that interpreter can't import them**. `--command python`
restores host resolution if you want a portable bundle and accept the risk.

### Registering by hand

Register it with an MCP client (stdio transport):

```json
{
  "mcpServers": {
    "militarypay": {
      "command": "C:\\path\\to\\mcp-militarypay\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_militarypay.server"],
      "env": { "MILITARYPAY_DB": "C:\\path\\to\\mcp-militarypay\\data\\militarypay.sqlite3" }
    }
  }
}
```

**`command` must be the interpreter from the virtual environment the package
was installed into**, not a system Python. A system interpreter cannot import
`mcp_militarypay` and the server exits with `ModuleNotFoundError` before it
speaks any MCP, which a client reports only as a server that failed to start.
Check it before restarting the client:

```bash
/path/to/.venv/bin/python -c "import mcp_militarypay; print('ok')"
```

The virtual environment also installs a console script, which avoids the
question entirely — use it as `command` with no `args`:

```
.venv/bin/militarypay-mcp          # Windows: .venv\Scripts\militarypay-mcp.exe
```

## Tools

All five are read-only (`readOnlyHint`, `idempotentHint`, `openWorldHint: false`
— they query the local database, not the live web). Every response carries the
**effective date and source URL** of the figures used.

| Tool | Returns |
|---|---|
| `get_base_pay(pay_grade, years_of_service, year?, months_active_duty?, senior_enlisted_advisor?)` | Monthly basic pay (taxable) plus applicable footnotes |
| `get_bah(zip_code, pay_grade, has_dependents, year?, as_of?)` | Monthly BAH (non-taxable), resolved MHA code, rate set and effective date |
| `find_housing_area(query, year?, limit?)` | The Military Housing Area for a place, MHA code or ZIP, with ZIP codes to query it with |
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
- **BAH follows the permanent duty station**, not the member's residence. A
  member assigned to Travis AFB but living in Winters draws the Travis rate,
  even though the home ZIP resolves to a Sacramento-area housing area and a
  lower figure. Every BAH response says so, and the `zip_code` parameter
  description says it where a caller reads it.
- **BAH rate protection** is noted on every BAH response.
- **Housing areas are looked up, not guessed.** BAH is published per Military
  Housing Area but `get_bah` takes a ZIP, so a caller with only a place name
  would otherwise supply a ZIP from memory — and a wrong one resolves to
  another real area and returns a confident rate for the wrong locality.
  `find_housing_area` searches by locality name, MHA code or ZIP and returns
  ZIP codes to pass on. DTMO names areas for localities, so an installation
  matches only where the published name includes it (`Travis AFB` finds
  `VALLEJO/TRAVIS AFB, CA`; Redstone Arsenal is inside `HUNTSVILLE, AL`) — the
  tool description says so, so a miss is not read as "no such place".

## Data sources

| Source | URL | Cadence |
|---|---|---|
| Basic pay (4 category pages) | `dfas.mil/.../Pay-Tables/Basic-Pay/{EM,CO,CO_FE,WO}/` | Annual, effective 1 Jan |
| BAS | `dfas.mil/.../Pay-Tables/bas/` | Annual, effective 1 Jan |
| BAH (ASCII bulk) | `travel.dod.mil/Portals/119/.../ASCII/BAH-ASCII-{year}.zip` | Annual **plus off-cycle** |

### The BAH ASCII bundle format

DTMO does publish a schema, but it is shipped **inside the bundle itself** as
`ASCII-FILE-FORMAT.pdf` rather than on the website. It confirms the delimiters,
the `CHAR(5)` MHA key, and the grade order — including the counterintuitive
part, that `O1E/O2E/O3E` come **before** `O1`.

That PDF's field list stops at `O7` (25 fields) where the published files carry
**28**, and since the list also runs off the bottom of its single page it looks
truncated. It isn't. DTMO's Excel workbook independently publishes exactly 24
rate columns ending at `O07`, so **`O-7` really is the last distinct grade** —
the ASCII files pad three further columns repeating the `O-7` value, which is
DTMO's "O-7/O-7+" bucket. Reading that tail as `O-8`/`O-9`/`O-10` gives correct
rates either way, and `verify` checks those four columns agree within every MHA.

### The Excel workbook

The other bulk download, and the **only** published form the off-cycle
adjustments appear in — there is no separate ASCII bundle for, say, the 2026
TX270 temporary increase. It is a clean grid rather than the government-Excel
hazard the design anticipated: two sheets (`With` / `Without`), a title row, a
header row, one row per MHA.

```
MHA | MHA_NAME | E01..E09 | W01..W05 | O01E O02E O03E | O01..O07
```

It carries **no ZIP-to-MHA crosswalk**, so a set ingested from it is not an
annual baseline; ZIP resolution goes through the annual set that has one.

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

272 tests, no network required — the parsers run against fixtures in
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
