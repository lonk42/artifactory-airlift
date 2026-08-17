# Deployment

Airlift runs as a sidecar in the Artifactory pod on each side.
There are two pieces to wire up per instance:

1. **This repo's Helm chart**, which ships the ConfigMap, Secret and PVCs the sidecar reads.
2. **The jfrog/artifactory chart's `customSidecarContainers` field**, which is where the container is injected.
   Supporting volumes and mounts go in `customVolumes` / `customVolumeMounts` alongside it.

The airlift chart has no pod template, so it creates no volumes.
Values such as `binarystore.configFrom.mountPath` declare where a file *will* be; the `customVolumes` entry is what puts it there.
The two can disagree silently, so check the rendered ConfigMap rather than reading the values diff.

## 1. Install the supporting resources

In the same namespace as the Artifactory instance:

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

Swap `oci://...` for `./helm` to install from a checkout.
Use `mode=receiver` on the destination.

## 2. Inject the sidecar

Add this under `artifactory.artifactory.*` in the jfrog/artifactory values.
`helm install artifactory-airlift ./helm --dry-run` prints the same block, filled in from your values.

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
        image: ghcr.io/lonk42/artifactory-airlift:<version>
        env:
          - { name: AIRLIFT_MODE, value: sender }   # "receiver" on the destination
          - name: AIRLIFT_ARTIFACTORY_TOKEN
            valueFrom: { secretKeyRef: { name: artifactory-airlift-token, key: token } }
        volumeMounts:
          - { name: airlift-state, mountPath: /var/airlift/state }
          - { name: airlift-spool, mountPath: /var/airlift/spool }
          - { name: airlift-config, mountPath: /etc/airlift }
          - { name: artifactory-volume, mountPath: /var/opt/jfrog/artifactory }
        # Omit this on a platform that assigns container UIDs itself.
        securityContext: { runAsUser: 1030, runAsGroup: 1030 }
```

The state PVC is mounted in **both** containers.
The receiver writes the extracted import tree there for the Artifactory process to read.
The jfrog chart wires this automatically because the mount is in `customVolumeMounts`.

The `artifactory-volume` mount gives the sidecar access to `binarystore.xml` and, for an on-disk filestore, to the blobs.
It is the same PVC the Artifactory container uses.

Give the sidecar equal `requests` and `limits`.
A sidecar with unequal values demotes the whole pod from `Guaranteed` to `Burstable`, which moves Artifactory up the node's eviction order.

## 3. Pre-create destination repositories

Repository definitions are not propagated.
Every repository on the source has to exist on the destination before the first sync, or its artifacts fail to import with `500 : The directory <repo> does not match any repository key.`

Create them through the Artifactory UI, `PUT /api/repositories/<key>`, or your provisioning tooling.

## 4. Transport

The sender writes finalised archives to `/var/airlift/spool/*.tar.zst` and the receiver reads the same path on its side.
Moving them between the two volumes is out of scope: that mechanism is the air gap.

Archives are content-addressed and idempotent, so replay is safe.
Deliver them in lexical order, which for chunked cycles is also the order the receiver needs.
For testing, `kubectl cp` between the two pods works.

## Consuming the chart as a dependency

The chart is published to OCI, so it can be a subchart of an umbrella chart rather than a standalone install.
This is the tidiest route through ArgoCD, because a Helm `dependencies:` entry can point at an HTTP repo, an `oci://` reference or a local path, but not at a git repo plus a path.

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

Run `helm dependency update` to vendor them.
In the umbrella `values.yaml`, airlift's values sit under the subchart key `artifactory-airlift:`, and the jfrog `customSidecarContainers` block goes under `artifactory:` as above.
The sidecar block references airlift's resource names, which stay stable when the chart renders as a subchart.

Point one ArgoCD `Application` at the umbrella chart.
`repoURL` plus `path` is valid here because the umbrella chart is git-hosted; airlift resolves as an OCI dependency during `helm dependency build`, so it needs no Application of its own:

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

ArgoCD needs OCI Helm support, the default in recent 2.x releases.
If you make the package private, register `ghcr.io` as an OCI-enabled Helm repository credential so the repo-server can pull it.
A multi-source `Application` is the alternative if you would rather not maintain a wrapper chart: one source is the airlift chart from `oci://ghcr.io/lonk42/charts`, another a git-hosted values file.

## Restricted security contexts

Some Kubernetes distributions reject pods that pin `runAsUser` to a UID outside a per-namespace range, assigning each container an arbitrary non-root UID with GID 0 instead.
The `runAsUser: 1030` above fails admission there.

To run under a platform-assigned UID:

- **Drop the `securityContext` from the sidecar block.**
  If you template the snippet from this chart, set `sidecar.securityContext: null`.
  An empty map `{}` merges with the chart defaults rather than clearing them.
  The image's writable directories are group-0 owned, so no further change is needed.
- **Unset the uid/gid pins in the jfrog/artifactory chart too.**
  That chart also defaults to 1030.
  With both unset, the two containers share the assigned UID, so blobs written by either stay readable by the other, and volume permissions follow the assigned `fsGroup`.
- **No airlift configuration changes.**
  The receiver's chown is skipped when the process lacks permission, logged at debug as `filestore.chown_skipped`, and blobs end up owned by the UID Artifactory itself runs as.

If your platform can grant an exemption admitting pinned non-root UIDs, keeping 1030 on both containers also works.
The unpinned route needs no extra privileges.

## Building the image

The published image at `ghcr.io/lonk42/artifactory-airlift` is public and needs no credential.
To build your own:

```sh
docker build -t <your-registry>/artifactory-airlift:<version> .
docker push     <your-registry>/artifactory-airlift:<version>
```

The image runs as uid/gid 1030 to match the Artifactory user, with its writable directories owned by group 0 so an arbitrary assigned UID can write to them.
