# Architecture

How a cycle works on each side, and the guarantees that fall out of it.

## The shape of the problem

Airlift polls.
It has no event feed from Artifactory, so every cycle has to answer "what changed since last time?" from scratch.
Doing that badly costs time proportional to the size of the instance rather than the size of the change.

Three things per cycle are therefore kept proportional to the delta: discovering what changed, describing it, and shipping it.

## What the sender does each cycle

1. **Skip if the transport is behind.**
   If any finalised `*.tar.zst` from a prior cycle is still in the spool directory, the cycle returns immediately and logs `sender.cycle_skipped_pending`.
   See [One delta in flight](#one-delta-in-flight).

2. **Enumerate the source with one AQL query.**
   `POST /api/search/aql` returns `(repo, path, name, sha1, size)` for every file.
   Both the allowlist and the denylist are part of the query, so excluded repositories are never fetched.
   The result is written as a sorted JSONL snapshot at `state/snapshots/<cycle_id>.jsonl`.

3. **Diff against the previous snapshot.**
   A two-pointer set-diff yields added `(sha1, repo, path)` triples, and removed ones when `propagate_deletes` is on.
   Removals are skipped when there is no previous snapshot, since a missing baseline cannot distinguish "deleted on the source" from "never seen".

4. **Check the deletion brake.**
   A removal set larger than `max_delete_fraction` of the baseline aborts the cycle without advancing the cursor.
   See [The deletion brake](#the-deletion-brake).

5. **Synthesise the metadata tree** for the added artifacts only, into `state/exports/<cycle_id>/repositories/...`.
   See [Metadata synthesis](#metadata-synthesis).

6. **Read each added blob from the binarystore** at `<prefix>/<sha1[:2]>/<sha1>`, streaming it into the archive.
   A blob that is not there yet is deferred rather than lost; see [Binarystore access](binarystore.md#blobs-that-have-not-landed-yet).

7. **Write `<cycle_id>.tar.zst`** containing `manifest.json`, the synthesised `metadata/` tree, and `blobs/<aa>/<sha1>` for each added sha1.
   Removed entries ship as manifest records only; their blobs are never re-included, because the same sha1 may still be in use elsewhere.
   The archive finalises atomically (`.partial` then `os.rename`).
   Cycles above `max_archive_bytes` split into several archives; see [Chunked deltas](#chunked-deltas).

8. **Prune** `state/snapshots/` under the tiered retention policy and `state/exports/` past `history_keep`.

## What the receiver does each cycle

1. List `spool/*.tar.zst` in lexical order, which is also time order.
2. Skip any `cycle_id` already in `state/processed.jsonl`.
3. Extract to `state/import/<cycle_id>/`.
   This has to be on the state PVC: the import API rejects paths under `/var/opt/jfrog/artifactory/...` with `Invalid Import Directory`.
4. Write each blob into the binarystore.
   On disk that is write to `<root>/<aa>/<sha1>.tmp-<pid>`, `fsync`, `rename`, `chown`, `chmod 0640`.
   On object storage it is an upload, switching to multipart or staged blocks above `binarystore_multipart_threshold`.
   Existing sha1s are skipped either way, since the binarystore is content-addressed.
5. `POST /api/import/repositories?path=<extract_dir>/metadata/repositories&verbose=1`.
   The endpoint returns 200 even when individual repositories fail, so the receiver scans the response body for failure lines.
   See [Import notices](#import-notices).
6. For each record in the manifest's `removed[]`, `DELETE /<repo>/<path>`.
   Imports run before deletions, so a sha1 that moves from one path to another within a cycle survives.
7. Append a row to `state/processed.jsonl` and move the archive to `spool/.done/`.

## Metadata synthesis

Artifactory registers an artifact from a directory of small XML files:

```
repositories/<repoKey>/<path...>/<file>.artifactory-metadata/
    artifactory-file.xml      checksums, sizes, timestamps, the deploying user
    properties.xml            one element per property key (omitted when there are none)
```

Nothing requires Artifactory to have produced that tree.
The sender builds it from the AQL rows for the changed artifacts, so its size tracks the delta rather than the instance.

Keeping the import path, rather than deploying bytes over REST, is what preserves fidelity: Artifactory registers the checksums, properties and original timestamps itself.
A synthesised tree imported onto a destination has been verified to reproduce the source's `created`, `createdBy`, `lastModified`, `modifiedBy`, `lastUpdated`, all three checksums and every property, for a Docker manifest with its config blob and five layers.

Two details that are not obvious from the file format:

- **`<original>` is not `<actual>`.**
  It records what the deploying client declared, and Artifactory omits it for sha1 and md5 unless the client stated one.
  Emitting it unconditionally imports cleanly but leaves the destination reporting an `originalChecksums` block the source does not have.
- **Download statistics are not synthesised.**
  A real export also writes `artifactory.stats.xml`; those counts belong to the instance, not to the artifact.

Only `repositories/` is emitted.
A full export also writes `etc/`, `artifactory.config.xml` and `licenses/`, which exist for `/api/import/system`, an endpoint airlift does not call.

## Enumeration hazards

AQL is a database query, and a query can come back short while looking healthy.
Two properties of it shape the design.

**It collapses adjacent duplicate rows over the projected fields.**
This is `uniq` semantics, not `SELECT DISTINCT`.
Against one repository holding 140 files:

```
.include("actual_sha1","size")            ->  43 rows
.include("name","actual_sha1","size")     ->  53 rows
.include("repo","path","name",...)        -> 140 rows
```

There is no error: the response is a 200 with well-formed JSON and a plausible row count.
A short enumeration reads as mass deletion.
The defence is structural: `(repo, path, name)` is a natural key, so no row can equal its neighbour once all three are projected, and the query builder always emits them.
Callers may add fields and cannot remove those.

**It has no set operators.**
`$in` and `$nin` do not exist and are rejected with a parse error.
Membership is written as an `$or` of `$eq`, and its negation as an `$and` of `$ne`.
Both need that form even for a single pair, because a JSON object cannot carry the same key twice and the duplicate collapses to whichever clause survives the parse.

## The deletion brake

`max_delete_fraction` (default `0.05`) caps how much of the mirror one cycle may remove.
Above it the cycle logs `sender.delete_brake_tripped` and returns without building an archive or advancing the cursor, so the next cycle re-runs the same diff against the same baseline.

It is cause-agnostic on purpose.
A collapsed projection, a partial result, a repository that went invisible, and a genuine mass deletion on the source all present identically, and all deserve a refusal.

The case operators hit in practice is a scope change: narrowing `included_repos` removes everything outside the new scope from the snapshot, and the diff reads that as deleting it from the destination.
Measured on a live pair, scoping a sender to one repository produced a diff of 245 removals out of 275 artifacts, which the brake refused.

To change scope deliberately, change the setting and then delete `state/cursor.json` on the sender.
With no cursor the next cycle is a cold start, which emits no removals and re-adds everything in the new scope.
The re-add is cheap, because the receiver finds the blobs already in its filestore and the import is idempotent.
Widening scope needs nothing, since additions are never braked.

## Chunked deltas

A cycle whose cumulative raw blob bytes exceed `max_archive_bytes` (default `8Gi`) splits into several archives.
This matters on a first cycle against a populated source, which sees every blob as added, and on bulk imports that would otherwise produce an archive larger than the spool volume.

Chunks share a `parent_cycle_id` and are named `<parent_cycle_id>-cNNN.tar.zst`, zero-padded so lexical order is sequence order.
Only the final chunk carries the `metadata/` tree and the `removed[]` list; the rest are blob payloads.

The receiver writes blobs from each chunk as it sees them and records `status: "blob-staged"`, deferring the import and the deletions until the final chunk arrives *and* every earlier chunk for that parent is already in the ledger.
Out-of-order delivery is safe: a final chunk that arrives early stays in spool and is re-evaluated next tick.

Single-chunk cycles keep the plain `<cycle_id>.tar.zst` name.
Set `max_archive_bytes: 0` to disable splitting.

## Spool backpressure

Before each chunk the sender requires `free(spool) >= spool_min_free_bytes + projected_chunk_bytes`, where the projection is the raw blob sum plus 256 MiB of framing overhead.
When the check fails it logs `sender.spool_backpressure`, deletes any chunks already written for the in-flight parent, and returns without advancing the cursor.
The next cycle re-emits the same diff, so an undersized spool produces repeatable aborts rather than half-shipped chunk sets.

Operator options: grow the spool volume, lower `max_archive_bytes`, or let the transport drain.
The retry cadence is `cycle_seconds`; there is no separate knob.

## One delta in flight

The sender refuses to start a cycle while a finalised archive from a prior cycle is still in spool.

Without the gate, a stalled transport still produces a snapshot and a synthesised tree per cycle, and those accumulate on the state PVC because pruning only runs after a cycle succeeds.
With it, a stalled transport produces no side effects and the cursor stays put.

`.partial` files do not trip the gate.
They are left by a SIGKILL during a build, are never picked up by the receiver, and are removed by a startup sweep on both sides along with orphaned `spool/.staging/<cycle_id>/` directories.
A non-empty sweep logs `sender.startup_sweep` / `receiver.startup_sweep`; a clean tree is silent.

## Import notices

`/api/import/repositories` walks every repository the destination holds and reports one line per repository with no directory in the tree:

```
500 : No directory for repository <key> found at <path>
500 : The directory <key> does not match any repository key.
```

Because the shipped tree covers only what changed, most repositories produce one of these on most cycles.
The receiver drops any such notice naming a repository with no directory in the extracted tree, and keeps it otherwise: a missing directory for a repository the cycle *did* ship means the archive or the extraction is wrong.

A repository that exists on the source but not on the destination surfaces the same way, which is why destination repositories have to be pre-created.

## Failure semantics

**Adding is idempotent.**
Blobs already in the filestore are skipped, the `cycle_id` is recorded, and a missing predecessor is logged but does not block, since wider later diffs converge.

**Deleting is idempotent in the desired-state sense.**
A 404 on the target means the artifact is already gone, which is the intended end state, so it counts as success and is recorded in `delete_failures[]` for visibility.

**Import failures flip the cycle to `partial`; delete failures do not.**
A partial cycle stays in the ledger and its archive stays in `.done/` for replay.

**Airlift never exits.**
A pod's readiness is the AND of its containers, so a crashlooping sidecar would drop Artifactory out of its Service and block StatefulSet rollout.
An unusable binarystore leaves the cycle idle and retries; an unrecoverable configuration error parks the process and restates the reason every 300 seconds.
The consequence for monitoring is that a misconfigured airlift looks healthy to Kubernetes while doing nothing, so alert on the `binarystore_unavailable` and `parked_after_*` events rather than on restart counts.
