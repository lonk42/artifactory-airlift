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
    """Copy any *.tar.zst archive in the sender's spool to the receiver's spool.

    Stands in for the real one-way transport channel during e2e tests.
    Returns the list of archive filenames moved.
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
        moved.append(name)
    return moved
