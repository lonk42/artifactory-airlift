from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def kubectl_run(args: list[str], *, capture: bool = True, check: bool = True) -> str:
    """Run a kubectl command and return stdout (decoded)."""
    proc = subprocess.run(
        args,
        check=check,
        capture_output=capture,
        text=True,
    )
    return proc.stdout


def pod_for(kubectl: str, namespace: str, selector: str) -> str:
    out = kubectl_run([
        kubectl, "-n", namespace, "get", "pod",
        "-l", selector,
        "-o", "jsonpath={.items[0].metadata.name}",
    ])
    if not out.strip():
        raise RuntimeError(f"no pod matching {selector!r} in namespace {namespace!r}")
    return out.strip()


def shuttle_spool(
    kubectl: str,
    *,
    src_ns: str,
    dst_ns: str,
    src_pod: str,
    dst_pod: str,
    container: str = "airlift",
    src_dir: str = "/var/airlift/spool",
    dst_dir: str = "/var/airlift/spool",
) -> list[str]:
    """Move any *.tar.zst archive from the sender's spool to the receiver's.

    Stands in for the real one-way air-gap transport channel during e2e
    tests. Each successful per-archive transfer deletes the source file,
    matching the move-semantics a real transport would have. This is
    critical now that the sender enforces a "one delta in flight" gate:
    archives left in the sender's spool would prevent it from starting
    the next cycle. Returns the list of archive filenames moved.
    """
    listing = kubectl_run([
        kubectl, "-n", src_ns, "exec", src_pod, "-c", container, "--",
        "sh", "-c", f"ls -1 {shlex.quote(src_dir)}/*.tar.zst 2>/dev/null || true",
    ]).splitlines()

    moved: list[str] = []
    for path in listing:
        path = path.strip()
        if not path:
            continue
        name = Path(path).name
        # Copy via stdout/stdin so we never need a local scratch file.
        cat = subprocess.Popen(
            [kubectl, "-n", src_ns, "exec", src_pod, "-c", container, "--",
             "cat", path],
            stdout=subprocess.PIPE,
        )
        write = subprocess.Popen(
            [kubectl, "-n", dst_ns, "exec", "-i", dst_pod, "-c", container, "--",
             "sh", "-c",
             f"cat > {shlex.quote(dst_dir + '/' + name + '.partial')} "
             f"&& mv {shlex.quote(dst_dir + '/' + name + '.partial')} "
             f"{shlex.quote(dst_dir + '/' + name)}"],
            stdin=cat.stdout,
        )
        if cat.stdout is not None:
            cat.stdout.close()
        write.wait()
        cat.wait()
        if write.returncode != 0 or cat.returncode != 0:
            raise RuntimeError(f"shuttle failed for {name}")
        # Only delete the source after the destination rename succeeded.
        # If this rm fails we surface the error rather than leaving the
        # archive duplicated; a retry on the same file is a no-op on the
        # destination side (cycle_id already in processed.jsonl) but
        # would deadlock the pending-gate if it stayed on the source.
        rm = subprocess.run(
            [kubectl, "-n", src_ns, "exec", src_pod, "-c", container, "--",
             "rm", "-f", path],
            check=False,
            capture_output=True,
            text=True,
        )
        if rm.returncode != 0:
            raise RuntimeError(
                f"shuttle moved {name} to dst but failed to delete source: "
                f"{rm.stderr.strip() or rm.stdout.strip()}"
            )
        moved.append(name)
    return moved
