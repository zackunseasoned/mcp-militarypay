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
    return builder.build_manifest(builder.read_version(), builder.PORTABLE_COMMAND)


@pytest.fixture(scope="module")
def pinned_manifest(builder):
    return builder.build_manifest(
        builder.read_version(), str(builder.base_interpreter())
    )


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


class TestPinnedInterpreter:
    """A host may launch `python3` rather than the `python` a manifest asks
    for. On a machine with several Pythons that can be a different version than
    the vendored wheels were built for, and pydantic_core then fails to import
    its compiled extension - or `python3` is not on PATH at all. The default
    bundle names an interpreter outright."""

    def test_default_command_is_an_absolute_path(self, pinned_manifest):
        command = pinned_manifest["server"]["mcp_config"]["command"]
        assert Path(command).is_absolute()

    def test_the_pinned_interpreter_exists(self, builder):
        assert builder.base_interpreter().is_file()

    def test_it_is_not_the_virtualenv(self, builder):
        """A venv path would tie the bundle to a checkout that can move."""
        import sys

        pinned = builder.base_interpreter().resolve()
        assert sys.base_prefix in (str(pinned), *(str(p) for p in pinned.parents))

    def test_the_pinned_interpreter_can_load_the_vendored_abi(self, builder):
        """Same ABI as the interpreter that vendors the wheels, which is the
        whole point of pinning to base_prefix rather than to anything else."""
        import subprocess
        import sys

        pinned = builder.base_interpreter()
        result = subprocess.run(
            [str(pinned), "-c",
             "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == f"{sys.version_info.major}.{sys.version_info.minor}"

    def test_portable_command_is_still_available(self, manifest, builder):
        assert manifest["server"]["mcp_config"]["command"] == builder.PORTABLE_COMMAND


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """A packed bundle, staged without vendoring - pip is far too slow here.

    Module-scoped and defined outside the class: a class-scoped fixture written
    as an instance method is deprecated and becomes an error in pytest 10.
    """
    import json
    import zipfile

    builder = _load_builder()
    staging = tmp_path_factory.mktemp("staging")
    (staging / "server").mkdir()
    (staging / "lib" / "somepkg").mkdir(parents=True)
    (staging / "manifest.json").write_text(
        json.dumps(builder.build_manifest("0.1.0", "python"))
    )
    (staging / "server" / "main.py").write_text("pass\n")
    (staging / "lib" / "somepkg" / "__init__.py").write_text("")

    bundle = tmp_path_factory.mktemp("dist") / "test.mcpb"
    manifest_path = staging / "manifest.json"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(manifest_path, "manifest.json")
        for path in sorted(staging.rglob("*")):
            if path != manifest_path and path.is_file():
                zf.write(path, path.relative_to(staging).as_posix())

    with zipfile.ZipFile(bundle) as opened:
        yield opened


class TestBuiltArchive:
    """A bundle a host rejects as 'no manifest' is indistinguishable from one
    that is merely wrong, so the packer reads its own output back."""

    def test_manifest_is_the_first_entry(self, archive):
        """A reader scanning the start of the archive should not have to walk
        several thousand vendored files to find it."""
        assert archive.namelist()[0] == "manifest.json"

    def test_manifest_is_at_the_archive_root(self, archive):
        assert "manifest.json" in archive.namelist()

    def test_entry_names_use_forward_slashes(self, archive):
        """A zip entry name is not an OS path; a strict reader will not find
        'server\\main.py'."""
        assert not any("\\" in name for name in archive.namelist())
        assert "server/main.py" in archive.namelist()

    def test_the_declared_entry_point_is_present(self, archive):
        import json

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["server"]["entry_point"] in archive.namelist()
