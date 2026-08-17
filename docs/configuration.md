# Configuration

The sidecar reads `/etc/airlift/config.yaml` from the mounted ConfigMap and overlays environment variables prefixed `AIRLIFT_`.
Environment wins over the file.
Every key is optional except the credentials.

> Every variable the sidecar consumes starts with `AIRLIFT_`.
> A bare `ARTIFACTORY_TOKEN` is ignored: Pydantic Settings uses `env_prefix="AIRLIFT_"`.

The Helm chart renders a subset of these keys into the ConfigMap.
A setting with no chart value is set through the sidecar's `env` block.
The chart renders every key it knows unconditionally, including ones you never set, so check the rendered ConfigMap after changing values:

```sh
helm template airlift ./helm -f values.yaml -s templates/configmap.yaml
```

## Authentication

Airlift accepts a bearer access token or a username and password pair.
The token can be given directly or read from a file.
Resolution order is basic auth, then `artifactory_token_file`, then `artifactory_token`, so leave username and password empty to use a token.

**Use an admin-scoped access token.**
Generate it once through the Artifactory UI (Administration, User Management, Access Tokens) for a user with admin rights, ideally a dedicated service user so the credential is independently revocable, with no expiry.
Airlift needs admin-level access because it drives `/api/import/repositories` on the destination.
A token scoped `applied-permissions/admin`, or any token whose subject is an admin user, authorises every endpoint it calls.

A token cannot be bootstrapped from a username and password.
The legacy `POST /api/security/token` endpoint only mints `member-of-groups:*` scopes and refuses the admin scope name, while `/access/api/v1/tokens`, which can mint admin scope, rejects both basic auth and legacy tokens.
This affects self-bootstrapping only; generating the token in the UI and supplying it works.

The chart writes credentials into a Secret named `artifactory-airlift-token` with keys `token`, `username` and `password`, read by the sidecar as `AIRLIFT_ARTIFACTORY_TOKEN`, `AIRLIFT_ARTIFACTORY_USERNAME` and `AIRLIFT_ARTIFACTORY_PASSWORD`.

### A token that rotates

Set `artifactory_token_file` to a path holding the token.
It is re-read on every request, so a token rotated in place is picked up without a restart, and a 401 raised mid-rotation is retried with a freshly read value.
Surrounding whitespace is stripped, which matters because `kubectl create secret --from-file` leaves a trailing newline.

The chart mounts it from `artifactory.tokenFile.existingSecret`.
When that is set, the chart skips its own Secret and omits the token/username/password `secretKeyRef` env vars, because an operator-supplied Secret holding only a `token` key would fail the other two references and the pod would not start.

**Do not mount it with `subPath`.**
Kubernetes never propagates updates into a `subPath` mount, so the token would freeze at pod start.
Secret volume updates land on the kubelet sync period, so expect up to about a minute of lag.

### Trusting a private CA

Airlift's HTTP client verifies against the bundled certifi store and does not read `SSL_CERT_FILE` or `SSL_CERT_DIR`, so a private CA has to be named:
set `artifactory_ca_cert` to a single concatenated PEM bundle **file**, not a directory.

CA material is public, so a ConfigMap is the idiomatic home for it, and many clusters already expose one (the per-namespace `kube-root-ca.crt`, a platform-injected bundle, or a cert-manager trust-manager `Bundle`).
Reference it through `artifactory.caCert.existingConfigMap` with `key` and `mountPath`; the chart mounts it and points the variable at the file.
The chart does not create the ConfigMap.

There is no option to disable verification.

## Snapshot retention

`state/snapshots/*.jsonl` is both the next cycle's diff baseline and the record of what the source held.
The three `snapshot_retention_*` keys give a grandfather-father-son policy: each tier keeps the newest snapshot in every non-empty bucket within its window, and the keep set is the union across tiers, so one snapshot can satisfy several.

`snapshot_retention_hours: 10`, `snapshot_retention_days: 30`, `snapshot_retention_months: 12` keeps one per hour for ten hours, one per day for thirty days, and one per calendar month for twelve months.
At least one tier must be above zero.
Months are calendar months, not thirty-day windows.

`state/exports/<cycle_id>/` holds the synthesised metadata trees and is retained by count through `history_keep`, decoupled from snapshot retention.
An old snapshot kept by the monthly tier has no matching tree, which is fine: it is a reference, not a replay source.

## Repository scope

By default the sender mirrors every repository except the JFrog system and BuildInfo repositories it always drops.

- `included_repos` narrows to a list of repository keys. Empty means everything.
- `excluded_repos` is a literal denylist, defaulting to the JFrog-owned repositories that either fail to import or are platform-managed.
- `excluded_package_types` drops repositories by `packageType`, defaulting to `BuildInfo`. This catches user-created build-info repositories that a name list would miss.

Both filters are part of the AQL query, so excluded repositories are never fetched.
The allowlist narrows and does not override: a repository syncs only when it is in `included_repos` **and** not caught by either denylist, so naming a system repository in the allowlist will not force it through.

Confirm with `Allowlist active: syncing only N repo(s)` at cycle start.

**Narrowing scope reads as mass deletion.**
Everything outside the new scope disappears from the snapshot, and the diff treats that as deleting it from the destination.
The brake refuses such a cycle.
To change scope deliberately, change the setting and then delete `state/cursor.json` on the sender, which makes the next cycle a cold start that emits no removals.
See [the deletion brake](architecture.md#the-deletion-brake).

## Reference

| Key (yaml) | Env | Default | Description |
| --- | --- | --- | --- |
| `mode` | `AIRLIFT_MODE` | `sender` | Which loop to run. `sender` enumerates, diffs and spools archives; `receiver` ingests archives, writes blobs and imports. |
| `instance_name` | `AIRLIFT_INSTANCE_NAME` | `unknown` | Label recorded in manifests and log lines. Useful when more than one source feeds a destination. |
| `artifactory_url` | `AIRLIFT_ARTIFACTORY_URL` | `http://localhost:8081/artifactory` | Base URL of the local Artifactory. As a sidecar this is loopback; override for a non-default port or when a private CA requires the external name. |
| `artifactory_token` | `AIRLIFT_ARTIFACTORY_TOKEN` | `""` | Bearer access token whose subject is an admin user. Ignored when basic auth or `artifactory_token_file` is set. |
| `artifactory_token_file` | `AIRLIFT_ARTIFACTORY_TOKEN_FILE` | `""` | Path to a file holding the token, re-read every request so rotation needs no restart. Whitespace is stripped. Outranks `artifactory_token`; basic auth outranks both. |
| `artifactory_username` | `AIRLIFT_ARTIFACTORY_USERNAME` | `""` | Admin username for basic auth. When username and password are both set, basic auth wins over any token. |
| `artifactory_password` | `AIRLIFT_ARTIFACTORY_PASSWORD` | `""` | Password paired with `artifactory_username`. |
| `artifactory_ca_cert` | `AIRLIFT_ARTIFACTORY_CA_CERT` | `""` | Path to a PEM CA bundle file used to verify Artifactory's certificate. Empty uses the certifi store. |
| `cycle_seconds` | `AIRLIFT_CYCLE_SECONDS` | `300` | Seconds between cycles. Sender: time between enumerations. Receiver: spool poll interval. |
| `propagate_deletes` | `AIRLIFT_PROPAGATE_DELETES` | `true` | Sender-only. Include a `removed[]` list so the receiver converges deletions. Cold-start cycles never emit removals. Set false for additive-only. |
| `max_delete_fraction` | `AIRLIFT_MAX_DELETE_FRACTION` | `0.05` | Sender-only. Refuse any cycle removing more than this fraction of the baseline. `1.0` disables the brake; `0` refuses any deletion. |
| `included_repos` | `AIRLIFT_INCLUDED_REPOS` | `[]` (all) | Sender-only allowlist of repository keys. Empty syncs everything. The denylists still apply on top. |
| `excluded_repos` | `AIRLIFT_EXCLUDED_REPOS` | JFrog system repos | Sender-only literal denylist. Comma-separated repository keys. |
| `excluded_package_types` | `AIRLIFT_EXCLUDED_PACKAGE_TYPES` | `BuildInfo` | Sender-only denylist by `packageType`, resolved through `/api/repositories` at cycle start. Falls back to the name list if that call fails. |
| `history_keep` | `AIRLIFT_HISTORY_KEEP` | `24` | Sender-only. Number of synthesised metadata trees to retain under `state/exports/`. |
| `done_keep_hours` | `AIRLIFT_DONE_KEEP_HOURS` | `72` | Receiver-only. How long to retain processed archives under `spool/.done/`. `0` keeps them forever. |
| `max_archive_bytes` | `AIRLIFT_MAX_ARCHIVE_BYTES` | `8Gi` | Sender-only per-archive raw blob budget. Takes a k8s quantity or a plain integer. Exceeding it splits the cycle into `<cycle_id>-cNNN.tar.zst` chunks. `0` disables splitting. |
| `spool_min_free_bytes` | `AIRLIFT_SPOOL_MIN_FREE_BYTES` | `2Gi` | Sender-only free-space floor on the spool volume. Each chunk needs `free >= this + projected size` or the cycle aborts without advancing the cursor. |
| `snapshot_retention_hours` | `AIRLIFT_SNAPSHOT_RETENTION_HOURS` | `0` | Sender-only. Keep the newest snapshot per hour-bucket for N hours. |
| `snapshot_retention_days` | `AIRLIFT_SNAPSHOT_RETENTION_DAYS` | `3` | Sender-only. Keep the newest snapshot per day-bucket for N days, UTC. |
| `snapshot_retention_months` | `AIRLIFT_SNAPSHOT_RETENTION_MONTHS` | `0` | Sender-only. Keep the newest snapshot per calendar month for N months. |
| `binarystore_config` | `AIRLIFT_BINARYSTORE_CONFIG` | `/var/opt/jfrog/artifactory/etc/artifactory/binarystore.xml` | Artifactory's binarystore descriptor, parsed at startup to detect the backend. Absent or unparseable falls back to `filestore_root`. |
| `binarystore_provider` | `AIRLIFT_BINARYSTORE_PROVIDER` | `auto` | Backend override. `auto` trusts the XML. `filesystem` forces the on-disk path. `s3` and `azure` assert the detected backend and fail at startup otherwise. Set it once the backend is known. |
| `binarystore_prefix` | `AIRLIFT_BINARYSTORE_PREFIX` | `""` (from the XML) | Key prefix override, the `<path>` in `<path>/<sha1[:2]>/<sha1>`. Empty takes it from the XML. Use `/` for the bucket root. See [when the prefix is not in the XML](binarystore.md#when-the-key-prefix-is-not-in-the-xml). |
| `binarystore_access_key` | `AIRLIFT_BINARYSTORE_ACCESS_KEY` | `""` | Access key for an S3-compatible store. Needed when the XML holds no plaintext credentials, which is the case for Artifactory's own copy. Outranks the XML. |
| `binarystore_secret_key` | `AIRLIFT_BINARYSTORE_SECRET_KEY` | `""` | Secret key paired with `binarystore_access_key`. |
| `binarystore_account_key` | `AIRLIFT_BINARYSTORE_ACCOUNT_KEY` | `""` | Shared key for an Azure Blob store. Leave empty to authenticate as a platform identity; setting it pins shared-key signing. Account name and container come from the XML. |
| `binarystore_ca_cert` | `AIRLIFT_BINARYSTORE_CA_CERT` | `""` | PEM CA bundle for the object-storage endpoint. Separate from `artifactory_ca_cert` because the store commonly sits behind a different CA. |
| `binarystore_multipart_threshold` | `AIRLIFT_BINARYSTORE_MULTIPART_THRESHOLD` | `256Mi` | Blobs at or above this upload as S3 multipart or Azure staged blocks. A single S3 PUT is capped at 5 GiB, so this is what lets large artifacts through. |
| `filestore_root` | `AIRLIFT_FILESTORE_ROOT` | `/var/opt/jfrog/artifactory/data/artifactory/filestore` | On-disk binarystore path, used only when the backend is file-system. |
| `state_dir` | `AIRLIFT_STATE_DIR` | `/var/airlift/state` | Durable per-side state: snapshots, cursor, ledger, lock, extracted import trees. Must be a PVC. |
| `spool_dir` | `AIRLIFT_SPOOL_DIR` | `/var/airlift/spool` | Where finalised archives land and are picked up. Never NFS. |
| `artifactory_uid` | `AIRLIFT_ARTIFACTORY_UID` | `1030` | UID the receiver chowns blobs to. Must match the Artifactory process. Skipped when the process lacks permission. |
| `artifactory_gid` | `AIRLIFT_ARTIFACTORY_GID` | `1030` | GID counterpart. |
| `artifactory_tmp` | `AIRLIFT_ARTIFACTORY_TMP` | `/var/opt/jfrog/artifactory/data/artifactory/tmp` | Artifactory's tmp directory. Informational; reserved. |
| `log_level` | `AIRLIFT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `log_format` (env only) | `AIRLIFT_LOG_FORMAT` | `console` | `console` emits one readable line per event. `json` emits structlog JSON. |
