"""Tests for the .mcpb bundle manifest.

A bundle is how the server installs into Claude for macOS and Windows. The
Store-packaged build virtualises %APPDATA%, so hand-editing
claude_desktop_config.json can land on a file the app never reads; installing a
bundle goes through the app's own flow instead.

These cover the manifest, not the zip: building one runs pip, which is too slow
for the suite. The zip layout is exercised by running the build script.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_mcpb", ROOT / "packaging" / "build_mcpb.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_builder()


@pytest.fixture(scope="module")
def manifest(builder):
    return builder.build_manifest(builder.read_version())


def test_version_matches_the_package(builder):
    from mcp_militarypay import __version__

    assert builder.read_version() == __version__


@pytest.mark.parametrize(
    "field",
    ["manifest_version", "name", "version", "description", "author", "server"],
)
def test_required_fields_are_present(manifest, field):
    """Required by the MCPB manifest spec."""
    assert manifest[field]


def test_author_has_a_name(manifest):
    assert manifest["author"]["name"]


def test_server_block_is_a_python_bundle(manifest):
    server = manifest["server"]
    assert server["type"] == "python"
    assert server["entry_point"] == "server/main.py"
    config = server["mcp_config"]
    assert config["command"] == "python"
    assert config["args"] == ["${__dirname}/server/main.py"]


def test_bundled_lib_is_on_the_path(manifest):
    """Nothing is pip-installed at install time, so the vendored lib/ has to be
    importable or the server cannot start."""
    assert manifest["server"]["mcp_config"]["env"]["PYTHONPATH"] == "${__dirname}/lib"


def test_database_path_is_user_configured(manifest):
    """The bundle ships no rate data; the user points it at a built database."""
    config = manifest["user_config"]["database_path"]
    assert config["type"] == "file"
    assert config["required"] is True
    env = manifest["server"]["mcp_config"]["env"]
    assert env["MILITARYPAY_DB"] == "${user_config.database_path}"


@pytest.mark.anyio
async def test_declared_tools_match_the_server(manifest):
    """The manifest lists tools for the install dialog. If a tool is added or
    renamed without updating it, the dialog misdescribes the extension."""
    from mcp_militarypay.server import mcp

    declared = {tool["name"] for tool in manifest["tools"]}
    actual = {tool.name for tool in await mcp.list_tools()}
    assert declared == actual


def test_every_declared_tool_has_a_description(manifest):
    for tool in manifest["tools"]:
        assert tool["description"]


def test_only_server_dependencies_are_vendored(builder):
    """The ingest's dependencies would be dead weight: a bundle serves from an
    already-built database and never fetches."""
    vendored = " ".join(builder.SERVER_REQUIREMENTS).lower()
    assert "fastmcp" in vendored
    for ingest_only in ("httpx", "beautifulsoup4", "lxml", "openpyxl"):
        assert ingest_only not in vendored


def test_disclaimer_is_carried_into_the_listing(manifest):
    """Someone reading the install dialog should see it is unofficial."""
    text = manifest["long_description"].lower()
    assert "unofficial" in text
    assert "dfas" in text
