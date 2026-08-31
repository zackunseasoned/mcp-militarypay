"""Parser for the DTMO "All BAH Rates" ASCII bundle.

DTMO publishes no schema for these files. The layout below is derived from the
working reference consumer mpyne-navy/bah-rate-map (MIT, CDR Mike Pyne USN),
whose index.html documents a sample row and slices it E1-E9, W1-W5, O1E-O3E,
O1-O10.

The bundle (BAH-ASCII-<year>.zip) carries more than the three files this
project needs. The 2026 bundle holds thirteen members:

  sorted_zipmha<yy>.txt  space-delimited "ZIP MHA" crosswalk (~41k US ZIPs)
  bahw<yy>.txt           rates WITH dependents          <- used
  bahwo<yy>.txt          rates WITHOUT dependents       <- used
  mhanames<yy>.txt       MHA code -> locality name      <- used
  ASCII-FILE-FORMAT.pdf  DTMO's own layout documentation
  *.dat, *_<yy>.dat      the same data in other encodings
  "* - old.txt/.dat"     the PREVIOUS publication, kept alongside the current

Those "- old" members are a live hazard: they end in .txt and share the
bahw/bahwo prefixes, so a filename-prefix fallback can silently ingest last
publication's rates. They are excluded explicitly.

The rate files are headerless CSV - not fixed-width - with 28 fields:
field 0 is the MHA code, fields 1..27 are monthly rates in BAH_RATE_COLUMNS
order.

Note the BAH grade set is not the basic pay grade set: it includes O-1E/O-2E/
O-3E, and the DTMO lookup form collapses O-7 and above into one "O-7/O-7+"
bucket (in the ASCII files, O-7..O-10 simply carry the same value).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field

from ..sources import BAH_RATE_COLUMNS, BAH_ROW_FIELD_COUNT, bah_inner_filenames


class ParseError(ValueError):
    """The BAH source files did not have the expected shape."""


@dataclass
class BahRateFile:
    """Rates parsed from one bahw/bahwo file."""

    with_dependents: bool
    source_file: str
    # (mha_code, pay_grade) -> monthly rate
    rates: dict[tuple[str, str], float] = field(default_factory=dict)
    raw_lines: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mha_codes(self) -> set[str]:
        return {mha for mha, _ in self.rates}


@dataclass
class BahBundle:
    """Everything read out of one BAH-ASCII-<year>.zip."""

    year: int
    zip_to_mha: dict[str, str] = field(default_factory=dict)
    mha_names: dict[str, str] = field(default_factory=dict)
    with_dependents: BahRateFile | None = None
    without_dependents: BahRateFile | None = None
    warnings: list[str] = field(default_factory=list)


_MHA_RE = re.compile(r"^[A-Z]{2}\d{3}$")

# Members like "bahw26 - old.txt" are the previous publication. They must never
# be picked up by a prefix fallback.
_SUPERSEDED_RE = re.compile(r"\bold\b|\bprev(?:ious)?\b|\bbackup\b", re.IGNORECASE)


def parse_mha_names(text: str) -> dict[str, str]:
    """Parse mhanames<yy>.txt: MHA code -> locality name.

    DTMO's exact delimiter here is not documented, so this accepts the three
    plausible forms without needing to know which is in use: the code followed
    by whitespace, by a comma, or run directly against the name. Locality names
    themselves contain commas ("ANCHORAGE, AK"), so only a leading field that is
    exactly an MHA code is treated as the key.
    """
    names: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if not stripped:
            continue
        code = stripped[:5].upper()
        if not _MHA_RE.match(code):
            continue
        name = stripped[5:].strip().lstrip(",").strip().strip('"').strip()
        if name:
            names[code] = " ".join(name.split())
    return names


def parse_zip_mha(text: str) -> dict[str, str]:
    """Parse sorted_zipmha<yy>.txt: whitespace-delimited 'ZIP MHA' per line.

    ZIP codes are zero-padded to five characters; leading zeros matter and are
    easy to lose (00501 is a real ZIP).
    """
    mapping: dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            raise ParseError(
                f"ZIP/MHA crosswalk line {line_no} is not 'ZIP MHA': {stripped!r}"
            )
        zip_code, mha = parts[0].strip(), parts[1].strip().upper()
        if not zip_code.isdigit():
            raise ParseError(
                f"ZIP/MHA crosswalk line {line_no} has a non-numeric ZIP: {stripped!r}"
            )
        mapping[zip_code.zfill(5)] = mha
    if not mapping:
        raise ParseError("ZIP/MHA crosswalk was empty")
    return mapping


def parse_rate_file(
    text: str, *, with_dependents: bool, source_file: str, keep_raw: bool = True
) -> BahRateFile:
    """Parse a bahw<yy>.txt / bahwo<yy>.txt rate file.

    Fails loudly on an unexpected field count rather than silently mapping rates
    onto the wrong pay grades, which is exactly how a layout change would
    otherwise corrupt the table.
    """
    result = BahRateFile(with_dependents=with_dependents, source_file=source_file)
    reader = csv.reader(io.StringIO(text))

    for line_no, row in enumerate(reader, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue

        raw = ",".join(row)
        if keep_raw:
            result.raw_lines.append((line_no, raw))

        if len(row) != BAH_ROW_FIELD_COUNT:
            raise ParseError(
                f"{source_file} line {line_no}: expected {BAH_ROW_FIELD_COUNT} "
                f"fields (MHA + {len(BAH_RATE_COLUMNS)} pay grades), got "
                f"{len(row)}. The DTMO file layout has probably changed - "
                f"compare against raw_bah_lines from the previous year before "
                f"trusting any ingest."
            )

        mha = row[0].strip().upper()
        if not _MHA_RE.match(mha):
            raise ParseError(
                f"{source_file} line {line_no}: {mha!r} is not an MHA code"
            )

        for column, cell in zip(BAH_RATE_COLUMNS, row[1:]):
            cell = cell.strip()
            if not cell:
                result.warnings.append(
                    f"{source_file} line {line_no}: empty rate for {mha} {column}"
                )
                continue
            try:
                result.rates[(mha, column)] = float(cell.replace(",", "").lstrip("$"))
            except ValueError as exc:
                raise ParseError(
                    f"{source_file} line {line_no}: rate for {mha} {column} "
                    f"is not a number: {cell!r}"
                ) from exc

    if not result.rates:
        raise ParseError(f"{source_file} contained no rate rows")
    return result


def _read_member(archive: zipfile.ZipFile, expected_name: str, prefix: str) -> tuple[str, str]:
    """Read one member, falling back to a prefix match.

    The two-digit-year filenames are only confirmed for 2023, so an exact-name
    miss falls back to matching on the filename prefix instead of failing.
    """
    names = archive.namelist()
    by_base = {name.rsplit("/", 1)[-1].lower(): name for name in names}

    chosen = by_base.get(expected_name.lower())
    if chosen is None:
        candidates = sorted(
            actual for base, actual in by_base.items()
            if base.startswith(prefix.lower())
            and base.endswith(".txt")
            and not _SUPERSEDED_RE.search(base)
        )
        if not candidates:
            raise ParseError(
                f"no member named {expected_name!r} (or starting {prefix!r}) in "
                f"the BAH bundle; it contains: {sorted(by_base)}"
            )
        chosen = candidates[0]

    data = archive.read(chosen)
    return chosen.rsplit("/", 1)[-1], data.decode("utf-8", errors="replace")


def parse_bah_bundle(zip_bytes: bytes, year: int, *, keep_raw: bool = True) -> BahBundle:
    """Parse a whole BAH-ASCII-<year>.zip into its three components."""
    expected = bah_inner_filenames(year)
    bundle = BahBundle(year=year)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        members = [n.rsplit("/", 1)[-1] for n in archive.namelist() if not n.endswith("/")]
        superseded = sorted(m for m in members if _SUPERSEDED_RE.search(m))
        if superseded:
            bundle.warnings.append(
                f"the {year} bundle also ships the previous publication "
                f"({superseded}); these are ignored"
            )

        zip_name, zip_text = _read_member(archive, expected["zip_mha"], "sorted_zipmha")
        bundle.zip_to_mha = parse_zip_mha(zip_text)

        # MHA names are a nicety rather than a requirement: a bundle without
        # them still yields correct rates, just without locality labels.
        try:
            _, names_text = _read_member(archive, expected["mha_names"], "mhanames")
            bundle.mha_names = parse_mha_names(names_text)
            if not bundle.mha_names:
                bundle.warnings.append(
                    f"{expected['mha_names']} was present but no MHA names could "
                    f"be parsed from it"
                )
        except ParseError:
            bundle.warnings.append(
                f"no MHA name file in the {year} bundle; rates will have no "
                f"locality names"
            )

        wd_name, wd_text = _read_member(archive, expected["with_dependents"], "bahw")
        wo_name, wo_text = _read_member(archive, expected["without_dependents"], "bahwo")

        # 'bahw' is a prefix of 'bahwo', so a fallback prefix match could pick
        # the same file twice. Detect that rather than ingesting it silently.
        if wd_name.lower() == wo_name.lower():
            raise ParseError(
                f"with- and without-dependents resolved to the same file "
                f"({wd_name}); cannot tell the two rate sets apart"
            )
        if wd_name.lower().startswith("bahwo"):
            raise ParseError(
                f"expected the with-dependents file to be bahw<yy>.txt, got {wd_name}"
            )

        bundle.with_dependents = parse_rate_file(
            wd_text, with_dependents=True, source_file=wd_name, keep_raw=keep_raw
        )
        bundle.without_dependents = parse_rate_file(
            wo_text, with_dependents=False, source_file=wo_name, keep_raw=keep_raw
        )

    with_mhas = bundle.with_dependents.mha_codes
    without_mhas = bundle.without_dependents.mha_codes
    if with_mhas != without_mhas:
        only_with = sorted(with_mhas - without_mhas)[:5]
        only_without = sorted(without_mhas - with_mhas)[:5]
        bundle.warnings.append(
            f"MHA sets differ between the two rate files: "
            f"{len(only_with)} only-with-dependents (e.g. {only_with}), "
            f"{len(only_without)} only-without (e.g. {only_without})"
        )

    unmapped = {mha for mha in bundle.zip_to_mha.values()} - with_mhas
    if unmapped:
        bundle.warnings.append(
            f"{len(unmapped)} MHAs in the ZIP crosswalk have no rates "
            f"(e.g. {sorted(unmapped)[:5]})"
        )
    return bundle
