"""Tests for the CLI commands used to build and inspect the database."""

import pytest

from mcp_militarypay import cli


def run(db_path, *args) -> int:
    return cli.main(["--db", str(db_path), *args])


class TestVerify:
    def test_passes_on_a_fully_loaded_database(self, db_path, capsys):
        # The fixture set has no officer page, so that check should skip.
        assert run(db_path, "verify") == 0
        out = capsys.readouterr().out
        assert "2026 basic pay E-5 over 4 = 3946.8" in out
        assert "skipped" in out

    def test_checks_that_are_not_loaded_skip_rather_than_fail(self, tmp_path, capsys):
        from mcp_militarypay import db, ingest
        from tests.conftest import FIXTURES

        path = tmp_path / "bas_only.sqlite3"
        conn = db.open_for_ingest(path)
        ingest.ingest_bas(conn, html=(FIXTURES / "dfas_bas.html").read_text())
        conn.commit()
        conn.close()

        assert run(path, "verify") == 0
        out = capsys.readouterr().out
        assert out.count("[skip]") == 4

    def test_missing_database_is_an_error_not_a_traceback(self, tmp_path, capsys):
        assert run(tmp_path / "nope.sqlite3", "verify") == 1
        assert "not found" in capsys.readouterr().err


class TestLookup:
    def test_full_lookup_reports_every_component_and_the_tax_split(
        self, db_path, capsys
    ):
        assert run(db_path, "lookup", "--grade", "E-5", "--years", "4",
                   "--zip", "92101", "--dependents") == 0
        out = capsys.readouterr().out
        assert "$3,946.80" in out
        assert "SAN DIEGO, CA" in out
        assert "taxable     : $3,946.80" in out
        assert "non-taxable :" in out

    def test_bah_only(self, db_path, capsys):
        assert run(db_path, "lookup", "--grade", "O-3", "--zip", "79601") == 0
        out = capsys.readouterr().out
        assert "TX270" in out
        assert "ABILENE/DYESS AFB, TX" in out

    def test_invalid_combination_is_explained_not_priced_at_zero(
        self, db_path, capsys
    ):
        run(db_path, "lookup", "--grade", "E-8", "--years", "2")
        out = capsys.readouterr().out
        assert "not_a_valid_combination" in out
        assert "not that the rate is zero" in out

    def test_unknown_zip_is_reported(self, db_path, capsys):
        assert run(db_path, "lookup", "--grade", "E-5", "--zip", "99999") == 1
        assert "crosswalk" in capsys.readouterr().err

    def test_requires_something_to_look_up(self, db_path, capsys):
        assert run(db_path, "lookup") == 2
        assert "at least --grade" in capsys.readouterr().err


class TestNotes:
    def test_reports_captured_footnotes_and_flat_rates(self, db_path, capsys):
        assert run(db_path, "notes") == 0
        out = capsys.readouterr().out
        assert "senior_enlisted_advisor" in out
        assert "11166.9" in out
