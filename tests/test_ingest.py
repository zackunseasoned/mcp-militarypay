import pytest

from mcp_militarypay import db, ingest


@pytest.fixture
def blank(tmp_path):
    connection = db.open_for_ingest(tmp_path / "blank.sqlite3")
    yield connection
    connection.close()


def count(conn, table, where="", params=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]


class TestLogging:
    def test_success_is_logged_with_a_row_count(self, blank, enlisted_html):
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)
        row = blank.execute(
            "SELECT * FROM source_fetch_log WHERE source = ?", ("base_pay:enlisted",)
        ).fetchone()
        assert row["ok"] == 1
        assert row["row_count"] == 99
        assert "dfas.mil" in row["url"]

    def test_failure_is_logged_and_reported_not_raised(self, blank):
        """A reformatted page must fail loudly, not write garbage."""
        result = ingest.ingest_base_pay(blank, "enlisted", html="<html>nope</html>")
        assert result.ok is False
        assert result.error
        row = blank.execute(
            "SELECT * FROM source_fetch_log WHERE source = ?", ("base_pay:enlisted",)
        ).fetchone()
        assert row["ok"] == 0
        assert count(blank, "base_pay") == 0

    def test_refuses_to_guess_the_year(self, blank):
        """A page with no effective date must not be filed under the wrong year."""
        html = """<html><body><table>
        <tr><th>Pay Grade</th><th>2 or less</th><th>Over 2</th></tr>
        <tr><td>E-5</td><td>1.00</td><td>2.00</td></tr></table></body></html>"""
        result = ingest.ingest_base_pay(blank, "enlisted", html=html)
        assert result.ok is False
        assert "refusing to guess" in result.error
        assert count(blank, "base_pay") == 0


class TestReplaceSemantics:
    def test_reingest_replaces_rather_than_duplicates(self, blank, enlisted_html):
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)
        first = count(blank, "base_pay")
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)
        assert count(blank, "base_pay") == first

    def test_prior_years_survive_a_refresh(self, blank, enlisted_html):
        """Historical rates stay queryable: BAH rate protection and back-pay
        questions both need them.

        The 2026-stamped fixture is filed as 2025 only with an explicit
        override, which is what that flag is for.
        """
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html, year=2025,
                               allow_year_mismatch=True)
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html, year=2026)
        years = {r[0] for r in blank.execute("SELECT DISTINCT year FROM base_pay")}
        assert years == {2025, 2026}

    def test_bas_upserts_on_effective_date(self, blank, bas_html):
        ingest.ingest_bas(blank, html=bas_html)
        ingest.ingest_bas(blank, html=bas_html)
        assert count(blank, "bas_rates") == 3


class TestBahIngest:
    def test_writes_both_dependency_statuses(self, blank, bah_zip_bytes):
        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        assert count(blank, "bah_rates", "WHERE with_dependents = 1") == 108
        assert count(blank, "bah_rates", "WHERE with_dependents = 0") == 108

    def test_keeps_raw_lines_for_year_over_year_diffing(self, blank, bah_zip_bytes):
        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        assert count(blank, "raw_bah_lines") == 8
        files = {r[0] for r in blank.execute("SELECT DISTINCT source_file FROM raw_bah_lines")}
        assert files == {"bahw26.txt", "bahwo26.txt"}

    def test_off_cycle_set_is_partial_and_carries_no_crosswalk(self, blank, bah_zip_bytes):
        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        ingest.ingest_bah(
            blank, 2026, zip_bytes=bah_zip_bytes, rate_set_id="2026-abilene-temp",
            effective_date="2026-05-16", is_annual_baseline=False,
            restrict_to_mha=["TX270"],
        )
        assert count(blank, "bah_rates", "WHERE rate_set = ?", ("2026-abilene-temp",)) == 54
        assert count(blank, "zip_to_mha", "WHERE rate_set = ?", ("2026-abilene-temp",)) == 0
        assert count(blank, "zip_to_mha", "WHERE rate_set = ?", ("2026",)) == 7

    def test_unknown_restricted_mha_fails_rather_than_writing_nothing_silently(
        self, blank, bah_zip_bytes
    ):
        result = ingest.ingest_bah(
            blank, 2026, zip_bytes=bah_zip_bytes, rate_set_id="bogus",
            effective_date="2026-05-16", is_annual_baseline=False,
            restrict_to_mha=["XX999"],
        )
        assert result.ok is False
        assert "XX999" in result.error

    def test_malformed_bundle_writes_no_rates(self, blank):
        result = ingest.ingest_bah(blank, 2026, zip_bytes=b"not a zip file")
        assert result.ok is False
        assert count(blank, "bah_rates") == 0

    def test_refreshing_a_set_does_not_disturb_another(self, blank, bah_zip_bytes):
        ingest.ingest_bah(blank, 2025, zip_bytes=bah_zip_bytes, rate_set_id="2025")
        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes, rate_set_id="2026")
        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes, rate_set_id="2026")
        assert count(blank, "bah_rates", "WHERE rate_set = ?", ("2025",)) == 216
        assert count(blank, "bah_rates", "WHERE rate_set = ?", ("2026",)) == 216


class TestSpecialRates:
    def test_footnote_rates_are_stored(self, blank, enlisted_html):
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)
        rows = {r["key"]: r["monthly_rate"] for r in blank.execute(
            "SELECT key, monthly_rate FROM base_pay_special")}
        assert rows["e1_under_4_months"] == 2225.70
        assert rows["senior_enlisted_advisor"] == 11166.90

    def test_notes_are_stored_verbatim(self, blank, enlisted_html):
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)
        assert count(blank, "base_pay_note") > 0


class TestCrossCategorySpecials:
    """base_pay_special is keyed on (year, key), so any page can overwrite
    another's entry. The prior-enlisted page's combat zone note once replaced
    the real senior enlisted advisor rate with $225."""

    def test_prior_enlisted_page_does_not_clobber_the_enlisted_rate(
        self, blank, enlisted_html
    ):
        from tests.conftest import FIXTURES

        prior = (FIXTURES / "dfas_officer_prior_enlisted_2026.html").read_text()
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)
        ingest.ingest_base_pay(blank, "officer_prior_enlisted", html=prior)

        rate = blank.execute(
            "SELECT monthly_rate FROM base_pay_special "
            "WHERE year = 2026 AND key = 'senior_enlisted_advisor'"
        ).fetchone()["monthly_rate"]
        assert rate == 11166.90

    def test_order_of_ingest_does_not_matter(self, blank, enlisted_html):
        from tests.conftest import FIXTURES

        prior = (FIXTURES / "dfas_officer_prior_enlisted_2026.html").read_text()
        ingest.ingest_base_pay(blank, "officer_prior_enlisted", html=prior)
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html)

        rate = blank.execute(
            "SELECT monthly_rate FROM base_pay_special "
            "WHERE year = 2026 AND key = 'senior_enlisted_advisor'"
        ).fetchone()["monthly_rate"]
        assert rate == 11166.90


class TestYearMismatch:
    """Filing one year's rates under another is exactly the silent staleness
    this project exists to avoid, so it fails rather than warns."""

    def test_year_conflicting_with_the_page_is_refused(self, blank, enlisted_html):
        result = ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html,
                                        year=2025)
        assert result.ok is False
        assert "Effective January 1, 2026" in result.error
        assert count(blank, "base_pay") == 0

    def test_the_refusal_is_logged(self, blank, enlisted_html):
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html, year=2025)
        row = blank.execute(
            "SELECT ok, notes FROM source_fetch_log WHERE source = ?",
            ("base_pay:enlisted",),
        ).fetchone()
        assert row["ok"] == 0
        assert "--year 2025" in row["notes"]

    def test_override_stores_it_and_records_why(self, blank, enlisted_html):
        result = ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html,
                                        year=2025, allow_year_mismatch=True)
        assert result.ok is True
        assert any("stamped 2026 but stored as 2025" in w for w in result.warnings)

    def test_matching_year_is_untouched(self, blank, enlisted_html):
        assert ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html,
                                      year=2026).ok is True


class TestWorkbookIngest:
    """Off-cycle adjustments are published only as an updated Excel workbook."""

    def test_diffing_the_workbooks_restricts_to_the_changed_area(
        self, blank, bah_zip_bytes, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        result = ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
            label="2026 Abilene Temporary Increase",
        )
        assert result.ok is True
        # 27 grades x 2 dependency statuses, for the one changed MHA.
        assert result.rows == 54
        assert count(blank, "bah_rates", "WHERE rate_set = ?", ("2026-abilene-temp",)) == 54
        mhas = {r[0] for r in blank.execute(
            "SELECT DISTINCT mha_code FROM bah_rates WHERE rate_set = ?",
            ("2026-abilene-temp",))}
        assert mhas == {"TX270"}

    def test_the_off_cycle_set_wins_for_its_own_mha_only(
        self, blank, bah_zip_bytes, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        from mcp_militarypay import queries as q

        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        affected = q.get_bah(blank, "79601", "E-5", True)
        assert affected["rate_set"] == "2026-abilene-temp"
        assert affected["effective_date"] == "2026-05-16"

        earlier = q.get_bah(blank, "79601", "E-5", True, as_of="2026-03-01")
        assert earlier["rate_set"] == "2026"

        elsewhere = q.get_bah(blank, "92101", "E-5", True)
        assert elsewhere["rate_set"] == "2026"

    def test_senior_officer_grades_do_not_fall_back_to_the_annual_set(
        self, blank, bah_zip_bytes, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        """The workbook stops at O-7; without expansion an O-8 lookup would miss
        the off-cycle set and silently serve the superseded annual rate."""
        from mcp_militarypay import queries as q

        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        for grade in ("O-7", "O-8", "O-9", "O-10"):
            assert q.get_bah(blank, "79601", grade, True)["rate_set"] == "2026-abilene-temp"

    def test_identical_workbooks_are_refused(self, blank, bah_workbook_bytes):
        result = ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="nothing-changed", effective_date="2026-05-16",
        )
        assert result.ok is False
        assert "identical" in result.error
        assert count(blank, "bah_rates") == 0

    def test_workbook_sets_write_no_zip_crosswalk(
        self, blank, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        assert count(blank, "zip_to_mha") == 0

    def test_marking_a_workbook_set_as_baseline_warns_about_the_crosswalk(
        self, blank, bah_workbook_bytes
    ):
        result = ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-xlsx", effective_date="2026-01-01",
            is_annual_baseline=True,
        )
        assert result.ok is True
        assert any("crosswalk" in w for w in result.warnings)


class TestAnnualBaselineRestore:
    """DTMO republishes the annual bundle in place when a mid-year adjustment
    lands, so the annual set silently acquires the post-change rates and an
    as-of query before the effective date returns them. The pre-change figures
    survive only in the baseline workbook."""

    def _seed_republished_annual(self, conn, updated_bytes):
        """An annual set already carrying the post-change rates, as the
        republished ASCII bundle does."""
        return ingest.ingest_bah_workbook(
            conn, 2026, xlsx_bytes=updated_bytes, rate_set_id="2026",
            effective_date="2026-01-01", is_annual_baseline=True,
        )

    def test_pre_change_rates_are_restored_into_the_annual_set(
        self, blank, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        self._seed_republished_annual(blank, bah_workbook_increase_bytes)
        overwritten = blank.execute(
            "SELECT monthly_rate FROM bah_rates WHERE rate_set='2026' AND "
            "mha_code='TX270' AND pay_grade='E-5' AND with_dependents=1"
        ).fetchone()["monthly_rate"]

        result = ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        assert result.ok is True
        assert any("restored" in w for w in result.warnings)

        restored = blank.execute(
            "SELECT monthly_rate FROM bah_rates WHERE rate_set='2026' AND "
            "mha_code='TX270' AND pay_grade='E-5' AND with_dependents=1"
        ).fetchone()["monthly_rate"]
        assert restored < overwritten

    def test_as_of_before_the_increase_returns_the_january_rate(
        self, blank, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        from mcp_militarypay import queries as q

        self._seed_republished_annual(blank, bah_workbook_increase_bytes)
        blank.execute(
            "INSERT OR REPLACE INTO zip_to_mha(rate_set, zip_code, mha_code) "
            "VALUES('2026', '79601', 'TX270')"
        )
        ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        current = q.get_bah(blank, "79601", "E-5", True)
        earlier = q.get_bah(blank, "79601", "E-5", True, as_of="2026-03-01")
        assert current["rate_set"] == "2026-abilene-temp"
        assert earlier["rate_set"] == "2026"
        assert earlier["monthly_rate"] < current["monthly_rate"]

    def test_unaffected_areas_are_left_alone(
        self, blank, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        self._seed_republished_annual(blank, bah_workbook_increase_bytes)
        before = dict(blank.execute(
            "SELECT pay_grade, monthly_rate FROM bah_rates WHERE rate_set='2026' "
            "AND mha_code='CA606' AND with_dependents=1").fetchall())
        ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        after = dict(blank.execute(
            "SELECT pay_grade, monthly_rate FROM bah_rates WHERE rate_set='2026' "
            "AND mha_code='CA606' AND with_dependents=1").fetchall())
        assert before == after

    def test_restore_can_be_declined(
        self, blank, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        self._seed_republished_annual(blank, bah_workbook_increase_bytes)
        before = blank.execute(
            "SELECT monthly_rate FROM bah_rates WHERE rate_set='2026' AND "
            "mha_code='TX270' AND pay_grade='E-5' AND with_dependents=1"
        ).fetchone()["monthly_rate"]
        ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
            restore_annual_baseline=False,
        )
        after = blank.execute(
            "SELECT monthly_rate FROM bah_rates WHERE rate_set='2026' AND "
            "mha_code='TX270' AND pay_grade='E-5' AND with_dependents=1"
        ).fetchone()["monthly_rate"]
        assert after == before

    def test_an_untouched_annual_set_reports_nothing_to_restore(
        self, blank, bah_zip_bytes, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        """When the annual bundle was captured before the republish, its rates
        already are the January ones."""
        from mcp_militarypay.parsers.bah_xlsx import parse_bah_workbook

        ingest.ingest_bah(blank, 2026, zip_bytes=bah_zip_bytes)
        # Align the ASCII-sourced annual rows with the baseline workbook, as
        # they would be had the bundle been captured before the republish.
        baseline = parse_bah_workbook(bah_workbook_bytes)
        for (mha, grade), rate in baseline.with_dependents.rates.items():
            blank.execute(
                "UPDATE bah_rates SET monthly_rate = ? WHERE rate_set = '2026' "
                "AND mha_code = ? AND pay_grade = ? AND with_dependents = 1",
                (rate, mha, grade),
            )
        result = ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        assert any("nothing to restore" in w for w in result.warnings)

    def test_restore_never_invents_rows(
        self, blank, bah_workbook_bytes, bah_workbook_increase_bytes
    ):
        """With no annual set present there is nothing to correct."""
        result = ingest.ingest_bah_workbook(
            blank, 2026, xlsx_bytes=bah_workbook_increase_bytes,
            baseline_xlsx_bytes=bah_workbook_bytes,
            rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        )
        assert any("no annual baseline rate set" in w for w in result.warnings)
        assert count(blank, "bah_rates", "WHERE rate_set = ?", ("2026",)) == 0
