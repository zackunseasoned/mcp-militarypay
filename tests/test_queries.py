import pytest

from mcp_militarypay import queries as q
from mcp_militarypay.queries import LookupError_


class TestBasePay:
    def test_known_published_value(self, conn):
        """E-5 over 4 = $3,946.80 for 2026."""
        assert q.get_base_pay(conn, "E-5", 4)["monthly_rate"] == 3946.80

    def test_bands_down_to_the_highest_threshold_at_or_below(self, conn):
        """5 years of service is paid at the 'Over 4' band, not 'Over 6'."""
        result = q.get_base_pay(conn, "E-5", 5)
        assert result["monthly_rate"] == 3946.80
        assert result["yos_band_min"] == 4

    def test_zero_years_uses_the_2_or_less_column(self, conn):
        assert q.get_base_pay(conn, "E-5", 0)["yos_band_min"] == 0

    def test_beyond_the_last_column_uses_the_last_band(self, conn):
        assert q.get_base_pay(conn, "E-5", 45)["yos_band_min"] == 26

    def test_invalid_combination_is_null_not_zero(self, conn):
        """E-8 at 2 years is a blank cell on the table, not a $0 entitlement."""
        result = q.get_base_pay(conn, "E-8", 2)
        assert result["monthly_rate"] is None
        assert result["rate_basis"] == "not_a_valid_combination"
        assert "not a published combination" in result["explanation"]

    def test_e1_under_four_months_is_a_different_rate(self, conn):
        standard = q.get_base_pay(conn, "E-1", 0)
        junior = q.get_base_pay(conn, "E-1", 0, months_active_duty=3)
        assert standard["monthly_rate"] == 2407.20
        assert junior["monthly_rate"] == 2225.70
        assert junior["rate_basis"] == "e1_under_4_months"
        assert junior["monthly_rate"] < standard["monthly_rate"]

    def test_e1_at_four_months_uses_the_table_rate(self, conn):
        assert q.get_base_pay(conn, "E-1", 0, months_active_duty=4)["monthly_rate"] == 2407.20

    def test_months_active_duty_does_not_affect_other_grades(self, conn):
        assert q.get_base_pay(conn, "E-5", 4, months_active_duty=1)["monthly_rate"] == 3946.80

    def test_senior_enlisted_advisor_is_a_flat_rate(self, conn):
        for years in (10, 20, 30):
            result = q.get_base_pay(conn, "E-9", years, senior_enlisted_advisor=True)
            assert result["monthly_rate"] == 11166.90
            assert result["rate_basis"] == "senior_enlisted_advisor_flat_rate"

    def test_carries_effective_date_and_source(self, conn):
        result = q.get_base_pay(conn, "E-5", 4)
        assert result["effective_date"] == "2026-01-01"
        assert "dfas.mil" in result["source_url"]
        assert result["disclaimer"]

    def test_surfaces_footnotes(self, conn):
        assert q.get_base_pay(conn, "E-5", 4)["notes"]

    def test_annual_rate_is_twelve_months(self, conn):
        result = q.get_base_pay(conn, "E-5", 4)
        assert result["annual_rate"] == pytest.approx(result["monthly_rate"] * 12)

    def test_rejects_negative_service(self, conn):
        with pytest.raises(LookupError_):
            q.get_base_pay(conn, "E-5", -1)

    def test_unknown_grade_for_year_raises(self, conn):
        with pytest.raises(LookupError_, match="no 2026 basic pay data"):
            q.get_base_pay(conn, "O-5", 10, 2026)


class TestBah:
    def test_resolves_zip_to_mha(self, conn):
        assert q.resolve_zip(conn, "79601", 2026) == "TX270"

    def test_accepts_short_zip_with_lost_leading_zero(self, conn):
        assert q.resolve_zip(conn, "501", 2026) == "ZZ998"

    def test_unknown_zip_raises(self, conn):
        with pytest.raises(LookupError_, match="not in the 2026 BAH ZIP-to-MHA"):
            q.resolve_zip(conn, "99999", 2026)

    def test_rejects_non_zip(self, conn):
        with pytest.raises(LookupError_):
            q.resolve_zip(conn, "abcde", 2026)

    def test_dependents_rate_exceeds_without(self, conn):
        with_dep = q.get_bah(conn, "92101", "E-5", True)["monthly_rate"]
        without = q.get_bah(conn, "92101", "E-5", False)["monthly_rate"]
        assert with_dep > without

    def test_is_non_taxable(self, conn):
        assert q.get_bah(conn, "92101", "E-5", True)["taxable"] is False

    def test_carries_mha_and_effective_date(self, conn):
        result = q.get_bah(conn, "92101", "E-5", True)
        assert result["mha_code"] == "CA606"
        assert result["effective_date"] == "2026-01-01"
        assert result["source_url"]

    def test_resolves_the_mha_locality_name(self, conn):
        """An MHA code alone is unreadable; the bundle ships the names."""
        assert q.get_bah(conn, "92101", "E-5", True)["mha_name"] == "SAN DIEGO, CA"
        assert q.get_bah(conn, "79601", "E-5", True)["mha_name"] == "ABILENE/DYESS AFB, TX"

    def test_rate_protection_is_always_noted(self, conn):
        notes = " ".join(q.get_bah(conn, "92101", "E-5", True)["notes"])
        assert "rate protection" in notes

    def test_senior_officer_collapse_is_noted(self, conn):
        notes = " ".join(q.get_bah(conn, "92101", "O-9", True)["notes"])
        assert "O-7/O-7+" in notes


class TestOffCycleRateSet:
    """The Abilene TX270 temporary increase is the case that proves modelling
    BAH by effective_date rather than by calendar year alone."""

    def test_affected_mha_uses_the_off_cycle_set_by_default(self, conn):
        result = q.get_bah(conn, "79601", "E-5", True)
        assert result["rate_set"] == "2026-abilene-temp"
        assert result["effective_date"] == "2026-05-16"

    def test_off_cycle_set_is_flagged_in_the_notes(self, conn):
        notes = " ".join(q.get_bah(conn, "79601", "E-5", True)["notes"])
        assert "off-cycle" in notes

    def test_as_of_before_the_increase_uses_the_annual_set(self, conn):
        result = q.get_bah(conn, "79601", "E-5", True, as_of="2026-03-01")
        assert result["rate_set"] == "2026"
        assert result["effective_date"] == "2026-01-01"

    def test_other_mhas_are_unaffected(self, conn):
        assert q.get_bah(conn, "92101", "E-5", True)["rate_set"] == "2026"

    def test_off_cycle_set_ships_no_crosswalk_of_its_own(self, conn):
        """ZIP resolution must go through the annual baseline."""
        rows = conn.execute(
            "SELECT COUNT(*) FROM zip_to_mha WHERE rate_set = ?",
            ("2026-abilene-temp",),
        ).fetchone()[0]
        assert rows == 0
        assert q.resolve_zip(conn, "79601", 2026) == "TX270"


class TestBas:
    def test_known_published_2026_values(self, conn):
        assert q.get_bas(conn, "enlisted")["monthly_rate"] == 476.95
        assert q.get_bas(conn, "officer")["monthly_rate"] == 328.48

    def test_bas_ii_only_when_asked(self, conn):
        assert q.get_bas(conn, "enlisted")["monthly_rate"] == 476.95
        explicit = q.get_bas(conn, "enlisted", bas_ii=True)
        assert explicit["monthly_rate"] == 953.90
        assert any("Service Secretary" in n for n in explicit["notes"])

    def test_warrant_officers_get_the_officer_rate(self, conn):
        assert q.get_bas(conn, "warrant")["monthly_rate"] == 328.48

    def test_is_non_taxable(self, conn):
        assert q.get_bas(conn, "enlisted")["taxable"] is False

    def test_historical_year_is_queryable(self, conn):
        assert q.get_bas(conn, "enlisted", 2011)["effective_date"] == "2011-01-01"

    def test_year_between_publications_uses_the_prior_rate(self, conn):
        assert q.get_bas(conn, "enlisted", 2015)["effective_date"] == "2011-01-01"

    def test_rejects_bad_type(self, conn):
        with pytest.raises(LookupError_):
            q.get_bas(conn, "sergeant")


class TestTotalCompensation:
    def test_splits_taxable_from_non_taxable(self, conn):
        result = q.estimate_total_compensation(conn, "E-5", 4, "92101", True)
        monthly = result["monthly"]
        assert monthly["base_pay_taxable"] == 3946.80
        assert monthly["bas_non_taxable"] == 476.95
        assert monthly["taxable_total"] == 3946.80
        assert monthly["non_taxable_total"] == pytest.approx(
            monthly["bah_non_taxable"] + 476.95
        )
        assert monthly["gross_total"] == pytest.approx(
            monthly["taxable_total"] + monthly["non_taxable_total"]
        )

    def test_annual_is_twelve_times_monthly(self, conn):
        result = q.estimate_total_compensation(conn, "E-5", 4, "92101", True)
        assert result["annual"]["gross_total"] == pytest.approx(
            result["monthly"]["gross_total"] * 12
        )

    def test_includes_every_component_breakdown(self, conn):
        result = q.estimate_total_compensation(conn, "E-5", 4, "92101", True)
        assert set(result["components"]) == {"base_pay", "bah", "bas"}
        assert result["complete"] is True

    def test_officer_gets_the_officer_bas_rate(self, conn):
        result = q.estimate_total_compensation(conn, "E-5", 4, "92101", True)
        assert result["components"]["bas"]["pay_grade_type"] == "enlisted"

    def test_reports_partial_results_rather_than_failing(self, conn):
        """An unknown ZIP should not lose the base pay and BAS answers."""
        result = q.estimate_total_compensation(conn, "E-5", 4, "99999", True)
        assert result["complete"] is False
        assert "bah" in result["errors"]
        assert result["monthly"]["base_pay_taxable"] == 3946.80

    def test_carries_the_tax_note_and_disclaimer(self, conn):
        result = q.estimate_total_compensation(conn, "E-5", 4, "92101", True)
        assert "non-taxable" in result["tax_note"]
        assert "DFAS" in result["disclaimer"]


class TestStatus:
    def test_reports_loaded_data(self, conn):
        status = q.database_status(conn)
        assert status["base_pay_years"] == [2026]
        assert status["bas_rows"] == 3
        assert {s["id"] for s in status["bah_rate_sets"]} == {"2026", "2026-abilene-temp"}
        assert status["recent_fetches"]
