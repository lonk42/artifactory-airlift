"""Env vars must override the mounted config file, not the other way round.

``config.load()`` parses /etc/airlift/config.yaml and hands it to Settings as
init kwargs, which pydantic-settings ranks above env vars by default. That
inverted the documented layering and, because the Helm chart renders every
key into the ConfigMap whether the operator set it or not, a mounted
ConfigMap silently masked every AIRLIFT_ env var. Settings reorders its
sources to fix this; these tests pin the behaviour down.
"""

from __future__ import annotations

from pathlib import Path

from artifactory_airlift import config

# Mirrors what the Helm chart renders with default values: every key present,
# several of them carrying a "nothing set" value.
_CHART_CONFIG = """\
mode: "sender"
instance_name: ""
artifactory_url: "http://localhost:8081/artifactory"
cycle_seconds: 300
included_repos: []
spool_dir: "/var/airlift/spool"
"""


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body)
    return cfg


def test_env_overrides_config_file(tmp_path, monkeypatch) -> None:
    # The regression: an allowlist set by env was thrown away by the chart's
    # default `included_repos: []`.
    cfg = _write(tmp_path, _CHART_CONFIG)
    monkeypatch.setenv("AIRLIFT_INCLUDED_REPOS", "airlift-rpm-local,airlift-npm-local")
    monkeypatch.setenv("AIRLIFT_INSTANCE_NAME", "site-a")
    monkeypatch.setenv("AIRLIFT_CYCLE_SECONDS", "45")

    s = config.load(cfg)

    assert s.included_repos == ["airlift-rpm-local", "airlift-npm-local"]
    assert s.instance_name == "site-a"
    assert s.cycle_seconds == 45


def test_config_file_still_applies_without_env(tmp_path, monkeypatch) -> None:
    # Demoting init kwargs must not stop the file being read; these values are
    # all deliberately different from the code defaults.
    monkeypatch.delenv("AIRLIFT_CYCLE_SECONDS", raising=False)
    monkeypatch.delenv("AIRLIFT_INCLUDED_REPOS", raising=False)
    cfg = _write(
        tmp_path,
        'mode: "receiver"\n'
        'instance_name: "from-file"\n'
        "cycle_seconds: 77\n"
        'included_repos: ["from-file-repo"]\n'
        'spool_dir: "/tmp/from-file-spool"\n',
    )

    s = config.load(cfg)

    assert s.mode == "receiver"
    assert s.instance_name == "from-file"
    assert s.cycle_seconds == 77
    assert s.included_repos == ["from-file-repo"]
    assert s.spool_dir == Path("/tmp/from-file-spool")


def test_env_overlays_only_the_keys_it_sets(tmp_path, monkeypatch) -> None:
    # Layering, not replacement: keys absent from the environment keep the
    # file's value.
    cfg = _write(
        tmp_path,
        'mode: "receiver"\n'
        'instance_name: "from-file"\n'
        "cycle_seconds: 77\n"
        'spool_dir: "/tmp/from-file-spool"\n',
    )
    monkeypatch.setenv("AIRLIFT_CYCLE_SECONDS", "99")

    s = config.load(cfg)

    assert s.cycle_seconds == 99
    assert s.instance_name == "from-file"
    assert s.spool_dir == Path("/tmp/from-file-spool")
