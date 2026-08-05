# Artifactory Airlift

One-way, delta-only sync designed for air-gapped JFrog Artifactory Kubernetes implementations. Airlift runs as a sidecar in a source and destination Artifactory to synchronise repos in one direction.

Unlike Artifactory's existing [import/export API](https://docs.jfrog.com/installation/docs/import-and-export) workflow, in sending mode Airlift uses the [system export API](https://docs.jfrog.com/artifactory/reference/exportsystem) to generate deltas from the binarystore metadata DB. From these deltas Airlift generates manifests, and writes a per-cycle archive containing only the new blobs plus the metadata needed to import them. In receiving mode, Airlift ingests these archives and uses a combination of Artifactory's repository import API and atomic file operations to link them. Airlift does not provide a transport mechanism between sidecars.

Tested against Artifactory 7.146.x. The sidecar runs as uid/gid 1030 by default to match the artifactory user; platforms that assign arbitrary UIDs are also supported, see [Restricted security contexts](#restricted-security-contexts).

## Architecture

```
   Source side (Sender)                                        Destination side (Receiver)
 ┌────────────────────────────────────────┐                  ┌────────────────────────────────────────┐
 │         Artifactory Namespace          │                  │         Artifactory Namespace          │
 │ ┌───────────────┐   ┌───────────────┐  │                  │ ┌───────────────┐    ┌──────────────┐  │
 │ │Artifactory Pod│◀──┤Airlift Sidecar│  │                  │ │Airlift Sidecar│───▶│Atifactory Pod│  │
 │ │               │   │   (Sender)    │──┼───▶ Transport ───┼─▶   (Receiver)  │    │              │  │
 │ └───────────────┘   └───────────────┘  │                  │ └───────────────┘    └──────────────┘  │
 │ - Shared filestore PVC                 │                  │ - Shared filestore PVC                 │
 │ - State PVC + spool PVC                │                  │ - State PVC + spool PVC                │
 └────────────────────────────────────────┘                  └────────────────────────────────────────┘
```

### What the sender does each cycle

1. `POST /api/export/system` with `excludeContent=true,includeMetadata=true,createArchive=false`. Artifactory writes the export tree to a path on the state PVC, nested inside a timestamped subdirectory.
2. Walk the export tree and parse every `*.artifactory-metadata/artifactory-file.xml`, extracting `(repo_key, repo_path, sha1, size)`. Write a sorted JSONL snapshot at `state/snapshots/<cycle_id>.jsonl`.
3. Two-pointer set-diff against the previous snapshot to compute new `(sha1, repo, path)` triples; when `propagate_deletes` is on (default), also compute removed triples (entries in the previous snapshot that are absent from the current one). Removals are skipped on the first cycle after a clean state, since a missing baseline cannot distinguish "deleted on source" from "never seen".
4. For each new sha1, read the raw blob from the binarystore at `<sha1[:2]>/<sha1>`, streaming it straight into the archive. That is a file under `<FILESTORE_ROOT>` for an on-disk filestore (airlift mounts the filestore PVC read-only on the sender side), or an object under the bucket's key prefix for an object-storage backend. A blob that is not there yet is deferred to the next cycle rather than skipped permanently; see "Object-storage binarystores".
5. Build `<cycle_id>.tar.zst` containing `manifest.json` (with `entries[]` for additions and `removed[]` for deletions), the full `metadata/` subtree from the export, and `blobs/<aa>/<sha1>` entries for each added sha1. Removed entries ship as metadata records only; their blobs are never re-included, because the same sha1 may still be in use elsewhere. The archive is finalised atomically (`.partial` → `os.rename`). When the cycle's cumulative raw blob bytes exceed `max_archive_bytes`, the diff is split into multiple archives named `<cycle_id>-cNNN.tar.zst`; see "Chunked deltas and spool backpressure" below.
6. Prune `state/snapshots/` under a tiered GFS retention policy (hours/days/months); prune `state/exports/` past the configured history depth.

### What the receiver does each cycle

1. List `spool/*.tar.zst` in lexical (and time) order.
2. Skip any cycle_id already present in `state/processed.jsonl`.
3. Extract the next archive to `state/import/<cycle_id>/` (must be on the state PVC, not under the artifactory data dir; the import API rejects paths under `/var/opt/jfrog/artifactory/...`).
4. Write each blob from `blobs/<aa>/<sha1>` into the binarystore. For an on-disk filestore that is a write to `<FILESTORE_ROOT>/<aa>/<sha1>.tmp-<pid>`, `fsync`, `rename`, `chown` to `artifactory_uid:artifactory_gid` (skipped when the process lacks permission, e.g. when running under an arbitrary UID), `chmod 0640`. For object storage it is an upload to the bucket, switching to multipart / staged blocks for large blobs. Existing sha1s are skipped either way (the binarystore is content-addressed and idempotent).
5. `POST /api/import/repositories?path=<extract_dir>/metadata/repositories&verbose=1`. Artifactory returns 200 even when individual repos fail; the receiver scans the verbose response body for `500 :`, `400 :`, `404 :`, and `Error` lines and records them as per-repo failures.
6. For each record in the manifest's `removed[]`, issue `DELETE /<repo>/<path>` against the destination. A 404 is treated as success (the artifact is already gone, which is the desired state) but recorded in `delete_failures[]`. Imports run before deletions so a sha1 that both moved away from one path and reappeared at another in the same cycle survives.
7. Append `{cycle_id, status, blob_count, total_bytes, repos, failures, deleted_count, delete_failures, processed_at}` to `state/processed.jsonl` and move the archive to `spool/.done/`. For chunked cycles, non-final chunks record `status: "blob-staged"` and skip the import and delete steps; the final chunk waits in spool until every earlier chunk for the same `parent_cycle_id` has been recorded, then runs import and deletes against the now-complete blob set.

### Failure semantics

Adding artifacts is idempotent: sha1s already in the filestore are skipped, the cycle_id is recorded in `processed.jsonl`, and a missing predecessor (`prev_cycle_id` not yet processed) is logged but does not block; wider future diffs converge state. Deleting artifacts is also idempotent in the desired-state sense, since a missing target returns 404 and the receiver counts that as success. A partial import (some repos failed) records `status: "partial"` in the ledger and leaves the archive in `.done/` for forensic replay; delete failures land in a separate `delete_failures[]` field and do **not** flip the cycle to `partial`, because the desired post-state on the destination is "this artifact is gone" and a 404 already satisfies it.

## Repo layout

```
src/artifactory_airlift/    Python package (sender + receiver share the same image)
helm/                       ConfigMap + Secret + state/spool PVCs (does NOT deploy the sidecar)
tests/unit/                 offline unit tests
tests/e2e/                  live-cluster tests, gated on E2E=1
Dockerfile                  multi-stage build, runs as uid 1030 (arbitrary-UID safe)
```

`AIRLIFT_MODE=sender|receiver` selects behaviour. Both sides run the same image.

## Prerequisites

- Two JFrog Artifactory instances deployed to Kubernetes. Only the [jfrog/artifactory](https://github.com/jfrog/charts) Helm chart implementation is covered.
- The container image is published at `ghcr.io/lonk42/artifactory-airlift` (semver tags plus `latest`), and the Helm chart at `oci://ghcr.io/lonk42/charts/artifactory-airlift`. Both are public, so no registry credential is needed. Building your own is still supported; see below.
- An admin-scoped access token on each Artifactory (username + password also works as a fallback). See [Authentication](#authentication) below.
- Every repository that exists on the source must also exist on the destination before the first sync. Repository *definitions* are not propagated by this tool.
- If either instance stores blobs in object storage rather than on disk, the sidecar needs credentials for that bucket or container. Airlift works out everything else itself; see [Object-storage binarystores](#object-storage-binarystores).

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
  --set artifactory.token=<admin-scoped-access-token>
```

The chart is also published as an OCI artifact, so you can install it without
cloning this repo by swapping `./helm` for the registry reference and pinning a
`--version`:

```sh
helm install artifactory-airlift oci://ghcr.io/lonk42/charts/artifactory-airlift \
  --version <chart-version> \
  --namespace <source-namespace> \
  --set mode=sender \
  --set instanceName=<source-name> \
  --set image.repository=<your-registry>/artifactory-airlift \
  --set image.tag=<version> \
  --set artifactory.token=<admin-scoped-access-token>
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
          - name: AIRLIFT_ARTIFACTORY_TOKEN
            valueFrom: { secretKeyRef: { name: artifactory-airlift-token, key: token } }
        volumeMounts:
          - { name: airlift-state, mountPath: /var/airlift/state }
          - { name: airlift-spool, mountPath: /var/airlift/spool }
          - { name: airlift-config, mountPath: /etc/airlift }
          - { name: artifactory-volume, mountPath: /var/opt/jfrog/artifactory }
        # Omit this if your platform assigns container UIDs itself; see
        # the restricted security contexts section below.
        securityContext: { runAsUser: 1030, runAsGroup: 1030 }
```

The state PVC must be mounted in **both** the artifactory container and the airlift container; the sender writes the system export there for the artifactory process to read, and the receiver writes the extracted import tree there for the artifactory process to read. The jfrog chart wires this automatically because the mount lives in `customVolumeMounts`.

The `artifactory-volume` mount on the sidecar is what gives the sender read access to the filestore (and the receiver write access). It's the same PVC the artifactory container uses.

### 3. Pre-create destination repositories

Every repo that exists on the source must also exist on the destination before the first sync. The batch import API returns `500 : The directory <repo> does not match any repository key.` when it can't find a matching repo on the destination. Create them via the Artifactory UI, `PUT /api/repositories/<key>`, or your provisioning tooling.

TODO: This is in-scope for a future feature

### 4. Transport

The sender writes finalised archives to `/var/airlift/spool/*.tar.zst`. The receiver reads from the same path on its side. Moving archives between the two PVCs is **not** in scope. (For testing use manual `kubectl cp`). The archives are content-addressed and idempotent; replay is safe.

## Consuming the chart as a dependency (umbrella chart / ArgoCD)

Because the chart is published to OCI (see the install-from-OCI note in step 1), you can pull it in as a subchart of your own umbrella chart rather than installing it standalone. This is the tidiest way to deploy airlift alongside Artifactory through ArgoCD: a Helm `dependencies:` entry can point at an HTTP Helm repo, an `oci://` reference, or a local `file://` path, but it cannot reference a git repo + path, which is why the chart needs to be published.

Declare both Artifactory and airlift as dependencies in a wrapper `Chart.yaml`:

```yaml
apiVersion: v2
name: artifactory-stack
version: 0.1.0
dependencies:
  - name: artifactory
    version: "<jfrog chart version>"
    repository: https://charts.jfrog.io
  - name: artifactory-airlift
    version: "<chart-version>"
    repository: oci://ghcr.io/lonk42/charts
```

Run `helm dependency update` to vendor the charts. In the umbrella `values.yaml`, airlift's values are namespaced under the subchart key (`artifactory-airlift:`) and the jfrog `customSidecarContainers` block goes under `artifactory:` exactly as in step 2 above. The sidecar block references the airlift resource names (the ConfigMap, Secret, and PVCs), which stay stable when the chart is rendered as a subchart.

Point a single ArgoCD `Application` at the umbrella chart in your gitops repo. `repoURL` + `path` is valid here because the umbrella chart is git-hosted; airlift is resolved as an OCI dependency by the repo-server during `helm dependency build`, so it does not need its own Application:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
spec:
  source:
    repoURL: https://github.com/<you>/<gitops>
    path: charts/artifactory-stack
    targetRevision: main
    helm:
      valueFiles: [values.yaml]
```

ArgoCD must have OCI Helm support enabled (the default in recent 2.x releases); if you make the chart package private, register `ghcr.io` as an OCI-enabled Helm repository credential in ArgoCD so the repo-server can pull it. If you would rather not maintain a wrapper chart, an ArgoCD multi-source `Application` is an alternative: one source is the airlift chart from `oci://ghcr.io/lonk42/charts` and another is a git-hosted values file that overrides it.

## Restricted security contexts

Some Kubernetes distributions enforce an admission policy that rejects pods pinning `runAsUser` to a UID outside a per-namespace allowed range, and instead assign each container an arbitrary non-root UID with GID 0. On those platforms the `runAsUser: 1030` in the sidecar snippet above fails admission. To run under a platform-assigned UID:

- **Drop the `securityContext` from the sidecar block** (if you template the snippet from this chart, set `sidecar.securityContext: null` in the values, or pass `--set sidecar.securityContext=null`; an empty map `{}` would merge with the chart defaults rather than clear them). The platform then assigns the sidecar an arbitrary non-root UID with GID 0; the image's writable directories are group-0 owned, so this works without further changes.
- **Unset the uid/gid pins in the jfrog/artifactory chart too.** That chart also defaults to uid/gid 1030 in its pod `securityContext`. With both unset, both containers in the pod run as the same assigned UID, so filestore blobs written by either side stay readable by the other, and volume permissions are handled by the assigned `fsGroup`.
- **No airlift config changes are needed.** The receiver's chown-to-`artifactory_uid` is skipped automatically when the process lacks permission (logged at debug as `filestore.chown_skipped`); blobs end up owned by the pod's assigned UID, which is the UID Artifactory itself runs as.

Alternatively, if your platform can grant the workload an exemption that admits pinned non-root UIDs, you can keep uid/gid 1030 on both containers as-is. The unpinned route above is recommended since it needs no extra privileges.

## Authentication

Airlift accepts either a bearer access token *or* a username + password pair. The token can be given directly or read from a file. The resolution order is basic auth, then `artifactory_token_file`, then `artifactory_token`, so to use a token leave username and password empty.

**Recommendation: use an admin-scoped access token.** Generate it once through the Artifactory UI (Administration -> User Management -> Access Tokens) for a user with admin rights, ideally a dedicated service user so the credential is independently revocable, with no expiry. Airlift needs admin-level access because it drives `/api/export/system` on the source and `/api/import/repositories` on the destination; a token scoped to `applied-permissions/admin` (or any token whose subject is an admin user) authorises every endpoint airlift calls. Put it in `artifactory.token` and the chart wires it into the sidecar as `AIRLIFT_ARTIFACTORY_TOKEN`.

A token cannot be bootstrapped programmatically from username + password: the legacy `POST /api/security/token` endpoint only mints `member-of-groups:*`-scoped tokens and refuses the `applied-permissions/admin` scope name, while `/access/api/v1/tokens` (the endpoint that can mint admin scope) rejects both basic auth and legacy tokens. This only affects self-bootstrapping; generating the token in the UI and supplying it directly is the supported path.

Username + password basic auth is still supported as a fallback (set `artifactory.username` and `artifactory.password`) if you would rather not manage a token.

The Helm chart writes the credentials into a Kubernetes Secret named `artifactory-airlift-token` with three keys: `token`, `username`, `password`. The sidecar reads them via the `AIRLIFT_ARTIFACTORY_TOKEN`, `AIRLIFT_ARTIFACTORY_USERNAME`, and `AIRLIFT_ARTIFACTORY_PASSWORD` env vars.

> Every env var the sidecar consumes starts with `AIRLIFT_`. Bare `ARTIFACTORY_TOKEN` etc. are silently ignored; Pydantic Settings uses `env_prefix="AIRLIFT_"`.

### Trusting a private CA

When Airlift talks to Artifactory over HTTPS with a certificate issued by a private or self-signed CA, hand it the CA to trust. Airlift's HTTP client uses the bundled certifi CA store and does not read `SSL_CERT_FILE` / `SSL_CERT_DIR`, so the CA has to be named explicitly: set `artifactory_ca_cert` (env `AIRLIFT_ARTIFACTORY_CA_CERT`) to the path of a single concatenated PEM bundle file (not a directory) that the container can read.

CA material is public, so the idiomatic home for it on Kubernetes is a ConfigMap; many clusters already expose one (for example the per-namespace `kube-root-ca.crt`, a platform-injected trusted-CA bundle, or a cert-manager trust-manager `Bundle`). Reference it via the chart's `artifactory.caCert.existingConfigMap` (with `key` and `mountPath`); the chart mounts that ConfigMap into the sidecar and points `AIRLIFT_ARTIFACTORY_CA_CERT` at the mounted file for you. The chart does not create the ConfigMap. Leave `existingConfigMap` empty to use the default trust store.

## Configuration reference

The sidecar reads `/etc/airlift/config.yaml` from the mounted ConfigMap and overlays env vars prefixed `AIRLIFT_`. All keys are optional except auth.

### Snapshot retention (GFS)

`state/snapshots/*.jsonl` is the breadcrumb trail used both as the next cycle's diff baseline and as the future basis for backfill. The three `snapshot_retention_*` keys give a grandfather-father-son retention policy: each tier independently keeps the newest snapshot in every non-empty bucket within its wall-clock window from now, and the final keep set is the union across tiers (a single snapshot can satisfy multiple tiers).

Example: `snapshot_retention_hours: 10`, `snapshot_retention_days: 30`, `snapshot_retention_months: 12` retains one snapshot per hour for the last ten hours, one per day for the last thirty days, and one per calendar month for the last twelve months. At least one of the three must be greater than zero; months are real calendar months, not thirty-day windows.

Note that `state/exports/<cycle_id>/` (the raw Artifactory export trees) is retained by count (`history_keep`) and is decoupled from snapshot retention. A six-month-old snapshot kept by the monthly tier has no matching export directory; the snapshot is still useful as a backfill reference because Artifactory's filestore deduplicates by sha1, and the export tree can be regenerated on demand.

### Chunked deltas and spool backpressure

A single cycle's diff is split into multiple archives when its cumulative raw blob bytes exceed `max_archive_bytes` (default `8Gi`; the field takes k8s-style quantities or a plain integer in bytes). This matters in two cases: the very first cycle on a populated source, which sees every blob as "added", and one-off bulk imports on the source that would otherwise produce a single archive larger than the spool PVC.

Chunks of one logical cycle share a `parent_cycle_id` in their manifest and use the filename `<parent_cycle_id>-cNNN.tar.zst` (zero-padded sequence). Only the final chunk (`chunk_seq == chunk_total`) carries the `metadata/` subtree and the `removed[]` list; earlier chunks are blob payloads. The receiver writes blobs from each chunk as it sees them, records `status: "blob-staged"` in `processed.jsonl`, and defers `POST /api/import/repositories` plus deletions until the final chunk arrives and every earlier chunk for the same parent is already recorded. Out-of-order delivery is safe: a final chunk that arrives before its predecessors stays in spool and is re-evaluated on the next receiver tick.

Single-chunk cycles keep the legacy `<cycle_id>.tar.zst` filename and emit one `sender.archive_finalized` event; multi-chunk cycles log `sender.cycle_chunked` once at the top and `sender.chunk_finalized` per chunk. Set `max_archive_bytes: 0` to disable chunking and restore the pre-0.7 single-archive behaviour.

The sender also enforces a **"one delta in flight" gate** at the start of every cycle: if any finalised `*.tar.zst` archive from a prior cycle is still in spool waiting for the transport, the cycle is skipped without taking a fresh export. This keeps each delta a clean diff from the last cycle the transport actually consumed, prevents stale snapshot and export-tree orphans from piling on the state PVC during prolonged transport stalls, and gives operators a single grep target (`sender.cycle_skipped_pending`) for "transport is behind".

**Startup sweep.** A SIGKILL during `archive.build` (OOM, node drain, container restart mid-build) can leave a half-written `<cycle_id>.tar.zst.partial` file and an empty `spool/.staging/<cycle_id>/` directory. Neither is picked up by the receiver's `*.tar.zst` glob and `.partial` files do not trip the pending-gate, but they sit on the spool PVC indefinitely. Both sender and receiver call `archive.sweep_orphan_partials()` immediately after creating the spool directory on process start; any partials and staging subdirectories present at that moment are deleted. A non-zero sweep logs `sender.startup_sweep` / `receiver.startup_sweep` with the counts; a clean tree is silent.

`spool_min_free_bytes` (default `2Gi`, same quantity syntax) is the per-chunk safety threshold and remains as defence-in-depth. Before each chunk build the sender requires `free_space(spool) >= spool_min_free_bytes + projected_chunk_bytes` (projection = raw blob sum + 256 MiB framing/metadata overhead). When the check fails, the sender logs `sender.spool_backpressure`, removes any partial chunks already on disk for the in-flight parent, and returns without advancing the cursor. The next cycle re-emits the same diff against the same snapshot baseline, so backpressure produces clean repeatable abort cycles instead of half-shipped chunk sets. Operator action when this fires: grow the spool PVC, lower `max_archive_bytes`, or wait for the transport to drain pending archives. The retry cadence is just `cycle_seconds`; there is no separate retry knob.

### Repo allowlist

By default the sender mirrors every repo on the source (less the JFrog
system/BuildInfo repos it always drops). To cherry-pick a subset instead, set
`included_repos` to a comma-separated list of repo keys; only those repos then
enter the snapshot, diff, archive, and the receiver's import path. An empty
list (the default) means sync everything, so existing deployments are
unaffected.

The allowlist narrows; it does not override the exclusions. A repo is synced
only when it is listed in `included_repos` **and** not caught by the
`excluded_repos` / `excluded_package_types` denylists, so listing a system repo
here will not force it through. Filtering happens at snapshot time on the
sender, so the receiver needs no configuration.

Set it via the env var on the sidecar (`AIRLIFT_INCLUDED_REPOS=foo,bar`) or
declaratively through the Helm `includedRepos` value:

```yaml
includedRepos:
  - airlift-rpm-local
  - airlift-npm-local
```

Watch the sender log for `Allowlist active: syncing only N repo(s): [...]` at
cycle start to confirm it is in effect.

### Object-storage binarystores

Airlift reads and writes artifact bytes in Artifactory's binarystore directly.
Three provider families are supported: the default on-disk filestore,
S3-compatible object storage (Ceph RADOS Gateway, MinIO, AWS S3), and Azure
Blob Storage. All three key blobs the same way, `<prefix>/<sha1[:2]>/<sha1>`,
which is what keeps the storage backend a narrow seam.

**The backend is detected, not configured.** At startup each side parses
Artifactory's own `binarystore.xml` (by default
`/var/opt/jfrog/artifactory/etc/artifactory/binarystore.xml`, already visible
because the sidecar mounts the artifactory volume) and works out the bucket or
container, endpoint, key prefix, and region. Confirm what it picked from the
first log line:

```
Binarystore backend: s3 (s3 bucket artifactory-a at http://minio:9000 (prefix 'artifactory/filestore')).
```

**The two sides are independent.** Each sidecar reads its own instance's
config, so a sender on Azure Blob feeding a receiver on an S3-compatible store
needs no special handling.

**Credentials come from the XML when it still has them, otherwise from
configuration.** Artifactory rewrites `<identity>`, `<credential>` and
`<accountKey>` encrypted once it takes ownership of the config, as an opaque
`<keyId>.<algorithm>.<ciphertext>` envelope airlift cannot read. Encrypted
values are treated as absent, and airlift falls back to
`binarystore_access_key` / `binarystore_secret_key` (S3) or
`binarystore_account_key` (Azure), mounted from a Secret via the chart's
`binarystore.existingSecret`. Explicit configuration always wins when both are
present.

The tidier option, where your Artifactory chart offers it, is to read
`binarystore.xml` from the Secret that chart renders rather than from the file
on Artifactory's own disk. The rendered copy still holds plaintext credentials,
because Artifactory only encrypts them in its own file once it takes ownership
on first boot, so one mount supplies the bucket, prefix, endpoint and
credentials together and no credential needs restating in your values. Set
`binarystore.configFrom.existingSecret` and the chart wires the mount, the
`AIRLIFT_BINARYSTORE_CONFIG` env var, and a projection of just the
`binarystore.xml` key, since such Secrets often also carry the instance master
and join keys.

**Unsupported chains fail loudly.** Sharded and redundant chains
(`sharding-cluster`, `double-shards`) and `full-db` providers raise at startup
naming the provider rather than guessing, because guessing wrong is silent:
blobs would be written where Artifactory never looks for them. If detection
misfires, `binarystore_provider` overrides it: `filesystem` forces the on-disk
path, while `s3` and `azure` assert the detected backend matches.

**Blobs that have not landed yet are retried, not lost.** A chain with an
`eventual` provider uploads asynchronously, so an artifact can appear in the
export metadata before its bytes reach the bucket. Those entries are left out
of the archive manifest and held back from the snapshot that becomes the next
baseline, so the following cycle re-detects them as added and ships them once
they are readable. The sender logs `Deferred N entr(ies) ...` when this
happens; a steady trickle is normal on a busy source, a growing count is not.

Large blobs upload as S3 multipart or Azure staged blocks above
`binarystore_multipart_threshold`. A single S3 PUT is capped at 5 GiB, so this
is what allows large artifacts through at all.

| Key (yaml)              | Env                              | Default                                                  | Description                                                                                                                  |
| ----------------------- | -------------------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `mode`                  | `AIRLIFT_MODE`                   | `sender`                                                 | Which loop to run: `sender` exports + diffs + spools archives; `receiver` ingests archives + writes blobs + imports repos.   |
| `instance_name`         | `AIRLIFT_INSTANCE_NAME`          | `unknown`                                                | Free-form label recorded in archive manifests and log lines. Helpful when more than one source feeds the same destination.   |
| `artifactory_url`       | `AIRLIFT_ARTIFACTORY_URL`        | `http://localhost:8081/artifactory`                      | Base URL of the local Artifactory. As a sidecar this is loopback; override only if Artifactory listens on a non-default port.|
| `artifactory_token`     | `AIRLIFT_ARTIFACTORY_TOKEN`      | `""`                                                     | Bearer access token. Use an admin-scoped token (subject is an admin user); it authorises every endpoint airlift calls, including `/api/export/system` and `/api/import/repositories`. Ignored when basic auth or `artifactory_token_file` is set.|
| `artifactory_token_file`| `AIRLIFT_ARTIFACTORY_TOKEN_FILE` | `""`                                                     | Path to a file holding the bearer token, re-read on every request so a token rotated in place is picked up without a restart. Surrounding whitespace is stripped. Takes precedence over `artifactory_token`; basic auth still wins over both. The chart mounts it for you from `artifactory.tokenFile.existingSecret`.|
| `artifactory_username`  | `AIRLIFT_ARTIFACTORY_USERNAME`   | `""`                                                     | Admin username for basic auth. When both username and password are set, basic auth takes precedence over `artifactory_token`.|
| `artifactory_password`  | `AIRLIFT_ARTIFACTORY_PASSWORD`   | `""`                                                     | Admin password paired with `artifactory_username`. Inject via a Secret; the chart writes this into `artifactory-airlift-token`.|
| `artifactory_ca_cert`   | `AIRLIFT_ARTIFACTORY_CA_CERT`    | `""`                                                     | Path to a single PEM CA bundle file used to verify Artifactory's TLS certificate. Empty uses the bundled certifi store. Set it to trust a private/self-signed CA; the HTTP client does not read `SSL_CERT_FILE`, so the CA must be named here and the file mounted into the container. See "Trusting a private CA".|
| `cycle_seconds`         | `AIRLIFT_CYCLE_SECONDS`          | `300`                                                    | Seconds between cycles. Sender: time between exports/diffs. Receiver: poll interval for new archives in the spool dir.       |
| `propagate_deletes`     | `AIRLIFT_PROPAGATE_DELETES`      | `true`                                                   | Sender-only. When true, each cycle's manifest includes a `removed[]` list of artifacts present in the previous snapshot but absent from the current one; the receiver issues `DELETE` calls to converge. Cold-start cycles (no previous snapshot) skip removal emission. Set false to fall back to additive-only behaviour. |
| `included_repos`        | `AIRLIFT_INCLUDED_REPOS`         | `[]` (all repos)                                         | Sender-only allowlist. Comma-separated repo keys; when empty every repo is synced. When set, only the listed repos enter the snapshot, diff, and archive. The system-repo exclusions still apply on top, so a repo syncs only if it is listed here and not excluded. |
| `history_keep`          | `AIRLIFT_HISTORY_KEEP`           | `24`                                                     | Sender-only. Number of raw export trees to retain under `state/exports/` before pruning the oldest. Snapshot baselines use the GFS retention keys below.|
| `done_keep_hours`       | `AIRLIFT_DONE_KEEP_HOURS`        | `72`                                                     | Receiver-only. How long to retain processed archives under `spool/.done/` before deleting them. Set `0` to keep forever.     |
| `max_archive_bytes`     | `AIRLIFT_MAX_ARCHIVE_BYTES`      | `8Gi`                                                    | Sender-only. Per-archive raw blob-byte budget. Accepts a k8s-style quantity (`8Gi`, `512Mi`, `1G`) or a plain integer. When a cycle's diff exceeds this, the diff is split into multiple archives named `<cycle_id>-cNNN.tar.zst`; only the final chunk carries the metadata tree and `removed[]`. Set `0` to disable chunking. |
| `spool_min_free_bytes`  | `AIRLIFT_SPOOL_MIN_FREE_BYTES`   | `2Gi`                                                    | Sender-only. Free-space safety threshold on the spool volume. Same quantity syntax as `max_archive_bytes`. Each chunk requires `free >= spool_min_free_bytes + projected chunk size` before it is built; otherwise the cycle aborts cleanly without advancing the cursor. |
| `snapshot_retention_hours`  | `AIRLIFT_SNAPSHOT_RETENTION_HOURS`  | `0` | Sender-only. GFS tier: keep the newest `state/snapshots/*.jsonl` per hour-bucket for the last N hour-buckets (wall clock).        |
| `snapshot_retention_days`   | `AIRLIFT_SNAPSHOT_RETENTION_DAYS`   | `3` | Sender-only. GFS tier: keep the newest `state/snapshots/*.jsonl` per day-bucket for the last N day-buckets (UTC).                  |
| `snapshot_retention_months` | `AIRLIFT_SNAPSHOT_RETENTION_MONTHS` | `0` | Sender-only. GFS tier: keep the newest `state/snapshots/*.jsonl` per calendar-month bucket for the last N months. Real calendar months. |
| `filestore_root`        | `AIRLIFT_FILESTORE_ROOT`         | `/var/opt/jfrog/artifactory/data/artifactory/filestore`  | Path to Artifactory's on-disk binarystore. Sender reads blobs by sha1; receiver writes blobs into `<root>/<sha1[:2]>/<sha1>`. Used only when the binarystore is file-system backed; object-storage backends address the bucket instead. |
| `binarystore_config`    | `AIRLIFT_BINARYSTORE_CONFIG`     | `/var/opt/jfrog/artifactory/etc/artifactory/binarystore.xml` | Artifactory's binarystore descriptor, parsed at startup to detect which backend holds the blobs. Absent or unparseable falls back to `filestore_root`, which is the pre-object-storage behaviour. See "Object-storage binarystores". |
| `binarystore_provider`  | `AIRLIFT_BINARYSTORE_PROVIDER`   | `auto`                                                   | Backend override. `auto` trusts `binarystore_config`. `filesystem` forces the on-disk path. `s3` and `azure` assert the detected backend is the expected one and fail at startup otherwise, which is worth setting once the backend is known: guessing wrong fails silently. |
| `binarystore_access_key`| `AIRLIFT_BINARYSTORE_ACCESS_KEY` | `""`                                                     | Access key for an S3-compatible binarystore. Required when the resolved backend is S3 and `binarystore_config` does not hold plaintext credentials, which is the case for the file on Artifactory's own disk (it encrypts them in place). Takes precedence over the XML when both are present. |
| `binarystore_secret_key`| `AIRLIFT_BINARYSTORE_SECRET_KEY` | `""`                                                     | Secret key paired with `binarystore_access_key`. Inject via a Secret; the chart mounts it from `binarystore.existingSecret`. |
| `binarystore_account_key`| `AIRLIFT_BINARYSTORE_ACCOUNT_KEY` | `""`                                                   | Shared key for an Azure Blob binarystore. Required when the resolved backend is Azure. The account name and container come from `binarystore.xml`. |
| `binarystore_ca_cert`   | `AIRLIFT_BINARYSTORE_CA_CERT`    | `""`                                                     | Path to a PEM CA bundle used to verify the object-storage endpoint's TLS certificate. Separate from `artifactory_ca_cert` because the object store commonly sits behind a different CA. Empty uses the bundled certifi store. |
| `binarystore_multipart_threshold` | `AIRLIFT_BINARYSTORE_MULTIPART_THRESHOLD` | `256Mi`                        | Blobs at or above this size upload as S3 multipart / Azure staged blocks rather than a single request. Same quantity syntax as `max_archive_bytes`. A single S3 PUT is capped at 5 GiB, so this is what lets large artifacts through. |
| `artifactory_tmp`       | `AIRLIFT_ARTIFACTORY_TMP`        | `/var/opt/jfrog/artifactory/data/artifactory/tmp`        | Artifactory's tmp dir. Reserved for future use; currently informational.                                                     |
| `state_dir`             | `AIRLIFT_STATE_DIR`              | `/var/airlift/state`                                     | Durable per-side state (snapshots, cursor, processed ledger, lockfile, extracted import trees). Must be on a PVC.            |
| `spool_dir`             | `AIRLIFT_SPOOL_DIR`              | `/var/airlift/spool`                                     | Where finalised `*.tar.zst` archives land (sender) and where they're picked up from (receiver). Never NFS; tearing on fsync. |
| `artifactory_uid`       | `AIRLIFT_ARTIFACTORY_UID`        | `1030`                                                   | UID the receiver chowns blobs to when placing them in the filestore. Must match the artifactory process's UID. The chown is skipped (debug log `filestore.chown_skipped`) when the process lacks permission, as under platform-assigned arbitrary UIDs. |
| `artifactory_gid`       | `AIRLIFT_ARTIFACTORY_GID`        | `1030`                                                   | GID counterpart of `artifactory_uid`.                                                                                        |
| `log_level`             | `AIRLIFT_LOG_LEVEL`              | `INFO`                                                   | Structlog level: `DEBUG`, `INFO`, `WARNING`, `ERROR`.                                                                        |
| `log_format` (env only) | `AIRLIFT_LOG_FORMAT`             | `console`                                                | `console` emits one human-readable line per event (`YYYY-MM-DD HH:MM:SS LEVEL component cycle=… message [k=v …]`). Set to `json` for the original structlog JSON output. |

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

**`401` on `/api/export/system` or `/api/import/repositories`.** Your token's subject is not an admin user. Mint an admin-scoped token (`applied-permissions/admin`, or any token whose subject has admin rights) through the Artifactory UI and put it in `artifactory.token`, or fall back to basic auth with an admin account (`artifactory.username` / `artifactory.password`).

**Sender's snapshot count is always 0.** Either (a) the export path isn't being read at the right subdirectory; Artifactory writes the export tree into a nested `<timestamp>/` subdir inside the path you give it, and the sender descends into this automatically; or (b) the metadata file isn't named `artifactory-file.xml` on your Artifactory version. The parser is `src/artifactory_airlift/export_unpacker.py:_parse_fileinfo`.

**Receiver records `500 : The directory <name> does not match any repository key.` in `processed.jsonl`.** The named repo exists on the source but not on the destination. For JFrog system repos (`jfrog-usage-logs`, `artifactory-build-info`, `auth-tokens`, etc.) this is benign; they're per-instance and shouldn't be synced. For your own repos, create them on the destination first.

**Receiver records `500` referencing `/api/repositories/<repo>/import`.** You're on an old version of airlift that called the broken per-repo endpoint. v0.3.0+ uses the batch endpoint `/api/import/repositories`, which works on 7.146.x. Upgrade the image.

**Sender refuses to start with empty token despite the env being set.** The env var name must start with `AIRLIFT_`. Bare `ARTIFACTORY_TOKEN` is silently ignored; Pydantic Settings uses `env_prefix="AIRLIFT_"`.

**Import call rejected with `Invalid Import Directory`.** The path passed to the import API must not be under `/var/opt/jfrog/artifactory/...`. The receiver extracts to `state_dir/import/<cycle_id>/` (under the state PVC) for this reason.

## Limitations

- **Artifactory version**: tested against 7.146.x. The system-export and batch-import API shapes are version-specific; other versions may need tweaks in `artifactory_client.py` and `export_unpacker.py`.
- **Repository definitions are not synced.** Pre-create all repos on the destination.
- **Binarystore providers**: file-system, S3-compatible, and Azure Blob are supported (see "Object-storage binarystores"). Sharded and redundant chains (`sharding-cluster`, `double-shards`) and database-backed providers (`full-db`) are not; airlift refuses to start on those rather than guessing a key layout.
- **Transport is out of scope.** Moving archives between the two spool PVCs is whatever your air-gap mechanism is.
- **`/api/import/system` overwrite caveat.** This tool does *not* call `/api/import/system`; it uses `/api/import/repositories`, which is non-destructive for instance config. If you change the receiver to use `/api/import/system` as a fallback, be aware that endpoint overwrites the destination's config (security data, joinKey, masterKey) with the source's.

## TODO

- **Create destination repositories.** They must currently be created manually; this will require additional API permissions and metadata.
- **Metadata snapshot de-duplication.** Metadata exports should be ignored if there are no differences, to reduce unnecessary retention.
- **Hooks for external alerting.** On partial and full failures you are gonna wanna know
- **Periodic full-state archives.** Add a cadence (every N cycles, or on a cron) where the sender emits a full snapshot as `added=[everything]` with an authoritative flag, so the receiver can resync from any gap shorter than the interval. Without this, a lost archive leaves both adds and deletes unreplayed for as long as the source state is unchanged.
- **Operator doc for on-demand full-state archives.** Document the procedure for generating a one-off full-state archive (clearing the sender cursor/snapshots to force a cold start on the next cycle, or a dedicated CLI subcommand) so operators have a recovery path when divergence is suspected without waiting for the periodic cadence.
