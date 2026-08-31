import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).parent / "fixtures"

from mcp_militarypay import db, ingest  # noqa: E402


@pytest.fixture(scope="session")
def enlisted_html() -> str:
    return (FIXTURES / "dfas_enlisted_2026.html").read_text()


@pytest.fixture(scope="session")
def bas_html() -> str:
    return (FIXTURES / "dfas_bas.html").read_text()


@pytest.fixture(scope="session")
def bah_zip_bytes() -> bytes:
    return (FIXTURES / "BAH-ASCII-2026.zip").read_bytes()


@pytest.fixture(scope="session")
def bah_workbook_bytes() -> bytes:
    return (FIXTURES / "2026_BAH_Rates.xlsx").read_bytes()


@pytest.fixture(scope="session")
def bah_workbook_increase_bytes() -> bytes:
    return (FIXTURES / "2026_BAH_Rates_TX270_increase.xlsx").read_bytes()


@pytest.fixture
def db_path(tmp_path, enlisted_html, bas_html, bah_zip_bytes) -> Path:
    """A database built from the fixtures, including the off-cycle rate set."""
    path = tmp_path / "test.sqlite3"
    connection = db.open_for_ingest(path)

    assert ingest.ingest_base_pay(connection, "enlisted", html=enlisted_html).ok
    assert ingest.ingest_bas(connection, html=bas_html).ok
    assert ingest.ingest_bah(connection, 2026, zip_bytes=bah_zip_bytes).ok
    assert ingest.ingest_bah(
        connection, 2026, zip_bytes=bah_zip_bytes,
        rate_set_id="2026-abilene-temp", effective_date="2026-05-16",
        label="2026 Abilene Temporary Increase", is_annual_baseline=False,
        restrict_to_mha=["TX270"],
    ).ok

    connection.commit()
    connection.close()
    return path


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path, read_only=True)
    yield connection
    connection.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"
