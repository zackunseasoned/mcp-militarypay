import io
import zipfile

import pytest

from mcp_militarypay.parsers.bah import (
    ParseError,
    parse_bah_bundle,
    parse_rate_file,
    parse_zip_mha,
)
from mcp_militarypay.sources import BAH_ROW_FIELD_COUNT


def _row(mha="TX270", value=1000.0):
    return ",".join([mha] + [f"{value:.2f}"] * 27)


class TestZipMhaCrosswalk:
    def test_parses_space_delimited_pairs(self):
        mapping = parse_zip_mha("79601 TX270\n92101 CA606\n")
        assert mapping == {"79601": "TX270", "92101": "CA606"}

    def test_preserves_leading_zeros(self):
        """00501 is a real ZIP; losing the leading zero silently misroutes it."""
        assert parse_zip_mha("501 NY123\n")["00501"] == "NY123"

    def test_rejects_malformed_lines(self):
        with pytest.raises(ParseError):
            parse_zip_mha("79601\n")
        with pytest.raises(ParseError):
            parse_zip_mha("ABCDE TX270\n")

    def test_rejects_empty_file(self):
        with pytest.raises(ParseError):
            parse_zip_mha("\n\n")


class TestRateFile:
    def test_maps_all_27_grades(self):
        parsed = parse_rate_file(_row(), with_dependents=True, source_file="bahw26.txt")
        assert len(parsed.rates) == 27
        assert parsed.rates[("TX270", "E-1")] == 1000.0
        assert parsed.rates[("TX270", "O-3E")] == 1000.0
        assert parsed.rates[("TX270", "O-10")] == 1000.0

    def test_wrong_field_count_fails_loudly(self):
        """A changed DTMO layout must not be mapped onto the wrong pay grades."""
        with pytest.raises(ParseError, match=str(BAH_ROW_FIELD_COUNT)):
            parse_rate_file("TX270,1.00,2.00\n", with_dependents=True,
                            source_file="bahw26.txt")

    def test_non_mha_first_field_fails(self):
        with pytest.raises(ParseError, match="not an MHA code"):
            parse_rate_file(_row(mha="NOTANMHA"), with_dependents=True,
                            source_file="bahw26.txt")

    def test_non_numeric_rate_fails(self):
        bad = ",".join(["TX270"] + ["abc"] * 27)
        with pytest.raises(ParseError, match="not a number"):
            parse_rate_file(bad, with_dependents=True, source_file="bahw26.txt")

    def test_keeps_raw_lines_for_diffing(self):
        parsed = parse_rate_file(_row(), with_dependents=True, source_file="bahw26.txt")
        assert parsed.raw_lines and parsed.raw_lines[0][0] == 1


class TestBundle:
    def test_parses_all_three_files(self, bah_zip_bytes):
        bundle = parse_bah_bundle(bah_zip_bytes, 2026)
        assert bundle.year == 2026
        assert bundle.zip_to_mha["79601"] == "TX270"
        assert bundle.with_dependents.rates[("TX270", "E-5")] > 0
        assert bundle.without_dependents.rates[("TX270", "E-5")] > 0
        assert bundle.warnings == []

    def test_with_dependents_rates_exceed_without(self, bah_zip_bytes):
        bundle = parse_bah_bundle(bah_zip_bytes, 2026)
        for key, rate in bundle.with_dependents.rates.items():
            assert rate > bundle.without_dependents.rates[key]

    def test_senior_officer_grades_collapse(self, bah_zip_bytes):
        """DTMO collapses O-7+ into one bucket; the file repeats the value."""
        rates = parse_bah_bundle(bah_zip_bytes, 2026).with_dependents.rates
        values = {rates[("TX270", g)] for g in ("O-7", "O-8", "O-9", "O-10")}
        assert len(values) == 1

    def test_falls_back_to_prefix_match_on_unexpected_filenames(self):
        """The <yy> filename convention is only confirmed for 2023."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("sorted_zipmha_new.txt", "79601 TX270\n")
            archive.writestr("bahw_new.txt", _row(value=2000))
            archive.writestr("bahwo_new.txt", _row(value=1800))
        bundle = parse_bah_bundle(buffer.getvalue(), 2099)
        assert bundle.with_dependents.rates[("TX270", "E-5")] == 2000.0
        assert bundle.without_dependents.rates[("TX270", "E-5")] == 1800.0

    def test_rejects_bundle_where_both_files_resolve_the_same(self):
        """'bahw' prefixes 'bahwo' - the two rate sets must stay distinguishable."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("sorted_zipmha99.txt", "79601 TX270\n")
            archive.writestr("bahwo99.txt", _row())
        with pytest.raises(ParseError):
            parse_bah_bundle(buffer.getvalue(), 2099)

    def test_warns_when_bundle_does_not_hold_three_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("sorted_zipmha26.txt", "79601 TX270\n")
            archive.writestr("bahw26.txt", _row(value=2000))
            archive.writestr("bahwo26.txt", _row(value=1800))
            archive.writestr("readme.txt", "extra")
        bundle = parse_bah_bundle(buffer.getvalue(), 2026)
        assert any("expected 3 files" in w for w in bundle.warnings)
