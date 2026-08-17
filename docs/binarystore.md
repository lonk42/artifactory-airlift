# Binarystore access

Airlift moves artifact bytes by reading and writing Artifactory's binarystore directly, rather than pulling and pushing them over the REST API.
Three provider families are supported: the on-disk filestore, S3-compatible object storage (MinIO, Ceph RADOS Gateway, AWS S3), and Azure Blob Storage.

All three key blobs the same way, `<prefix>/<sha1[:2]>/<sha1>`, which is what keeps the backend a narrow seam in the code.

## Detection

**The backend is detected, not configured.**
Each side parses Artifactory's own `binarystore.xml`, by default at `/var/opt/jfrog/artifactory/etc/artifactory/binarystore.xml` and already visible because the sidecar mounts the Artifactory volume.
From it airlift works out the bucket or container, endpoint, key prefix and region.

Confirm what it picked from the startup line:

```
Binarystore backend: s3 (s3 bucket artifactory-a at http://minio:9000 (prefix 'artifactory/filestore')).
```

**The two sides are independent.**
Each sidecar reads its own instance's configuration, so a sender on Azure feeding a receiver on S3 needs no special handling.

**Set `binarystore_provider` explicitly once the backend is known.**
Under `auto`, a `binarystore.xml` that cannot be found or parsed falls back to the on-disk filestore and logs the choice at INFO.
On an object-storage instance that means reading and writing local disk where Artifactory never looks, which is a silent no-op rather than an error.
Setting `s3` or `azure` turns that into a startup failure.
`auto` is right only when you do not know.

**Unsupported chains fail loudly.**
Sharded and redundant chains (`sharding-cluster`, `double-shards`) and `full-db` providers raise at startup naming the provider, rather than guessing a key layout.

## Credentials

**They come from the XML when it still has them, otherwise from configuration.**
Artifactory rewrites `<identity>`, `<credential>` and `<accountKey>` into an opaque `<keyId>.<algorithm>.<ciphertext>` envelope once it takes ownership of its config file.
Airlift treats an encrypted value as absent and falls back to `binarystore_access_key` / `binarystore_secret_key` for S3, or `binarystore_account_key` for Azure.
Explicit configuration wins when both are present.

**For a key-based store, prefer the copy your Artifactory chart renders.**
The chart's rendered `binarystore.xml`, usually a key in a unified Secret, is never encrypted, so one mount supplies the bucket, prefix, endpoint *and* credentials with nothing restated in your values.
Set `binarystore.configFrom.existingSecret` and the chart wires the mount, the `AIRLIFT_BINARYSTORE_CONFIG` variable, and a projection of just the `binarystore.xml` key, since such Secrets often also carry the instance master and join keys.

**For a credential-free store, use Artifactory's own copy and mount nothing extra.**
Where the provider authenticates as a platform identity there is nothing for Artifactory to encrypt, both copies are equivalent, and the whole `configFrom` apparatus is overhead.
The `artifactory-volume` mount the sidecar already needs is enough.

## Azure without a key

Where `binarystore.xml` says `<useInstanceCredentials>true</useInstanceCredentials>`, Artifactory holds no storage credential: it asks the platform who it is and receives a short-lived token.
Airlift does the same whenever no account key is configured, detecting what the platform injected:

- **Federated (workload) identity** when `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` are all present.
- **Instance metadata** otherwise.

Tokens are cached and refreshed ahead of expiry, and the assertion file is re-read on every exchange, so rotation needs no restart.
Setting `binarystore_account_key` pins shared-key signing instead.

Two things this depends on, both environmental rather than configuration:

- **The identity has to reach the airlift container**, not only Artifactory's.
  A projected token volume is per-container.
  An injection webhook normally covers every container in a pod it admits, but a `skip-containers` annotation defeats that; then mount the projected volume and set the four `AZURE_*` variables on the sidecar by hand.
  Never with `subPath`, which would freeze the assertion at pod start.
  The settling check is `kubectl exec <pod> -c airlift -- env | grep AZURE_`.
- **Data-plane RBAC is separate from control-plane.**
  The identity needs a role granting access to blob data (Storage Blob Data Reader suffices on the sender, Contributor on the receiver).
  A role on the storage account is not enough, and the failure is a 403 on the first blob call after a healthy-looking startup.

This is also the only route on a storage account with `allowSharedKeyAccess: false`, where no account key exists to configure.

## When the key prefix is not in the XML

Blobs are keyed `<path>/<sha1[:2]>/<sha1>`, where `<path>` comes from the provider block.
When that element is omitted the provider falls back to its own default, and those defaults differ:

| Provider | `<path>` default |
| --- | --- |
| `s3-storage-v3` | `filestore` |
| `azure-blob-storage-v2` | `data` |
| `azure-blob-storage` (v1) | no `<path>` parameter |

Airlift assumes `filestore`, so an Azure Blob v2 store whose XML omits `<path>` is addressed at the wrong keys.
Set `binarystore_prefix` to correct it: `data` for that case, or `/` for the container root.

**Do not add `<path>` to `binarystore.xml` instead.**
Artifactory reads the same file, so stating a prefix it was not already using relocates its own filestore and it stops finding every blob it has written.

This one wants checking rather than assuming, because it fails quietly.
A blob that is not where airlift looked returns 404, which is indistinguishable from one that has not been uploaded yet, so the entry is deferred instead of raising.
The signature is every read returning 404 and a deferral count that only grows.
Confirm against the store itself, by finding a known artifact's sha1 in a listing of the bucket or container.

A 404 rather than a 403 is also useful evidence: Azure returns 403 before it looks for the blob, so a 404 proves the identity and data-plane RBAC are working and the key is wrong.

## Blobs that have not landed yet

A chain with an `eventual` provider uploads asynchronously, so an artifact can appear in the enumeration before its bytes reach the store.

Those entries are left out of the archive manifest and held back from the snapshot that becomes the next baseline, so the following cycle re-detects them as added and ships them once they are readable.
The sender logs `Deferred N entr(ies)`.
A steady trickle is normal on a busy source; a count that only grows is not, and usually means the key prefix is wrong.

## Large blobs

Blobs at or above `binarystore_multipart_threshold` (default `256Mi`) upload as S3 multipart or Azure staged blocks.
A single S3 PUT is capped at 5 GiB, so this is what allows large artifacts through.
