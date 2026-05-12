from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["sender", "receiver"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIRLIFT_",
        env_file=None,
        extra="ignore",
    )

    mode: Mode = "sender"
    instance_name: str = "unknown"

    artifactory_url: str = "http://localhost:8081/artifactory"
    artifactory_token: str = ""
    artifactory_username: str = ""
    artifactory_password: str = ""

    filestore_root: Path = Path("/var/opt/jfrog/artifactory/data/artifactory/filestore")
    artifactory_tmp: Path = Path("/var/opt/jfrog/artifactory/data/artifactory/tmp")

    state_dir: Path = Path("/var/airlift/state")
    spool_dir: Path = Path("/var/airlift/spool")

    cycle_seconds: int = Field(default=300, ge=10)
    history_keep: int = Field(default=24, ge=1)
    done_keep_hours: int = Field(default=72, ge=1)

    # GFS retention for sender-side snapshot baselines (state/snapshots/*.jsonl).
    # Each tier independently keeps the newest snapshot in each non-empty
    # bucket within its wall-clock window from now. Final keep set is the
    # union across tiers; a single snapshot can satisfy multiple tiers.
    # Months are real calendar months, not 30-day windows.
    snapshot_retention_hours: int = Field(default=0, ge=0)
    snapshot_retention_days: int = Field(default=3, ge=0)
    snapshot_retention_months: int = Field(default=0, ge=0)

    artifactory_uid: int = 1030
    artifactory_gid: int = 1030

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_snapshot_retention(self) -> "Settings":
        total = (
            self.snapshot_retention_hours
            + self.snapshot_retention_days
            + self.snapshot_retention_months
        )
        if total <= 0:
            raise ValueError(
                "at least one of snapshot_retention_hours, "
                "snapshot_retention_days, snapshot_retention_months must be > 0"
            )
        return self


def load(config_path: Path | None = None) -> Settings:
    overrides: dict[str, object] = {}
    cfg = config_path or Path("/etc/airlift/config.yaml")
    if cfg.is_file():
        data = yaml.safe_load(cfg.read_text()) or {}
        if isinstance(data, dict):
            overrides = {k: v for k, v in data.items() if v is not None}
    return Settings(**overrides)
