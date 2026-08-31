import pytest

from mcp_militarypay.sources import (
    BAH_RATE_COLUMNS,
    BAH_ROW_FIELD_COUNT,
    bah_inner_filenames,
    category_for_grade,
    normalize_pay_grade,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("e5", "E-5"), ("E5", "E-5"), ("E-5", "E-5"), (" e-5 ", "E-5"),
        ("o3e", "O-3E"), ("O3E", "O-3E"), ("O-3E", "O-3E"),
        ("w4", "W-4"), ("O10", "O-10"), ("o-10", "O-10"),
    ],
)
def test_normalize_pay_grade(raw, expected):
    assert normalize_pay_grade(raw) == expected


@pytest.mark.parametrize("raw", ["", "X-5", "E-0", "E-10", "O-11", "W-6", "sergeant", "5", None])
def test_normalize_pay_grade_rejects_junk(raw):
    with pytest.raises(ValueError):
        normalize_pay_grade(raw)


@pytest.mark.parametrize(
    "grade,category",
    [
        ("E-1", "enlisted"), ("E-9", "enlisted"),
        ("O-1", "officer"), ("O-10", "officer"),
        ("O-1E", "officer_prior_enlisted"), ("O-3E", "officer_prior_enlisted"),
        ("W-1", "warrant"), ("W-5", "warrant"),
    ],
)
def test_category_for_grade(grade, category):
    assert category_for_grade(grade) == category


def test_bah_column_layout_matches_reference_implementation():
    """28 fields: MHA + E1-E9, W1-W5, O1E-O3E, O1-O10.

    Layout derived from mpyne-navy/bah-rate-map, which documents a sample row
    and slices it exactly this way.
    """
    assert BAH_ROW_FIELD_COUNT == 28
    assert len(BAH_RATE_COLUMNS) == 27
    assert BAH_RATE_COLUMNS[:9] == tuple(f"E-{i}" for i in range(1, 10))
    assert BAH_RATE_COLUMNS[9:14] == tuple(f"W-{i}" for i in range(1, 6))
    assert BAH_RATE_COLUMNS[14:17] == ("O-1E", "O-2E", "O-3E")
    assert BAH_RATE_COLUMNS[17:] == tuple(f"O-{i}" for i in range(1, 11))


def test_bah_inner_filenames():
    assert bah_inner_filenames(2026) == {
        "zip_mha": "sorted_zipmha26.txt",
        "with_dependents": "bahw26.txt",
        "without_dependents": "bahwo26.txt",
    }
    assert bah_inner_filenames(2023)["with_dependents"] == "bahw23.txt"
