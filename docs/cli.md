# Command line

The image ships a second entry point, `airlift`, alongside the sidecar process.
It reads the same configuration the daemon reads, in the same order, so what it reports is what the daemon sees.

Run it in the sidecar:

```sh
kubectl -n <namespace> exec sts/artifactory -c airlift -- airlift status
```

Every command takes `--json` and prints the same data as a structure, for scripting.
Airlift's own logging goes to stderr and is quiet unless `-v` is given, so stdout carries only the command's output.

## Current state

`airlift status` is one screen per side: whether the daemon holds the state lock, whether Artifactory answers, which binarystore backend resolved, what the spool holds, and where the cursor sits.

```
airlift
  mode         sender (artifactory-a)
  daemon       running (holds the state lock)
  artifactory  OK http://localhost:8081/artifactory (12ms)
  binarystore  OK s3 bucket artifactory-a at http://minio:9000 (prefix 'artifactory/filestore')

spool
  pending  0 archive(s), 0B
  free     18.5GiB

sender
  baseline        1787016712-05043365
  last success    4s ago
  snapshots       24 retained, newest 275 artifact(s)
  deferred blobs  0
```

`airlift config` prints every setting with the origin of its value: `env`, `file`, or `default`.
Environment variables outrank the mounted ConfigMap, and the chart renders every key into that ConfigMap whether or not it was set, so a file value that the environment overrides is listed as masked rather than dropped from the output.
Credentials are replaced with their length.

`airlift doctor` runs the checks actively and exits 2 if any fail: directory permissions, spool headroom, the import path restriction, ping, whether the token can list repositories, and backend resolution.
Add `--write-probe` to write and remove a probe object; the receiver does this anyway, the sender does not, because the source only needs read access.

`airlift repos` lists every repository with its package type and whether the current allowlist and denylists admit it, which answers what a cycle will cover before a cycle answers it.
`--counts` adds an artifact count per repository.

## History

The receiver records `state/processed.jsonl` and the sender records `state/cycles.jsonl`.
`airlift cycles` reads whichever applies and normalises both into one table.

```
CYCLE                WHEN (UTC)           STATUS         ADDED  REMOVED  SIZE     REPOS
1787015468-aaaaaaa1  2026-08-18 01:11:08  ok             +10             40.0KiB  example-repo-local
1787016008-ccccccc3  2026-08-18 01:19:08  brake-refused         -9                9 of 12 (75.0%) exceeds max_delete_fraction 0.05
```

Cycles that did nothing (`no-changes`, `skipped-pending`, `ping-failed`) are hidden unless `--all` is given: a stalled transport writes one every `cycle_seconds` and would bury the rest.
Filters are `--since`, `--until`, `--status`, `--repo` and `-n`.
Times accept ISO 8601, epoch seconds, or a window such as `7d`; bare timestamps are UTC.

| Command | Shows |
|---|---|
| `airlift show <cycle>` | Every trace of one cycle: ledger rows, snapshot, metadata tree, archives and their manifests |
| `airlift snapshots` | Retained baselines, entry counts, and which one the cursor points at |
| `airlift diff <a> <b>` | Added and removed between two snapshots, with the deletion brake verdict |
| `airlift archives` | Archives in the spool, `--where done` for those already applied |
| `airlift archive <ref>` | One archive's manifest, `--entries` for its contents, `--verify` to rehash every blob |

`latest` and `prev` work anywhere a cycle id, snapshot or archive is expected.

## Ad-hoc exports

`airlift export` builds archives for a selection that has nothing to do with the cycle diff.
The cursor and the snapshots are untouched, so the next ordinary cycle behaves as though the export never happened.

| Selector | Ships |
|---|---|
| `--since 7d [--until ...]` | Artifacts whose timestamp falls in the window. `--time-field` picks `created`, `modified` (default) or `updated` |
| `--from-snapshot X` | Everything in a retained baseline |
| `--from-snapshot X --to-snapshot Y` | What was added between two baselines |
| `--artifact <repo>/<path>` | One named artifact, repeatable |
| `--all` | Everything the current filters admit |

`--repo` narrows any of them.
`--dry-run` reports the selection, the raw byte total and the chunk plan without writing.

```sh
airlift export --since 30d --repo airlift-npm-local --dry-run
airlift export --since 30d --repo airlift-npm-local --yes
```

Archives land in the spool, where the transport picks them up like any other.
An archive sitting in the spool holds the next ordinary cycle until it is drained, which is the same rule that applies to a cycle's own output.
`--out DIR` writes elsewhere; the spool free-space floor does not apply to a destination named explicitly.

An ad-hoc archive carries no predecessor, so the receiver does not report a gap for it.

**Recovering a lost archive.**
A cycle whose archive never arrived can be rebuilt from the pair of snapshots that bracket it, without the source being involved:

```sh
airlift export --from-snapshot <previous-cycle> --to-snapshot <lost-cycle>
```

Both must still be retained; `airlift snapshots` says what is.

`airlift plan` enumerates the source and diffs it against the current baseline the way a cycle does, then discards the result.
It reports what the next cycle would add, remove and chunk, and whether the deletion brake would refuse it.

## Debugging

`airlift blob <sha1>` prints where the configured backend looks for a blob and whether it is there.
A blob that is not where airlift looked and one Artifactory has not written yet both read as a 404, so the address is printed either way; comparing it against a blob known to exist is what separates a wrong key prefix from an absence.
`--get FILE` downloads it and checks the digest.

`airlift aql '<query>'` runs a query and refuses a projection that omits `repo`, `path` or `name`.
AQL collapses adjacent duplicate rows over the projected fields, silently and with a healthy 200, so an ad-hoc query can under-report and read as evidence of a problem that does not exist.
On a repository holding 140 files, a projection of `actual_sha1` and `size` returned 137 rows; the same query with the item key returned 140.
`--force` runs it anyway and prints the warning; `--count '<criteria>'` returns `range.total` for a criteria object.

`airlift import <dir>` calls `/api/import/repositories` against a tree directly, for separating an import failure from everything upstream of it.
Point it at the directory that contains per-repository directories.

## Fixing

| Command | Does |
|---|---|
| `airlift cursor show` | The current baseline, and whether its snapshot is still retained |
| `airlift cursor clear` | Drops the cursor, making the next cycle a cold start |
| `airlift cursor set <cycle>` | Moves the baseline to a retained snapshot |
| `airlift forget <cycle>` | Drops one cycle from the receiver ledger so its archive can be applied again |
| `airlift replay <cycle>` | Moves an archive out of `.done/` back into the spool and forgets it |

Clearing the cursor is the documented way to change sync scope: a narrowed scope reads as mass deletion and the brake refuses the cycle, while a cold start emits no removals and re-adds everything in the new scope.
`airlift cursor set` refuses a cycle whose snapshot is no longer retained, because a cursor pointing at a missing snapshot is treated as a cold start on the next cycle.

`airlift replay` stages the work rather than doing it: the receiver owns the import path, and a second process running it concurrently is the race the chunk ordering guard exists to prevent.
The archive is applied on the next receiver cycle.

**Concurrency.**
The daemon holds `state/<mode>.lock` for the lifetime of the process, not for the duration of a cycle, so no command can take that lock while the sidecar is healthy.
Commands that change state take `state/cli.lock` instead, which serialises them against each other, and warn when the daemon is running.
A cycle already in flight can overwrite a cursor edit; re-run the command if it does not take effect.
Every operation here is idempotent, so re-running is safe.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The command could not be carried out, or `blob` found nothing |
| 2 | A check failed: `doctor`, `archive --verify`, a partial `import`, an aborted `export` |
| 130 | Interrupted |
