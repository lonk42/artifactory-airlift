"""Tests for the k8s-quantity byte-size parser used by Settings."""
from __future__ import annotations

import pytest

from artifactory_airlift.config import Settings, _parse_byte_size


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (1024, 1024),
        ("0", 0),
        ("1024", 1024),
        ("1B", 1),
        ("8Gi", 8 * 1024**3),
        ("8GiB", 8 * 1024**3),
        ("512Mi", 512 * 1024**2),
        ("1Ti", 1024**4),
        ("1G", 1_000_000_000),
        ("1GB", 1_000_000_000),
        ("250M", 250_000_000),
        ("  4Gi  ", 4 * 1024**3),  # surrounding whitespace tolerated
        ("4 Gi", 4 * 1024**3),    # internal whitespace tolerated
    ],
)
def test_parse_byte_size_accepts_quantities(raw, expected: int) -> None:
    assert _parse_byte_size(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "8gi",      # k8s convention: Gi (binary) and G (decimal); gi is unknown
        "ten",
        "1.5Gi",   # fractional not supported on purpose; matches PVC rounding
        "Gi",
        "",
        "-1Gi",
    ],
)
def test_parse_byte_size_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_byte_size(bad)


def test_parse_byte_size_rejects_bool() -> None:
    with pytest.raises(TypeError):
        _parse_byte_size(True)


def test_settings_field_validator_coerces_quantity_strings() -> None:
    # Both yaml-loaded strings and env-var strings flow through the
    # before-validator and land as ints in the resolved Settings object.
    s = Settings(max_archive_bytes="4Gi", spool_min_free_bytes="512Mi")
    assert s.max_archive_bytes == 4 * 1024**3
    assert s.spool_min_free_bytes == 512 * 1024**2


def test_settings_field_validator_accepts_plain_int() -> None:
    s = Settings(max_archive_bytes=12345, spool_min_free_bytes=0)
    assert s.max_archive_bytes == 12345
    assert s.spool_min_free_bytes == 0


def test_settings_field_validator_rejects_invalid_quantity() -> None:
    with pytest.raises(Exception):
        Settings(max_archive_bytes="banana")
