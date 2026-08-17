# Artifactory Airlift

One-way delta sync between two JFrog Artifactory instances separated by an air gap.
Airlift runs as a sidecar in the Artifactory pod on each side: the sender works out what changed and writes a self-contained archive, the receiver ingests archives and converges its instance to match.
Moving archives across the gap is left to whatever mechanism the gap allows.

The destination converges on the source, deletions included.
Repository contents are mirrored; repository definitions are not.

Tested against Artifactory 7.146.x.

```
   Source side (sender)                                       Destination side (receiver)
 ┌────────────────────────────────────────┐                  ┌────────────────────────────────────────┐
 │         Artifactory namespace          │                  │         Artifactory namespace          │
 │ ┌───────────────┐   ┌───────────────┐  │                  │ ┌───────────────┐    ┌───────────────┐ │
 │ │Artifactory pod│◀──┤Airlift sidecar│  │                  │ │Airlift sidecar│───▶│Artifactory pod│ │
 │ │               │   │   (sender)    │──┼───▶ Transport ───┼─▶  (receiver)   │    │               │ │
 │ └───────────────┘   └───────────────┘  │                  │ └───────────────┘    └───────────────┘ │
 │ - Shared binarystore                   │                  │ - Shared binarystore                   │
 │ - State PVC + spool PVC                │                  │ - State PVC + spool PVC                │
 └────────────────────────────────────────┘                  └────────────────────────────────────────┘
```

## What it does

- **Cost proportional to the change, not to the instance.**
  A cycle enumerates the source with one AQL query and ships only the artifacts that changed, with a metadata tree built for those artifacts alone.
  [Detail](docs/architecture.md#what-the-sender-does-each-cycle)
- **Full metadata fidelity.**
  Artifacts arrive with their original checksums, properties and timestamps, because the destination registers them through Artifactory's own import path rather than a re-upload.
  [Detail](docs/architecture.md#metadata-synthesis)
- **Deletion propagation**, with a brake that refuses any cycle removing more than a configurable fraction of the mirror.
  [Detail](docs/architecture.md#the-deletion-brake)
- **Object-storage binarystores.**
  On-disk filestore, S3-compatible stores and Azure Blob, detected from Artifactory's own `binarystore.xml` rather than configured.
  [Detail](docs/binarystore.md)
- **Azure workload identity.**
  Where the platform provides an identity, no storage credential is configured.
  [Detail](docs/binarystore.md#azure-without-a-key)
- **Repository scoping**, by allowlist, by key, or by package type.
  [Detail](docs/configuration.md#repository-scope)
- **Deltas chunked to fit the spool volume**, with backpressure when it fills and an ordering guarantee so a partial chunk set is never applied.
  [Detail](docs/architecture.md#chunked-deltas)
- **Idempotent replay.**
  Archives are content-addressed; reprocessing one changes nothing.
  [Detail](docs/architecture.md#failure-semantics)
- **A rotating token file** and **private CA trust**, for environments that need them.
  [Detail](docs/configuration.md#authentication)
- **No exit path that can take Artifactory down.**
  A sidecar that crashloops drops the whole pod out of its Service, so airlift idles on errors instead.
  [Detail](docs/architecture.md#failure-semantics)

## Requirements

- Two Artifactory instances on Kubernetes, deployed with the [jfrog/artifactory](https://github.com/jfrog/charts) Helm chart.
  That is the only deployment shape covered here.
- An admin-scoped access token on each, or an admin username and password.
  [Detail](docs/configuration.md#authentication)
- Every repository on the source already created on the destination.
- Credentials for the object store, if the binarystore is not on disk and Artifactory's own configuration has them encrypted.
  [Detail](docs/binarystore.md#credentials)

The image is published at `ghcr.io/lonk42/artifactory-airlift` and the chart at `oci://ghcr.io/lonk42/charts/artifactory-airlift`.
Both are public.

## Quick start

Install the supporting resources into each Artifactory's namespace, then inject the sidecar through the jfrog chart.

```sh
helm install artifactory-airlift oci://ghcr.io/lonk42/charts/artifactory-airlift \
  --version <chart-version> \
  --namespace <source-namespace> \
  --set mode=sender \
  --set instanceName=<source-name> \
  --set image.repository=ghcr.io/lonk42/artifactory-airlift \
  --set image.tag=<version> \
  --set artifactory.token=<admin-scoped-access-token>
```

Repeat with `mode=receiver` on the destination.
The chart ships a ConfigMap, a Secret and the state and spool PVCs; it does **not** deploy the sidecar, because the sidecar has to live in the Artifactory pod.
`helm install artifactory-airlift ./helm --dry-run` prints the block to paste into the jfrog chart's `customSidecarContainers`, `customVolumes` and `customVolumeMounts`.

Three things bite here, all covered in [the deployment guide](docs/deployment.md):
the state PVC has to be mounted in both containers, destination repositories have to exist before the first sync, and the chart creates no volumes of its own, so a `mountPath` in the values and the volume that backs it can silently disagree.

## Configuration

The sidecar reads `/etc/airlift/config.yaml` from the mounted ConfigMap and overlays environment variables prefixed `AIRLIFT_`, which take precedence.
The settings that come up most often:

| Key | Default | Effect |
| --- | --- | --- |
| `mode` | `sender` | Which loop to run. Both sides run the same image. |
| `cycle_seconds` | `300` | Sender: time between enumerations. Receiver: spool poll interval. |
| `propagate_deletes` | `true` | Whether deletions on the source are applied to the destination. |
| `max_delete_fraction` | `0.05` | Refuse a cycle that would delete more than this share of the mirror. |
| `included_repos` | `[]` (all) | Restrict the sync to named repositories. |
| `max_archive_bytes` | `8Gi` | Split a cycle into several archives above this many raw blob bytes. |
| `binarystore_provider` | `auto` | Set it once the backend is known, so a misdetection fails at startup instead of writing to local disk. |

Every key, with defaults and environment names, is in the [configuration reference](docs/configuration.md#reference).

## Transport

The sender writes finalised archives to `/var/airlift/spool/*.tar.zst`.
The receiver reads the same path on its side.
What carries them between the two volumes is out of scope: that step is the air gap.

Deliver archives in lexical order, which for chunked cycles is the order the receiver needs.
Replay is safe.

Until the transport collects the last delta the sender skips its cycles rather than piling up work, logging `sender.cycle_skipped_pending`.
[Detail](docs/architecture.md#one-delta-in-flight)

## Limitations

- **Repository definitions are not synced.**
  Repositories must be pre-created on the destination.
  Artifacts for a missing repository fail to import and the cycle is recorded `partial`.
- **Sharded, redundant and database-backed binarystores are unsupported.**
  `sharding-cluster`, `double-shards` and `full-db` raise at startup naming the provider rather than guessing a key layout, because guessing wrong writes blobs where Artifactory never looks.
- **A lost archive is not detected.**
  The receiver logs a gap and carries on, and later diffs only converge artifacts that change again.
  Recovery today is a cold start on the sender; periodic full-state archives are on the roadmap below.
- **Multi-valued property ordering is not preserved.**
  Artifactory stores property values as an unordered set.
- **Tested against 7.146.x only.**
  The AQL and import API shapes are version-specific.
- **Download statistics are not mirrored.**
  They describe the instance rather than the artifact.

## Roadmap

- **Create destination repositories**, rather than requiring they exist.
- **Periodic full-state archives**, so the receiver can resync from any gap shorter than the interval, plus the operator procedure for generating one on demand when divergence is suspected.
- **Webhook-driven cycles** for latency, keeping the periodic enumeration as the correctness backbone.
  Webhook delivery is at-most-once, so a dropped deletion event would otherwise strand an artifact on the destination forever.
- **Hooks for external alerting** on partial and failed cycles.
- **Metadata de-duplication**, so unchanged metadata is not retained repeatedly.

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | What each side does per cycle, and the guarantees that follow |
| [docs/deployment.md](docs/deployment.md) | Installing the chart, injecting the sidecar, ArgoCD, restricted security contexts |
| [docs/configuration.md](docs/configuration.md) | Authentication, retention, repository scope, and every setting |
| [docs/binarystore.md](docs/binarystore.md) | Backend detection, credentials, Azure identity, key prefixes |
| [docs/operations.md](docs/operations.md) | Reading the log, resets, scope changes, common issues |

## Repository layout

```
src/artifactory_airlift/    Python package; sender and receiver share one image
helm/                       ConfigMap, Secret and PVCs (does not deploy the sidecar)
tests/unit/                 offline tests
tests/e2e/                  live-cluster tests, gated on E2E=1
Dockerfile                  runs as uid 1030, safe under arbitrary assigned UIDs
```
