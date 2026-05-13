"""Idempotent helpers for creating projects and local repos on a live cluster.

Used by the session-scoped `provisioned_repos` fixture so the e2e suite is
self-contained: it asserts the desired state of A and B at session start
and leaves the repos in place between runs (so the soak scripts in
`scripts/soak/` can reuse them).

Auth split:
  - Project create/read/delete goes through the JFrog UI bridge at
    /ui/api/v1/projects with admin cookie auth (Access /access/api/v1
    rejects everything we can mint from username/password).
  - Repo CRUD goes through /artifactory/api/repositories with basic auth
    (works fine on the cluster).
"""

from __future__ import annotations

from dataclasses import dataclass

from .conftest import ArtClient


class ProjectsUnavailable(RuntimeError):
    """Raised when the JFrog Projects feature is not licensed/accessible."""


@dataclass(frozen=True)
class ProvisionedRepo:
    key: str
    package_type: str
    project: str | None


def ensure_project(client: ArtClient, project_key: str, display_name: str) -> None:
    """Create the project if missing. Idempotent on repeated runs.

    Goes through the UI bridge because /access/api/v1/projects rejects every
    auth shape reachable from username/password on this version of JFrog.

    Raises ProjectsUnavailable if the bridge returns 403/404 on a write,
    suggesting Projects is not enabled.
    """
    body = {
        "name": display_name,
        "projectKey": project_key,
        "description": "airlift e2e + soak test fixtures",
        "softLimit": False,
        "storageQuota": -1,
        "canProjectAdminsManageResources": True,
        "canProjectAdminsCreateLocalRepository": True,
        "canProjectAdminsDeleteLocalRepository": True,
        "canAdminsManageMembers": True,
        "canAdminsIndexResources": True,
    }
    ui = client.ui_session()
    try:
        r = ui.get(f"/ui/api/v1/projects/{project_key}")
        if r.status_code == 200:
            return
        if r.status_code not in (404, 400):
            raise RuntimeError(
                f"GET /ui/api/v1/projects/{project_key} -> {r.status_code} {r.text[:200]}"
            )
        r = ui.post("/ui/api/v1/projects", json=body)
        if r.status_code in (200, 201):
            return
        if r.status_code in (403, 404):
            raise ProjectsUnavailable(
                f"POST /ui/api/v1/projects returned {r.status_code} "
                f"(Projects not enabled?): {r.text[:200]}"
            )
        # Conflict / already exists is benign.
        if r.status_code == 409:
            return
        if r.status_code == 400 and "already exists" in r.text.lower():
            return
        raise RuntimeError(
            f"POST /ui/api/v1/projects -> {r.status_code} {r.text[:200]}"
        )
    finally:
        ui.close()


def ensure_local_repo(
    client: ArtClient,
    repo_key: str,
    package_type: str,
    project_key: str | None = None,
) -> None:
    """Create a local repo of the given package type. Idempotent.

    Existing repos return 400 with "Case insensitive repository key already exists"
    or similar; that is treated as success.
    """
    body: dict[str, object] = {
        "key": repo_key,
        "rclass": "local",
        "packageType": package_type,
    }
    if project_key:
        body["projectKey"] = project_key
    with client.http() as http:
        r = http.get(f"/api/repositories/{repo_key}")
        if r.status_code == 200:
            existing = r.json()
            existing_project = existing.get("projectKey") or None
            if (project_key or None) != (existing_project or None):
                raise RuntimeError(
                    f"repo {repo_key!r} exists but projectKey "
                    f"{existing_project!r} != desired {project_key!r}"
                )
            existing_type = (existing.get("packageType") or "").lower()
            if existing_type != package_type.lower():
                raise RuntimeError(
                    f"repo {repo_key!r} exists but packageType "
                    f"{existing_type!r} != desired {package_type!r}"
                )
            return
        if r.status_code not in (400, 404):
            r.raise_for_status()
        r = http.put(f"/api/repositories/{repo_key}", json=body)
        if r.status_code in (200, 201):
            return
        if r.status_code == 400 and "already exists" in r.text.lower():
            return
        r.raise_for_status()
