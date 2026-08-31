-- Schema for the DFAS/DTMO military pay rate database.
-- Applied idempotently by db.apply_schema(). See db.SCHEMA_VERSION.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Raw landing layer (BAH only)
--
-- BAH's row format is undocumented by DTMO, so we keep the source text verbatim.
-- Diffing this year's raw rows against last year's is how a silent layout change
-- gets caught. The DFAS HTML tables are small enough to eyeball against the live
-- page, so they write straight through with no raw layer.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_bah_lines (
  source_file  TEXT    NOT NULL,   -- e.g. 'bahw26.txt'
  rate_set     TEXT    NOT NULL,
  fetched_at   TEXT    NOT NULL,   -- ISO 8601
  line_no      INTEGER NOT NULL,
  raw_text     TEXT    NOT NULL,
  PRIMARY KEY (source_file, rate_set, line_no)
);

-- ---------------------------------------------------------------------------
-- Normalized layer
-- ---------------------------------------------------------------------------

-- A BAH rate set is a year OR an off-cycle adjustment. BAH is not immutable per
-- calendar year: e.g. the 2026 Abilene/Dyess AFB (MHA TX270) temporary increase
-- effective 2026-05-16. Modelling by effective_date rather than year alone is
-- what stops a stale rate being served for such an MHA.
CREATE TABLE IF NOT EXISTS bah_rate_set (
  id              TEXT PRIMARY KEY,   -- '2026', '2026-abilene-temp'
  year            INTEGER NOT NULL,
  effective_date  TEXT    NOT NULL,   -- ISO 8601 date
  label           TEXT,
  -- 1 = the annual publication covering every MHA (the ZIP crosswalk baseline).
  -- 0 = a partial off-cycle set covering only some MHAs.
  is_annual_baseline INTEGER NOT NULL DEFAULT 1,
  source_url      TEXT    NOT NULL,
  fetched_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bah_rates (
  rate_set        TEXT    NOT NULL REFERENCES bah_rate_set(id) ON DELETE CASCADE,
  mha_code        TEXT    NOT NULL,      -- 'CA606', 'TX270'
  mha_name        TEXT,
  pay_grade       TEXT    NOT NULL,      -- BAH grade set: E-1..E-9, W-1..W-5, O1E..O3E, O-1..O-10
  with_dependents INTEGER NOT NULL,      -- 0 / 1
  monthly_rate    REAL    NOT NULL,
  PRIMARY KEY (rate_set, mha_code, pay_grade, with_dependents)
);

CREATE TABLE IF NOT EXISTS zip_to_mha (
  rate_set TEXT NOT NULL REFERENCES bah_rate_set(id) ON DELETE CASCADE,
  zip_code TEXT NOT NULL,
  mha_code TEXT NOT NULL,
  PRIMARY KEY (rate_set, zip_code)
);

-- Long, not wide: the DFAS years-of-service columns are unpivoted on the way in.
-- Storing yos_min as an integer makes the lookup a banding query
--   WHERE yos_min <= :yos ORDER BY yos_min DESC LIMIT 1
-- rather than matching on column labels like 'Over 18'.
--
-- monthly_rate is NULL where the grade/YOS combination does not exist (E-8 has
-- no rate below 'Over 8', E-9 none below 'Over 10'). NULL is not zero, and the
-- lookup reports it as "not a valid combination".
CREATE TABLE IF NOT EXISTS base_pay (
  year         INTEGER NOT NULL,
  category     TEXT    NOT NULL,   -- enlisted|officer|officer_prior_enlisted|warrant
  pay_grade    TEXT    NOT NULL,
  yos_min      INTEGER NOT NULL,   -- 0 for '2 or less', 2 for 'Over 2', ...
  monthly_rate REAL,
  PRIMARY KEY (year, category, pay_grade, yos_min)
);

-- Footnotes carrying real entitlement logic, kept verbatim so callers see them.
CREATE TABLE IF NOT EXISTS base_pay_note (
  year      INTEGER NOT NULL,
  category  TEXT    NOT NULL,
  pay_grade TEXT,                  -- NULL = applies to the whole table
  note      TEXT    NOT NULL,
  PRIMARY KEY (year, category, pay_grade, note)
);

-- Flat rates that override the YOS grid entirely (E-1 with <4 months active
-- duty; senior enlisted advisor billets). Kept separate from base_pay because
-- they are not a function of years of service.
CREATE TABLE IF NOT EXISTS base_pay_special (
  year         INTEGER NOT NULL,
  key          TEXT    NOT NULL,   -- 'e1_under_4_months' | 'senior_enlisted_advisor'
  category     TEXT    NOT NULL,
  pay_grade    TEXT,
  label        TEXT    NOT NULL,
  monthly_rate REAL,               -- NULL if the note existed but no rate could be extracted
  note         TEXT,
  PRIMARY KEY (year, key)
);

CREATE TABLE IF NOT EXISTS bas_rates (
  effective_date TEXT PRIMARY KEY, -- '2026-01-01'
  officer_rate   REAL NOT NULL,
  enlisted_rate  REAL NOT NULL,
  bas_ii_rate    REAL NOT NULL,
  source_url     TEXT,
  fetched_at     TEXT
);

CREATE TABLE IF NOT EXISTS source_fetch_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source     TEXT NOT NULL,
  url        TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  row_count  INTEGER,
  ok         INTEGER NOT NULL,
  notes      TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zip ON zip_to_mha(zip_code);
CREATE INDEX IF NOT EXISTS idx_bah_lookup ON bah_rates(rate_set, mha_code, pay_grade, with_dependents);
CREATE INDEX IF NOT EXISTS idx_bah_set_year ON bah_rate_set(year, effective_date);
CREATE INDEX IF NOT EXISTS idx_base_pay_lookup ON base_pay(year, pay_grade, yos_min);
