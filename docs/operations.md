# Operations

Running the sidecar: what to look at, what to do about it.

## Reading the log

The console format is one line per event:

```
2026-08-17 22:11:42 INFO  sender   cycle=1786964702 Diff vs prev=1786964672: 1 added, 0 removed.
```

Columns are date, clock time, fixed-width level, an eight-character component (`sender`, `receiver`, `archive`, `aql`, `synth`, `import`, `main`), an optional `cycle=` tag, then the message.
Set `AIRLIFT_LOG_FORMAT=json` for structlog JSON during incident triage.

A healthy sender cycle with a change in it:

```
sender   cycle=… Cycle starting; enumerating the source.
aql      Enumerated 275 row(s) from the source.
sender   cycle=… Snapshot written: 275 artifacts across 8 repos.
sender   cycle=… Diff vs prev=…: 1 added, 0 removed.
sender   cycle=… Per-repo changes: example-repo-local=+1
synth    Metadata tree written: 1 artifact(s), 0 unresolved.
archive  Archive built: 1 blob(s), 293.0KiB uncompressed, packed to 294.5KiB (1 repo(s))
sender   cycle=… Archive ready: /var/airlift/spool/….tar.zst (294.5KiB, 1 blob(s), 1 repo(s)).
```

The matching receiver cycle:

```
receiver cycle=… Extracting archive: … (294.5KiB).
receiver cycle=… Manifest loaded: 1 blob(s) (293.0KiB uncompressed) across 1 repo(s); 0 pending deletion(s).
receiver cycle=… Blobs: 1 written, 0 skipped (already in filestore).
receiver cycle=… Per-repo changes applied: example-repo-local=+1
receiver cycle=… Cycle done: status=ok.
```

Worth grepping for:

| Event | Means |
| --- | --- |
| `Archive ready` / `Extracting archive` | The two ends of one transport hop. |
| `Per-repo changes` | The delta this cycle, `+N` and `-N` per repository. |
| `Cycle done: status=` | `ok` or `partial`. |
| `Skipping cycle: N archive(s) still in spool` | The transport is behind. |
| `Deferred N entr(ies)` | Blobs not yet in the store. A trickle is normal, growth is not. |
| `REFUSING cycle` | The deletion brake tripped. |
| `Binarystore unavailable` | The store is unreachable; the cycle idles and retries. |
| `Idling after` | The process parked on an unrecoverable error. |

**A misconfigured airlift looks healthy to Kubernetes.**
It never exits, because a crashlooping sidecar would take Artifactory out of its Service.
Alert on `binarystore_unavailable` and `parked_after_*` rather than on restart counts.

## Inspecting state

```sh
# Sender: snapshots, cursor, synthesised metadata trees
kubectl -n <source-namespace> exec sts/artifactory -c airlift -- ls /var/airlift/state

# Either side: pending archives at the top, processed under .done/
kubectl -n <namespace> exec sts/artifactory -c airlift -- ls -R /var/airlift/spool

# Receiver: the idempotency ledger, one line per cycle
kubectl -n <destination-namespace> exec sts/artifactory -c airlift -- cat /var/airlift/state/processed.jsonl
```

## Forcing a cycle

The loop runs once immediately on startup, so restarting the container is enough:

```sh
kubectl -n <namespace> exec sts/artifactory -c airlift -- kill 1
```

`kubectl rollout restart` also works but restarts Artifactory with it.

## Changing which repositories are synced

Narrowing `included_repos`, or adding to a denylist, removes everything outside the new scope from the snapshot.
The diff reads that as deleting it from the destination, and the brake refuses the cycle.

Change the setting, then clear the cursor:

```sh
kubectl -n <source-namespace> exec sts/artifactory -c airlift -- rm -f /var/airlift/state/cursor.json
```

The next cycle is a cold start, which emits no removals and re-adds everything in the new scope.
The re-add is cheap: the receiver finds the blobs already in its filestore and the import is idempotent.

Widening scope needs nothing, because additions are never braked.

## Resetting a side

```sh
# Sender: drop state and spool, forcing a clean baseline on the next tick
kubectl -n <source-namespace> exec sts/artifactory -c airlift -- \
  sh -c 'rm -rf /var/airlift/state/* /var/airlift/spool/*'

# Receiver: drop the ledger so every archive in spool is reprocessed
kubectl -n <destination-namespace> exec sts/artifactory -c airlift -- \
  rm -f /var/airlift/state/processed.jsonl
```

Reprocessing is safe: blobs are content-addressed and imports are idempotent.

## Common issues

**`503` on `/api/system/ping` just after a restart.**
Artifactory is still booting.
The retry decorator covers it and the next cycle succeeds.
One failed retry chain per pod start is expected.

**`401` on `/api/import/repositories`.**
The token's subject is not an admin user.
Mint an admin-scoped token through the UI, or fall back to basic auth with an admin account.

**Every cycle logs `Skipping cycle: N archive(s) still in spool`.**
The transport has not collected the last delta.
This is the intended response to a stalled transport and produces no side effects.
Move or remove the archives to release it.

**`REFUSING cycle: N of M artifacts would be deleted`.**
Either the source lost that much, or the enumeration came back short, or the sync scope changed.
Check `Per-repo counts on source` against what you expect before doing anything.
For an intended scope change, clear the cursor as above.
Raising `max_delete_fraction` should be a deliberate decision, not a reflex.

**`Deferred N entr(ies)` grows without bound.**
Blobs are not where airlift is looking.
On object storage the usual cause is a wrong key prefix; see [when the key prefix is not in the XML](binarystore.md#when-the-key-prefix-is-not-in-the-xml).

**Receiver records `500 : The directory <name> does not match any repository key.`**
The named repository exists on the source but not on the destination.
Create it there.
Notices for repositories the cycle did not ship are filtered out, so one that surfaces names a repository whose artifacts were in the archive.

**Import rejected with `Invalid Import Directory`.**
The path handed to the import API must not sit under `/var/opt/jfrog/artifactory/...`.
The receiver extracts to `state_dir/import/<cycle_id>/` for this reason, so this means `state_dir` has been pointed somewhere it should not be.

**Credentials set but ignored.**
Every variable the sidecar reads starts with `AIRLIFT_`.
A bare `ARTIFACTORY_TOKEN` is silently ignored.

**A chart value appears to have no effect.**
The chart has no values schema, so a key at the wrong nesting level is accepted and ignored while the default renders.
Check the rendered ConfigMap rather than the values diff:

```sh
helm template airlift ./helm -f values.yaml -s templates/configmap.yaml
```

## Validating operator values without cluster access

This sequence has caught real problems and is worth reusing.

The three `custom*` blocks in the jfrog chart's values are strings, so a plain `yaml.safe_load` of the file proves nothing about their contents.
Parse each and cross-check the names:

```sh
python -c "
import yaml; d=yaml.safe_load(open('values.yaml'))['jfrog']['artifactory']['artifactory']
v={x['name'] for x in yaml.safe_load(d['customVolumes'])}
m={x['name'] for x in yaml.safe_load(d['customSidecarContainers'])[0]['volumeMounts']}
print('dangling:', sorted(m-v-{'artifactory-volume'}))"
```

A `volumeMount` whose volume was removed blocks the pod at admission and takes Artifactory with it.

Then render the chart and push the result through the real settings loader, which is the only way to see what environment-versus-ConfigMap precedence resolves to:

```sh
helm template airlift ./helm -f values.yaml -s templates/configmap.yaml \
  | python -c "import sys,yaml; print(yaml.safe_load(sys.stdin)['data']['config.yaml'])" > config.yaml
python -c "
from pathlib import Path; from artifactory_airlift import config
print(config.load(Path('config.yaml')))"
```

And run the real `binarystore.xml` through the real parser, which turns a guess about the backend into a concrete answer:

```sh
python -c "
from pathlib import Path; from artifactory_airlift.binarystore import config as bc
print(bc.parse(Path('binarystore.xml')))"
```
