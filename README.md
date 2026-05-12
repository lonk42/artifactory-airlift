# Artifactory Airlift

One-way, delta-only sync designed for air-gapped JFrog Artifactory Kubernetes implementations. Airlift runs as a sidecar in a source and destination Artifactory to synchronise repos in one direction.

Unlike Artifactory's existing [import/export API](https://docs.jfrog.com/installation/docs/import-and-export) workflow, in sending mode Airlift uses the [system export API](https://docs.jfrog.com/artifactory/reference/exportsystem) to generate deltas from the binarystore metadata DB. From these deltas Airlift generates manifests, and writes a per-cycle archive containing only the new blobs plus the metadata needed to import them. In receiving mode, Airlift ingests these archives and uses a combination of Artifactory's repository import API and atomic file operations to link them. Airlift does not provide a transport mechanism between sidecars.

Tested against Artifactory 7.146.x. The sidecar runs as uid/gid 1030 to match the artifactory user.

## Architecture

```
 source side (sender)                                destination side (receiver)
 ┌────────────────────────────────┐                  ┌────────────────────────────────┐
 │ artifactory pod                │                  │ artifactory pod                │
 │ ┌──────────┐   ┌────────────┐  │                  │  ┌────────────┐   ┌──────────┐ │
 │ │artifactory├──▶ airlift    │  │  spool PVC       │  │ airlift    ◀───┤artifactory│ │
 │ │           │   │ (sender)  │──┼───▶ archive ─────┼──▶ (receiver) │   │           │ │
 │ └──────────┘   └────────────┘  │  one-way         │  └────────────┘   └──────────┘ │
 │     │  shared filestore PVC    │  transport       │     ▲ shared filestore PVC    │
 │     ▼                          │  (out of scope)  │     │                          │
 │  state PVC + spool PVC         │                  │  state PVC + spool PVC         │
 └────────────────────────────────┘                  └────────────────────────────────┘
```

### What the sender does each cycle

1. `POST /api/export/system` with `excludeContent=true,includeMetadata=true,createArchive=false`. Artifactory writes the export tree to a path on the state PVC, nested inside a timestamped subdirectory.
2. Walk the export tree and parse every `*.artifactory-metadata/artifactory-file.xml`, extracting `(repo_key, repo_path, sha1, size)`. Write a sorted JSONL snapshot at `state/snapshots/<cycle_id>.jsonl`.
3. Two-pointer set-diff against the previous snapshot to compute new `(sha1, repo, path)` triples.
4. For each new sha1, locate the raw blob at `<FILESTORE_ROOT>/<sha1[:2]>/<sha1>` (airlift mounts the filestore PVC read-only on the sender side).
5. Build `<cycle_id>.tar.zst` containing `manifest.json`, the full `metadata/` subtree from the export, and `blobs/<aa>/<sha1>` entries. The archive is finalised atomically (`.partial` → `os.rename`).
6. Prune `state/snapshots/` and `state/exports/` past the configured history depth.

### What the receiver does each cycle

1. List `spool/*.tar.zst` in lexical (and time) order.
2. Skip any cycle_id already present in `state/processed.jsonl`.
3. Extract the next archive to `state/import/<cycle_id>/` (must be on the state PVC, not under the artifactory data dir; the import API rejects paths under `/var/opt/jfrog/artifactory/...`).
4. Stream each blob from `blobs/<aa>/<sha1>` into `<FILESTORE_ROOT>/<aa>/<sha1>.tmp-<pid>`, `fsync`, `rename`, `chown 1030:1030`, `chmod 0640`. Existing sha1s are skipped (filestore is content-addressed and idempotent).
5. `POST /api/import/repositories?path=<extract_dir>/metadata/repositories&verbose=1`. Artifactory returns 200 even when individual repos fail; the receiver scans the verbose response body for `500 :`, `400 :`, `404 :`, and `Error` lines and records them as per-repo failures.
6. Append `{cycle_id, status, blob_count, total_bytes, repos, failures, processed_at}` to `state/processed.jsonl` and move the archive to `spool/.done/`.

### Failure semantics

The receiver is add-only and idempotent. Replaying an archive is a no-op because (a) sha1s already in the filestore are skipped and (b) the cycle_id is in `processed.jsonl`. A missing predecessor (`prev_cycle_id` not yet processed) is logged but does not block; wider future diffs converge state. A partial import (some repos failed) records `status: "partial"` in the ledger and leaves the archive in `.done/` for forensic replay.

## Repo layout

```
src/artifactory_airlift/    Python package (sender + receiver share the same image)
helm/                       ConfigMap + Secret + state/spool PVCs (does NOT deploy the sidecar)
tests/unit/                 offline unit tests
tests/e2e/                  live-cluster tests, gated on E2E=1
Dockerfile                  multi-stage build, runs as uid 1030
```

`AIRLIFT_MODE=sender|receiver` selects behaviour. Both sides run the same image.

## Prerequisites

- Two JFrog Artifactory instances deployed to Kubernetes. Only the [jfrog/artifactory](https://github.com/jfrog/charts) Helm chart implementation is covered.
- The Airlift container image is not yet published in a container registry; you will need to build and publish it yourself.
- An admin username + password on each Artifactory (see [Authentication](#authentication) below).
- Every repository that exists on the source must also exist on the destination before the first sync. Repository *definitions* are not propagated by this tool.

## Building the airlift sidecar image

```sh
docker build -t <your-registry>/artifactory-airlift:<version> .
docker push     <your-registry>/artifactory-airlift:<version>
```

## Deploy

There are two pieces to wire up per Artifactory instance:

1. **This repo's Helm chart** ships the ConfigMap, Secret, and PVCs that the sidecar reads for each Artifactory instance.
2. **The jfrog/artifactory chart's `customSidecarContainers` field** is where the sidecar container actually gets injected. The supporting volumes and mounts go in `customVolumes` / `customVolumeMounts` alongside it.

### 1. Install the supporting resources

Install the Helm chart in the same namespace as your existing Artifactory instance. Make sure to set the mode to either `sender` or `receiver` in your values.

```sh
# Source side
helm install artifactory-airlift ./helm \
  --namespace <source-namespace> \
  --set mode=sender \
  --set instanceName=<source-name> \
  --set image.repository=<your-registry>/artifactory-airlift \
  --set image.tag=<version> \
  --set artifactory.username=admin \
  --set artifactory.password=<admin-password>
```

### 2. Inject the sidecar into the artifactory pod

Add the following to the jfrog/artifactory Helm values for each instance, under `artifactory.artifactory.*`. `helm template ./helm | grep -A1 'NOTES'` prints the same block ready to paste; the values referenced below come from this chart's defaults.

```yaml
artifactory:
  artifactory:
    customVolumes: |
      - name: airlift-state
        persistentVolumeClaim: { claimName: artifactory-airlift-state }
      - name: airlift-spool
        persistentVolumeClaim: { claimName: artifactory-airlift-spool }
      - name: airlift-config
        configMap: { name: artifactory-airlift-config }
    customVolumeMounts: |
      - { name: airlift-state, mountPath: /var/airlift/state }
      - { name: airlift-spool, mountPath: /var/airlift/spool }
      - { name: airlift-config, mountPath: /etc/airlift }
    customSidecarContainers: |
      - name: airlift
        image: <your-registry>/artifactory-airlift:<version>
        env:
          - { name: AIRLIFT_MODE, value: sender }   # "receiver" on the destination
          - name: AIRLIFT_ARTIFACTORY_USERNAME
            valueFrom: { secretKeyRef: { name: artifactory-airlift-token, key: username } }
          - name: AIRLIFT_ARTIFACTORY_PASSWORD
            valueFrom: { secretKeyRef: { name: artifactory-airlift-token, key: password } }
        volumeMounts:
          - { name: airlift-state, mountPath: /var/airlift/state }
          - { name: airlift-spool, mountPath: /var/airlift/spool }
          - { name: airlift-config, mountPath: /etc/airlift }
          - { name: artifactory-volume, mountPath: /var/opt/jfrog/artifactory }
        securityContext: { runAsUser: 1030, runAsGroup: 1030 }
```

The state PVC must be mounted in **both** the artifactory container and the airlift container; the sender writes the system export there for the artifactory process to read, and the receiver writes the extracted import tree there for the artifactory process to read. The jfrog chart wires this automatically because the mount lives in `customVolumeMounts`.

The `artifactory-volume` mount on the sidecar is what gives the sender read access to the filestore (and the receiver write access). It's the same PVC the artifactory container uses.

### 3. Pre-create destination repositories

Every repo that exists on the source must also exist on the destination before the first sync. The batch import API returns `500 : The directory <repo> does not match any repository key.` when it can't find a matching repo on the destination. Create them via the Artifactory UI, `PUT /api/repositories/<key>`, or your provisioning tooling.

TODO: This is in-scope for a future feature

### 4. Transport

The sender writes finalised archives to `/var/airlift/spool/*.tar.zst`. The receiver reads from the same path on its side. Moving archives between the two PVCs is **not** in scope. (For testing use manual `kubectl cp`). The archives are content-addressed and idempotent; replay is safe.

## Authentication

Airlift accepts either a bearer access token *or* a username + password pair. When both are provided basic auth takes precedence.

**Recommendation: use basic auth.** A token minted via the legacy `POST /api/security/token` endpoint with username + password gets `scope=member-of-groups:*`, which Artifactory accepts on read-style admin endpoints but rejects with 401 on destructive ones (notably `/api/export/system`). The proper `applied-permissions/admin` scope can only be requested by an already-admin bearer token via `/access/api/v1/tokens`, which means there is no clean bootstrap path from username/password to an admin-scoped token without going through the Artifactory UI. Basic auth sidesteps this entirely.

The Helm chart writes the credentials into a Kubernetes Secret named `artifactory-airlift-token` with three keys: `token`, `username`, `password`. The sidecar reads them via the `AIRLIFT_ARTIFACTORY_TOKEN`, `AIRLIFT_ARTIFACTORY_USERNAME`, and `AIRLIFT_ARTIFACTORY_PASSWORD` env vars.

> Every env var the sidecar consumes starts with `AIRLIFT_`. Bare `ARTIFACTORY_TOKEN` etc. are silently ignored; Pydantic Settings uses `env_prefix="AIRLIFT_"`.

## Configuration reference

The sidecar reads `/etc/airlift/config.yaml` from the mounted ConfigMap and overlays env vars prefixed `AIRLIFT_`. All keys are optional except auth.

| Key (yaml)              | Env                              | Default                                                  | Description                                                                                                                  |
| ----------------------- | -------------------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `mode`                  | `AIRLIFT_MODE`                   | `sender`                                                 | Which loop to run: `sender` exports + diffs + spools archives; `receiver` ingests archives + writes blobs + imports repos.   |
| `instance_name`         | `AIRLIFT_INSTANCE_NAME`          | `unknown`                                                | Free-form label recorded in archive manifests and log lines. Helpful when more than one source feeds the same destination.   |
| `artifactory_url`       | `AIRLIFT_ARTIFACTORY_URL`        | `http://localhost:8081/artifactory`                      | Base URL of the local Artifactory. As a sidecar this is loopback; override only if Artifactory listens on a non-default port.|
| `artifactory_token`     | `AIRLIFT_ARTIFACTORY_TOKEN`      | `""`                                                     | Bearer access token. Must be `applied-permissions/admin`-scoped to call `/api/export/system`. Ignored when basic auth is set.|
| `artifactory_username`  | `AIRLIFT_ARTIFACTORY_USERNAME`   | `""`                                                     | Admin username for basic auth. When both username and password are set, basic auth takes precedence over `artifactory_token`.|
| `artifactory_password`  | `AIRLIFT_ARTIFACTORY_PASSWORD`   | `""`                                                     | Admin password paired with `artifactory_username`. Inject via a Secret; the chart writes this into `artifactory-airlift-token`.|
| `cycle_seconds`         | `AIRLIFT_CYCLE_SECONDS`          | `300`                                                    | Seconds between cycles. Sender: time between exports/diffs. Receiver: poll interval for new archives in the spool dir.       |
| `history_keep`          | `AIRLIFT_HISTORY_KEEP`           | `24`                                                     | Sender-only. Number of past snapshots and raw export trees to retain under `state/` before pruning the oldest.               |
| `done_keep_hours`       | `AIRLIFT_DONE_KEEP_HOURS`        | `72`                                                     | Receiver-only. How long to retain processed archives under `spool/.done/` before deleting them. Set `0` to keep forever.     |
| `filestore_root`        | `AIRLIFT_FILESTORE_ROOT`         | `/var/opt/jfrog/artifactory/data/artifactory/filestore`  | Path to Artifactory's binarystore. Sender reads blobs by sha1; receiver writes blobs into `<root>/<sha1[:2]>/<sha1>`.        |
| `artifactory_tmp`       | `AIRLIFT_ARTIFACTORY_TMP`        | `/var/opt/jfrog/artifactory/data/artifactory/tmp`        | Artifactory's tmp dir. Reserved for future use; currently informational.                                                     |
| `state_dir`             | `AIRLIFT_STATE_DIR`              | `/var/airlift/state`                                     | Durable per-side state (snapshots, cursor, processed ledger, lockfile, extracted import trees). Must be on a PVC.            |
| `spool_dir`             | `AIRLIFT_SPOOL_DIR`              | `/var/airlift/spool`                                     | Where finalised `*.tar.zst` archives land (sender) and where they're picked up from (receiver). Never NFS; tearing on fsync. |
| `artifactory_uid`       | `AIRLIFT_ARTIFACTORY_UID`        | `1030`                                                   | UID the receiver chowns blobs to when placing them in the filestore. Must match the artifactory process's UID.               |
| `artifactory_gid`       | `AIRLIFT_ARTIFACTORY_GID`        | `1030`                                                   | GID counterpart of `artifactory_uid`.                                                                                        |
| `log_level`             | `AIRLIFT_LOG_LEVEL`              | `INFO`                                                   | Structlog level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. JSON logs are written to stdout.                                       |

## Troubleshooting the sidecar

### Inspect state

```sh
# Sender: snapshots, cursor, raw exports retained per history_keep
kubectl -n <source-namespace> exec sts/artifactory -c airlift -- ls /var/airlift/state

# Both: spool. Pending archives at the top, finalised under .done/
kubectl -n <namespace> exec sts/artifactory -c airlift -- ls -R /var/airlift/spool

# Receiver: idempotency ledger (one line per cycle)
kubectl -n <destination-namespace> exec sts/artifactory -c airlift -- cat /var/airlift/state/processed.jsonl
```

### Force a cycle now

Restart the sidecar container; the cycle loop runs once immediately on startup. Either kill PID 1 inside the container, or `kubectl rollout restart` the StatefulSet (heavier, restarts artifactory too).

```sh
kubectl -n <namespace> exec sts/artifactory -c airlift -- kill 1
```

### Reset a side

```sh
# Sender: drop all state + spool, force a clean baseline cycle on the next tick
kubectl -n <source-namespace> exec sts/artifactory -c airlift -- sh -c 'rm -rf /var/airlift/state/* /var/airlift/spool/*'

# Receiver: drop the ledger so every archive in spool gets reprocessed (idempotent, safe)
kubectl -n <destination-namespace> exec sts/artifactory -c airlift -- rm -f /var/airlift/state/processed.jsonl
```

### Common issues

**`503` on `/api/system/ping` right after a restart.** Artifactory is still booting. The retry decorator handles it; the next cycle succeeds.

**`401` on `/api/export/system`.** You're authenticating with a legacy access token whose scope is `member-of-groups:*`. Either switch to basic auth (set `artifactory.username` and `artifactory.password`), or mint an `applied-permissions/admin`-scoped token through the Artifactory UI and put it in `artifactory.token`.

**Sender's snapshot count is always 0.** Either (a) the export path isn't being read at the right subdirectory; Artifactory writes the export tree into a nested `<timestamp>/` subdir inside the path you give it, and the sender descends into this automatically; or (b) the metadata file isn't named `artifactory-file.xml` on your Artifactory version. The parser is `src/artifactory_airlift/export_unpacker.py:_parse_fileinfo`.

**Receiver records `500 : The directory <name> does not match any repository key.` in `processed.jsonl`.** The named repo exists on the source but not on the destination. For JFrog system repos (`jfrog-usage-logs`, `artifactory-build-info`, `auth-tokens`, etc.) this is benign; they're per-instance and shouldn't be synced. For your own repos, create them on the destination first.

**Receiver records `500` referencing `/api/repositories/<repo>/import`.** You're on an old version of airlift that called the broken per-repo endpoint. v0.3.0+ uses the batch endpoint `/api/import/repositories`, which works on 7.146.x. Upgrade the image.

**Sender refuses to start with empty token despite the env being set.** The env var name must start with `AIRLIFT_`. Bare `ARTIFACTORY_TOKEN` is silently ignored; Pydantic Settings uses `env_prefix="AIRLIFT_"`.

**Import call rejected with `Invalid Import Directory`.** The path passed to the import API must not be under `/var/opt/jfrog/artifactory/...`. The receiver extracts to `state_dir/import/<cycle_id>/` (under the state PVC) for this reason.

## Limitations

- **Artifactory version**: tested against 7.146.x. The system-export and batch-import API shapes are version-specific; other versions may need tweaks in `artifactory_client.py` and `export_unpacker.py`.
- **Repository definitions are not synced.** Pre-create all repos on the destination.
- **Filestore provider**: this tool assumes the default file-system binarystore. Object-store / S3 / multi-tier providers are not yet supported; the path math `<root>/<sha1[:2]>/<sha1>` is hardcoded.
- **Transport is out of scope.** Moving archives between the two spool PVCs is whatever your air-gap mechanism is.
- **`/api/import/system` overwrite caveat.** This tool does *not* call `/api/import/system`; it uses `/api/import/repositories`, which is non-destructive for instance config. If you change the receiver to use `/api/import/system` as a fallback, be aware that endpoint overwrites the destination's config (security data, joinKey, masterKey) with the source's.

## TODO

- **Create destination repositories.** They must currently be created manually; this will require additional API permissions and metadata.
- **Authentication should avoid username/password.** Test bearer auth and find a better pattern.
- **Logging readability.** Logs are JSON objects; stdout should be more parseable, with JSON relegated to debug logging.
- **Metadata snapshot de-duplication.** Metadata exports should be ignored if there are no differences, to reduce unnecessary retention.
- **Hooks for external alerting.** On partial and full failures you are gonna wanna know
- **Metadata retention periods.** The basic x count retention works but a more granular x hours, x days, x months would be good for backfilling longer desyncs.
