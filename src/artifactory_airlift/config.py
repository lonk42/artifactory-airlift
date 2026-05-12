from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field
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

    artifactory_uid: int = 1030
    artifactory_gid: int = 1030

    log_level: str = "INFO"


def load(config_path: Path | None = None) -> Settings:
    overrides: dict[str, object] = {}
    cfg = config_path or Path("/etc/airlift/config.yaml")
    if cfg.is_file():
        data = yaml.safe_load(cfg.read_text()) or {}
        if isinstance(data, dict):
            overrides = {k: v for k, v in data.items() if v is not None}
    return Settings(**overrides)
