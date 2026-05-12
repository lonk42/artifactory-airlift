import pytest
from pydantic import ValidationError

from artifactory_airlift.config import Settings


def test_defaults_three_days() -> None:
    s = Settings()
    assert s.snapshot_retention_hours == 0
    assert s.snapshot_retention_days == 3
    assert s.snapshot_retention_months == 0


def test_all_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            snapshot_retention_hours=0,
            snapshot_retention_days=0,
            snapshot_retention_months=0,
        )


def test_partial_zero_ok() -> None:
    s = Settings(
        snapshot_retention_hours=10,
        snapshot_retention_days=0,
        snapshot_retention_months=0,
    )
    assert s.snapshot_retention_hours == 10
    assert s.snapshot_retention_days == 0
    assert s.snapshot_retention_months == 0


def test_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(snapshot_retention_days=-1)
