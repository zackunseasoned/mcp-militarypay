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
        questions both need them."""
        ingest.ingest_base_pay(blank, "enlisted", html=enlisted_html, year=2025)
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
