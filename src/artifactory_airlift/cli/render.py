"""Rendering for CLI output.

Every command produces a dictionary first and renders it second, so
``--json`` is never a second implementation of the command and a future UI
can consume the same structures. Nothing here uses colour or terminal
control: the usual reader is ``kubectl exec`` output in a pipeline.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Sequence


def emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Two-space separated columns, padded to the widest cell.

    Right-aligns a column when every value in it reads as a number, which is
    what makes counts and byte sizes scannable down the page.
    """
    body = [[("" if c is None else str(c)) for c in row] for row in rows]
    if not body:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    numeric = []
    for i in range(len(headers)):
        column = [row[i] for row in body if i < len(row) and row[i] not in ("", "-")]
        numeric.append(bool(column) and all(_looks_numeric(c) for c in column))

    def render(cells: Sequence[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            if i >= len(widths):
                out.append(cell)
            elif numeric[i]:
                out.append(cell.rjust(widths[i]))
            else:
                out.append(cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    lines = [render([h.upper() for h in headers])]
    lines.extend(render(row) for row in body)
    return "\n".join(lines)


def _looks_numeric(cell: str) -> bool:
    stripped = cell.replace(",", "").replace("+", "").replace("-", "")
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB", "s", "m", "h", "%"):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            break
    try:
        float(stripped)
    except ValueError:
        return False
    return True


def kv(pairs: Iterable[tuple[str, Any]], *, indent: str = "  ") -> str:
    """Aligned ``label: value`` block."""
    items = [(str(k), "" if v is None else str(v)) for k, v in pairs]
    if not items:
        return f"{indent}(none)"
    width = max(len(k) for k, _ in items)
    return "\n".join(f"{indent}{k.ljust(width)}  {v}" for k, v in items)


def section(title: str, body: str) -> str:
    return f"{title}\n{body}"


def blocks(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def status_mark(ok: bool | None) -> str:
    """Text status marker. ASCII on purpose: this output gets piped and diffed."""
    if ok is None:
        return "??"
    return "OK" if ok else "FAIL"
