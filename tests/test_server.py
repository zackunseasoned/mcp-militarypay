"""Exercise the tools through the MCP layer, not just the query functions."""

import json

import pytest
from fastmcp import Client

from mcp_militarypay import server
from tests.conftest import FIXTURES


@pytest.fixture
def mcp_client(db_path):
    server.configure(db_path)
    yield Client(server.mcp)
    server.configure(None)


def payload(result):
    if getattr(result, "structured_content", None):
        content = result.structured_content
        return content.get("result", content)
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_all_tools_are_read_only(mcp_client):
    async with mcp_client as client:
        tools = await client.list_tools()
    assert {t.name for t in tools} == {
        "get_base_pay", "get_bah", "find_housing_area", "get_bas",
        "estimate_total_compensation", "get_database_status",
    }
    for tool in tools:
        annotations = tool.annotations
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        # The tools query a local database, not the live web.
        assert annotations.openWorldHint is False


@pytest.mark.anyio
async def test_every_tool_is_documented(mcp_client):
    async with mcp_client as client:
        tools = await client.list_tools()
    for tool in tools:
        assert tool.description and len(tool.description) > 40


@pytest.mark.anyio
async def test_get_base_pay_known_value(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_base_pay", {"pay_grade": "E-5", "years_of_service": 4}
        )
    data = payload(result)
    assert data["monthly_rate"] == 3946.80
    assert data["effective_date"] == "2026-01-01"
    assert "dfas.mil" in data["source_url"]


@pytest.mark.anyio
async def test_get_base_pay_accepts_loose_grade_spelling(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_base_pay", {"pay_grade": "e5", "years_of_service": 4}
        )
    assert payload(result)["monthly_rate"] == 3946.80


@pytest.mark.anyio
async def test_get_base_pay_invalid_combination(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_base_pay", {"pay_grade": "E-8", "years_of_service": 2}
        )
    data = payload(result)
    assert data["monthly_rate"] is None
    assert data["rate_basis"] == "not_a_valid_combination"


@pytest.mark.anyio
async def test_get_base_pay_e1_footnote(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_base_pay",
            {"pay_grade": "E-1", "years_of_service": 0, "months_active_duty": 2},
        )
    assert payload(result)["monthly_rate"] == 2225.70


@pytest.mark.anyio
async def test_get_bah_off_cycle_rate_set(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_bah",
            {"zip_code": "79601", "pay_grade": "E-5", "has_dependents": True},
        )
    data = payload(result)
    assert data["mha_code"] == "TX270"
    assert data["rate_set"] == "2026-abilene-temp"
    assert data["taxable"] is False


@pytest.mark.anyio
async def test_get_bas_defaults_are_not_bas_ii(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool("get_bas", {"pay_grade_type": "enlisted"})
    assert payload(result)["monthly_rate"] == 476.95


@pytest.mark.anyio
async def test_estimate_total_compensation(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "estimate_total_compensation",
            {"pay_grade": "E-5", "years_of_service": 4,
             "zip_code": "92101", "has_dependents": True},
        )
    data = payload(result)
    assert data["complete"] is True
    assert data["monthly"]["taxable_total"] == 3946.80
    assert data["monthly"]["non_taxable_total"] > 0


@pytest.mark.anyio
async def test_lookup_failure_returns_structured_error_not_a_traceback(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_bah",
            {"zip_code": "99999", "pay_grade": "E-5", "has_dependents": False},
        )
    data = payload(result)
    assert data["error"] == "lookup_failed"
    assert "crosswalk" in data["message"]


@pytest.mark.anyio
async def test_bad_pay_grade_returns_structured_error(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool(
            "get_base_pay", {"pay_grade": "sergeant", "years_of_service": 4}
        )
    assert payload(result)["error"] == "lookup_failed"


@pytest.mark.anyio
async def test_database_status(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool("get_database_status", {})
    data = payload(result)
    assert data["base_pay_years"] == [2026]
    assert data["database_path"]


@pytest.mark.anyio
async def test_reconfigure_takes_effect_on_worker_threads(db_path, tmp_path):
    """FastMCP dispatches tools on worker threads. Clearing only the calling
    thread's connection left every other thread serving the old database."""
    from mcp_militarypay import db as db_module
    from mcp_militarypay import ingest

    other = tmp_path / "other.sqlite3"
    conn = db_module.open_for_ingest(other)
    ingest.ingest_bas(conn, html=(FIXTURES / "dfas_bas.html").read_text())
    conn.commit()
    conn.close()

    server.configure(db_path)
    try:
        async with Client(server.mcp) as client:
            first = payload(await client.call_tool("get_database_status", {}))
        assert first["base_pay_years"] == [2026]
        assert str(db_path) in first["database_path"]

        server.configure(other)
        async with Client(server.mcp) as client:
            second = payload(await client.call_tool("get_database_status", {}))
        # The other database has BAS only, so this proves the switch landed.
        assert second["base_pay_years"] == []
        assert str(other) in second["database_path"]
    finally:
        server.configure(None)


@pytest.mark.anyio
async def test_server_runs_over_a_real_stdio_transport(db_path):
    """Launch the server as a subprocess and speak MCP to it, the way a client
    does.

    Everything else exercises the tools in-process. This is the only check that
    the console entry point starts, negotiates, and answers over stdio - the
    path an actual MCP client uses and the one a packaging or import error
    would break without failing any other test.
    """
    import os
    import sys

    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "mcp_militarypay.server"],
        env=dict(os.environ, MILITARYPAY_DB=str(db_path)),
    )

    async with Client(transport) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {
            "get_base_pay", "get_bah", "find_housing_area", "get_bas",
            "estimate_total_compensation", "get_database_status",
        }

        result = await client.call_tool(
            "get_base_pay", {"pay_grade": "E-5", "years_of_service": 4}
        )
        assert payload(result)["monthly_rate"] == 3946.80

        result = await client.call_tool(
            "estimate_total_compensation",
            {"pay_grade": "E-5", "years_of_service": 4,
             "zip_code": "92101", "has_dependents": True},
        )
        data = payload(result)
        assert data["complete"] is True
        assert data["monthly"]["taxable_total"] == 3946.80

        # A failed lookup must come back as a structured result, not a crash
        # that takes the subprocess down.
        result = await client.call_tool(
            "get_bah",
            {"zip_code": "99999", "pay_grade": "E-5", "has_dependents": False},
        )
        assert payload(result)["error"] == "lookup_failed"

        # Still alive after the error.
        result = await client.call_tool("get_bas", {"pay_grade_type": "enlisted"})
        assert payload(result)["monthly_rate"] == 476.95


@pytest.mark.anyio
async def test_find_housing_area_then_get_bah(mcp_client):
    """The intended two-step: look the area up, then use a ZIP it returns —
    instead of supplying one from memory and silently hitting the wrong area."""
    async with mcp_client as client:
        found = payload(await client.call_tool(
            "find_housing_area", {"query": "Abilene"}))
        assert found["housing_areas"][0]["mha_code"] == "TX270"

        zip_code = found["housing_areas"][0]["example_zip_codes"][0]
        rate = payload(await client.call_tool(
            "get_bah",
            {"zip_code": zip_code, "pay_grade": "E-5", "has_dependents": True}))
    assert rate["mha_code"] == "TX270"


@pytest.mark.anyio
async def test_find_housing_area_reports_no_match_without_erroring(mcp_client):
    async with mcp_client as client:
        result = payload(await client.call_tool(
            "find_housing_area", {"query": "Atlantis"}))
    assert result["count"] == 0
    assert "error" not in result


@pytest.mark.anyio
async def test_zip_parameter_says_duty_station_not_residence(mcp_client):
    """The description is what stops a caller passing a home ZIP, so it has to
    say so where the caller actually reads it."""
    async with mcp_client as client:
        tools = {t.name: t for t in await client.list_tools()}

    for name in ("get_bah", "estimate_total_compensation"):
        described = tools[name].inputSchema["properties"]["zip_code"]["description"]
        assert "DUTY STATION" in described.upper()
        assert "home" in described.lower() or "residence" in described.lower()


@pytest.mark.anyio
async def test_bah_response_carries_the_duty_station_rule(mcp_client):
    async with mcp_client as client:
        result = payload(await client.call_tool(
            "get_bah",
            {"zip_code": "92101", "pay_grade": "E-5", "has_dependents": True}))
    assert any("permanent duty station" in note for note in result["notes"])
