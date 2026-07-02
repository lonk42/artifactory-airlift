import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Mode = Literal["sender", "receiver"]


# Byte-size suffixes matching the Kubernetes "quantity" convention used by
# PVC sizes etc. IEC binary suffixes (Ki/Mi/Gi/Ti) and decimal SI suffixes
# (K/M/G/T) are both accepted; an optional trailing "B" is allowed for
# readability ("8GiB" is the same as "8Gi"). Bare numbers are bytes.
_BYTE_UNITS = {
    "": 1, "B": 1,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
}
_BYTE_SIZE_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")


def _parse_byte_size(v: int | str) -> int:
    """Coerce a k8s-style quantity string ("8Gi", "512Mi", "1G", "0") to an int.

    Integer inputs pass through unchanged. Suffix matching is case-sensitive
    on the unit prefix (k8s convention: Ki != ki) but the trailing "B" can
    be any case. Fractional quantities are not supported on purpose; PVC
    sizing rounds the same way.
    """
    if isinstance(v, bool):  # bool is an int subclass; reject explicitly
        raise TypeError(f"expected int or quantity string, got bool: {v!r}")
    if isinstance(v, int):
        return v
    if not isinstance(v, str):
        raise TypeError(f"expected int or quantity string, got {type(v).__name__}: {v!r}")
    m = _BYTE_SIZE_RE.match(v)
    if not m:
        raise ValueError(f"invalid byte-size quantity: {v!r}")
    num, unit = m.group(1), m.group(2)
    # Normalise the trailing B (e.g. "8GIB" -> "8GiB" key form).
    if unit and unit.endswith(("B", "b")) and len(unit) > 1:
        unit = unit[:-1] + "B"
    mult = _BYTE_UNITS.get(unit)
    if mult is None:
        raise ValueError(f"unknown byte-size unit: {unit!r} in {v!r}")
    return int(num) * mult


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

    # Path to a PEM CA bundle file used to verify the Artifactory TLS
    # certificate. Empty (default) uses the certifi CA store bundled with
    # httpx. Set this when Artifactory presents a certificate from a
    # private/self-signed CA; httpx does not read SSL_CERT_FILE/SSL_CERT_DIR,
    # so the CA must be named here explicitly. Must be a single concatenated
    # PEM file, not a directory (httpx/ssl wants a bundle file, not a CApath).
    artifactory_ca_cert: str = ""

    filestore_root: Path = Path("/var/opt/jfrog/artifactory/data/artifactory/filestore")
    artifactory_tmp: Path = Path("/var/opt/jfrog/artifactory/data/artifactory/tmp")

    state_dir: Path = Path("/var/airlift/state")
    spool_dir: Path = Path("/var/airlift/spool")

    cycle_seconds: int = Field(default=300, ge=10)
    history_keep: int = Field(default=24, ge=1)
    done_keep_hours: int = Field(default=72, ge=1)

    # Sender chunking + spool backpressure. A single cycle's add-entries are
    # split into N archives of cumulative raw blob bytes <= max_archive_bytes.
    # Most package formats are already-compressed, so raw ~= compressed for
    # threshold purposes. Set to 0 to disable chunking (single archive per
    # cycle, the pre-0.7 behaviour). Before each chunk build the sender
    # requires shutil.disk_usage(spool_dir).free to exceed
    # spool_min_free_bytes + the projected chunk size; otherwise the cycle is
    # aborted, partial chunks for the parent are cleaned up, and the cursor
    # is left untouched so the next loop tick retries from the same baseline.
    #
    # Both fields accept either an int (bytes) or a k8s-style quantity
    # string ("8Gi", "512Mi", "1G", "0"); see _parse_byte_size.
    max_archive_bytes: int = Field(default=8 * 1024**3, ge=0)
    spool_min_free_bytes: int = Field(default=2 * 1024**3, ge=0)

    @field_validator("max_archive_bytes", "spool_min_free_bytes", mode="before")
    @classmethod
    def _coerce_byte_size(cls, v):
        return _parse_byte_size(v)

    # GFS retention for sender-side snapshot baselines (state/snapshots/*.jsonl).
    # Each tier independently keeps the newest snapshot in each non-empty
    # bucket within its wall-clock window from now. Final keep set is the
    # union across tiers; a single snapshot can satisfy multiple tiers.
    # Months are real calendar months, not 30-day windows.
    snapshot_retention_hours: int = Field(default=0, ge=0)
    snapshot_retention_days: int = Field(default=3, ge=0)
    snapshot_retention_months: int = Field(default=0, ge=0)

    propagate_deletes: bool = True

    # Repos to drop before they enter the snapshot. The system export
    # writes every repo on the source, including JFrog-internal ones that
    # either fail to import on the destination or are platform-managed
    # and should be left alone. Two filters apply in parallel:
    #   - excluded_repos: literal repo-key match, for system repos whose
    #     packageType is Generic (e.g. jfrog-usage-logs) and so cannot be
    #     caught structurally.
    #   - excluded_package_types: packageType match resolved against
    #     /api/repositories at cycle time; catches any BuildInfo repo
    #     regardless of name (artifactory-build-info, airlift-build-info,
    #     team-foo-build-info, ...).
    # Operators can extend either via env (comma-separated):
    #   AIRLIFT_EXCLUDED_REPOS=foo,bar
    #   AIRLIFT_EXCLUDED_PACKAGE_TYPES=BuildInfo,...
    excluded_repos: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "artifactory-build-info",
            "jfrog-usage-logs",
            "auto-trashcan",
            "artifactory-pipe-info",
            "artifactory-edge-uploads",
            "jfrog-support-bundle",
            "release-bundles",
            "release-bundles-v2",
        ]
    )
    excluded_package_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BuildInfo"]
    )

    # Optional allowlist. When non-empty, only these repo keys enter the
    # snapshot (and so the diff, archive, and receiver import). Empty
    # (default) syncs every repo. The exclusion filters above still apply on
    # top: a repo is synced only if it is in included_repos AND not excluded,
    # so BuildInfo/system repos stay out even if one is listed here.
    #   AIRLIFT_INCLUDED_REPOS=foo,bar
    included_repos: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator(
        "excluded_repos", "excluded_package_types", "included_repos", mode="before"
    )
    @classmethod
    def _split_csv(cls, v):
        # Env vars arrive as raw strings; accept comma-separated values
        # for operator ergonomics (AIRLIFT_EXCLUDED_REPOS=foo,bar).
        # YAML / Python list inputs pass through unchanged.
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

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
