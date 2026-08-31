"""Build a .mcpb bundle so the server installs with one click.

An MCP Bundle is a zip holding the server, its dependencies and a manifest.json
describing them. Claude for macOS and Windows installs one directly, which
matters for the Store-packaged build: it virtualises %APPDATA%, so hand-editing
claude_desktop_config.json can land on a file the app never reads. Installing a
bundle goes through the app's own flow instead.

Run this with the interpreter you want the bundle built for - dependencies are
vendored from the running environment, so building on Windows produces the
Windows wheels:

    python packaging/build_mcpb.py

Output: dist/militarypay-<version>.mcpb

The manifest pins an absolute interpreter path by default. A host may launch
`python3` rather than the `python` a manifest asks for, and on a machine with
several Pythons that can be a different version than the one whose ABI the
vendored wheels were built against - pydantic_core then fails to import its
compiled extension, or `python3` is not resolvable at all. Pinning removes the
guess. Use --command to override.

Only the server's own dependency (fastmcp) is vendored. The ingest needs
httpx, beautifulsoup4, lxml and openpyxl, but a bundle serves rates from an
already-built database and never fetches, so those would be dead weight.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mcp_militarypay"

# Vendored into lib/. Deliberately not the ingest-only dependencies.
SERVER_REQUIREMENTS = ["fastmcp>=2.0"]

# What --command accepts for a host-resolved (portable, fragile) bundle.
PORTABLE_COMMAND = "python"

ENTRY_POINT = '''"""Bundle entry point: run the militarypay MCP server over stdio."""

import sys
from pathlib import Path

# PYTHONPATH is set by the manifest, but a bundle should not depend on the host
# honouring it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from mcp_militarypay.server import main  # noqa: E402

if __name__ == "__main__":
    main()
'''


def base_interpreter() -> Path:
    """The interpreter the vendored wheels are ABI-compatible with.

    sys.executable inside a virtualenv points at the venv, which ties the
    bundle to a checkout that may move. sys.base_prefix is the installation the
    venv was created from: same ABI, stable location.
    """
    base = Path(sys.base_prefix)
    candidates = (
        [base / "python.exe", base / "Scripts" / "python.exe"]
        if sys.platform == "win32"
        else [base / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}",
              base / "bin" / "python3", base / "bin" / "python"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def read_version() -> str:
    text = (SRC / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("could not read __version__")


def build_manifest(version: str, command: str) -> dict:
    return {
        "manifest_version": "0.3",
        "name": "militarypay",
        "display_name": "US Military Pay Rates",
        "version": version,
        "description": (
            "Look up published US military pay rates: basic pay and BAS from "
            "DFAS, and BAH from the DoD Defense Travel Management Office."
        ),
        "long_description": (
            "Answers questions like \"what does an E-5 with 6 years make\" or "
            "\"what is BAH for a sergeant with dependents in San Diego\", with "
            "a taxable/non-taxable breakdown.\n\n"
            "Reads a local SQLite database built by the project's ingest from "
            "the published DFAS and DTMO rate tables; it makes no network "
            "requests of its own. Every answer carries the effective date and "
            "source URL of the figures used.\n\n"
            "Unofficial. It does not read anyone's Leave and Earnings "
            "Statement and is not an authoritative source of pay. For actual "
            "pay questions contact DFAS at 1-888-332-7411 or use myPay."
        ),
        "author": {"name": "zackunseasoned"},
        "repository": {
            "type": "git",
            "url": "https://github.com/zackunseasoned/mcp-militarypay",
        },
        "server": {
            "type": "python",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": command,
                "args": ["${__dirname}/server/main.py"],
                "env": {
                    "PYTHONPATH": "${__dirname}/lib",
                    "MILITARYPAY_DB": "${user_config.database_path}",
                },
            },
        },
        "tools": [
            {"name": "get_base_pay",
             "description": "Monthly basic pay for a pay grade and years of service"},
            {"name": "get_bah",
             "description": "Monthly Basic Allowance for Housing for a ZIP code and pay grade"},
            {"name": "get_bas",
             "description": "Monthly Basic Allowance for Subsistence"},
            {"name": "estimate_total_compensation",
             "description": "Base pay + BAH + BAS with a taxable/non-taxable split"},
            {"name": "get_database_status",
             "description": "What rate data is loaded and when each source was fetched"},
        ],
        "user_config": {
            "database_path": {
                "type": "file",
                "title": "Rate database",
                "description": (
                    "The militarypay.sqlite3 file built by "
                    "`militarypay-ingest ingest --all`. Usually data/"
                    "militarypay.sqlite3 inside the checkout."
                ),
                "required": True,
            }
        },
        "compatibility": {
            "platforms": ["win32", "darwin", "linux"],
            "runtimes": {"python": ">=3.11"},
        },
        "keywords": ["military", "pay", "BAH", "BAS", "DFAS", "DTMO"],
        "license": "MIT",
    }


def vendor_dependencies(lib: Path) -> None:
    """Install the server's dependencies and the package itself into lib/."""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--target", str(lib), *SERVER_REQUIREMENTS],
        check=True,
    )
    # The package itself, copied rather than pip-installed so the bundle does
    # not carry an editable-install path pointing back at this checkout.
    shutil.copytree(SRC, lib / "mcp_militarypay", dirs_exist_ok=True)
    for cache in (lib / "mcp_militarypay").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    if not (lib / "mcp_militarypay" / "schema.sql").is_file():
        raise SystemExit("schema.sql missing from the bundled package")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    parser.add_argument(
        "--command", default=None,
        help="Interpreter the manifest tells the host to run. Defaults to an "
             "absolute path to the interpreter whose ABI the vendored wheels "
             "match. Pass 'python' for a portable-but-fragile bundle.")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Leave the unzipped staging directory in place.")
    args = parser.parse_args()

    version = read_version()
    command = args.command or str(base_interpreter())
    out_dir = Path(args.out_dir)
    staging = out_dir / "mcpb-staging"
    bundle = out_dir / f"militarypay-{version}.mcpb"

    if staging.exists():
        shutil.rmtree(staging)
    (staging / "server").mkdir(parents=True)
    lib = staging / "lib"
    lib.mkdir()

    (staging / "manifest.json").write_text(
        json.dumps(build_manifest(version, command), indent=2) + "\n",
        encoding="utf-8")
    (staging / "server" / "main.py").write_text(ENTRY_POINT, encoding="utf-8")
    (staging / "requirements.txt").write_text(
        "\n".join(SERVER_REQUIREMENTS) + "\n", encoding="utf-8")

    print(f"vendoring dependencies with {sys.executable} ...")
    vendor_dependencies(lib)

    # Verify before packing, and before the staging tree is removed: this is
    # the check that would have caught the wheels being built for one Python
    # and the manifest naming another.
    if command not in ("python", "python3"):
        check = subprocess.run(
            [command, "-c", "import pydantic_core._pydantic_core"],
            env={**os.environ, "PYTHONPATH": str(lib)},
            capture_output=True, text=True,
        )
        if check.returncode != 0:
            print(
                f"\nERROR: {command} cannot import the vendored wheels.\n"
                f"They were built by {sys.executable}, whose ABI it does not "
                f"share.\n{check.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        print(f"verified: {command} loads the vendored wheels")

    if bundle.exists():
        bundle.unlink()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(staging))

    if not args.keep_staging:
        shutil.rmtree(staging)

    size_mb = bundle.stat().st_size / (1024 * 1024)
    print(f"\nbuilt {bundle}  ({size_mb:.1f} MB)")
    print(f"manifest command: {command}")
    print("Install it in Claude: Settings -> Extensions -> Install Extension, "
          "or open the file directly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
