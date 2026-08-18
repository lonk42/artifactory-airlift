"""Argument parsing and dispatch for the ``airlift`` CLI.

Named ``app`` rather than ``main`` so the module and the ``main``
function the package exports cannot shadow each other.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import __version__
from .. import archive as archive_mod
from .. import log
from ..log import human_bytes
from . import actions, common, render, views
from .common import CLIError, fmt_age, fmt_ts

_EPILOG = """\
Run inside the airlift sidecar:
  kubectl -n <namespace> exec sts/artifactory -c airlift -- airlift status
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airlift",
        description="Inspect and operate an airlift sidecar.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"airlift {__version__}")

    # Global options are declared on a parent parser so they can be given
    # after the subcommand too ("airlift status --json"), which is where an
    # operator reaches for them.
    globals_parser = argparse.ArgumentParser(add_help=False)
    globals_parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of text"
    )
    globals_parser.add_argument(
        "--config", metavar="PATH", help="config file (default /etc/airlift/config.yaml)"
    )
    globals_parser.add_argument("--state-dir", metavar="DIR", help="override state_dir")
    globals_parser.add_argument("--spool-dir", metavar="DIR", help="override spool_dir")
    globals_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show airlift's own INFO logging (on stderr)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(
            name, help=help_text, description=help_text, parents=[globals_parser]
        )

    p = add("status", "Current state of this side: cursor, spool, backend, last cycle.")
    p.add_argument(
        "--offline",
        action="store_true",
        help="skip the Artifactory and binarystore probes",
    )
    p.set_defaults(func=cmd_status)

    p = add("config", "Effective settings, with the origin of each value.")
    p.add_argument(
        "--changed", action="store_true", help="only settings that are not at default"
    )
    p.set_defaults(func=cmd_config)

    p = add("doctor", "Run active checks and exit non-zero on failure.")
    p.add_argument(
        "--write-probe",
        action="store_true",
        help="write a probe object to the binarystore (implied on the receiver)",
    )
    p.set_defaults(func=cmd_doctor)

    p = add("repos", "Repositories on this instance and whether the filters admit them.")
    p.add_argument(
        "--counts", action="store_true", help="include an artifact count per repository"
    )
    p.add_argument("--synced", action="store_true", help="only repositories that sync")
    p.set_defaults(func=cmd_repos)

    p = add("cycles", "Cycle history for this side.")
    p.add_argument("-n", "--limit", type=int, default=20, help="rows to show (0 = all)")
    p.add_argument("--since", metavar="WHEN", help="ISO time, epoch, or a window like 7d")
    p.add_argument("--until", metavar="WHEN")
    p.add_argument("--status", metavar="S", help="comma-separated status filter")
    p.add_argument("--repo", metavar="KEY", help="only cycles touching this repository")
    p.add_argument(
        "--all",
        dest="include_quiet",
        action="store_true",
        help="include cycles that did nothing (no-changes, skipped-pending)",
    )
    p.set_defaults(func=cmd_cycles)

    p = add("show", "Everything recorded about one cycle.")
    p.add_argument("cycle_id", help="a cycle id, or 'latest' / 'prev'")
    p.set_defaults(func=cmd_show)

    p = add("snapshots", "Retained snapshot baselines (sender).")
    p.set_defaults(func=cmd_snapshots)

    p = add("diff", "Added and removed between two snapshots.")
    p.add_argument("previous", help="cycle id, path, 'latest' or 'prev'")
    p.add_argument("current", nargs="?", default="latest")
    p.add_argument("--repo", metavar="KEY")
    p.add_argument("--full", action="store_true", help="list every entry")
    p.set_defaults(func=cmd_diff)

    p = add("archives", "Archives in the spool, and optionally those already done.")
    p.add_argument(
        "--where", choices=("spool", "done", "all"), default="spool"
    )
    p.set_defaults(func=cmd_archives)

    p = add("archive", "Inspect one archive.")
    p.add_argument("ref", help="a cycle id, a file name, or a path")
    p.add_argument("--entries", action="store_true", help="list manifest entries")
    p.add_argument(
        "--verify", action="store_true", help="recompute the sha1 of every blob inside"
    )
    p.add_argument("--unpack", metavar="DIR", help="extract to DIR for inspection")
    p.set_defaults(func=cmd_archive)

    p = add("blob", "Where the binarystore keeps a sha1, and whether it is there.")
    p.add_argument("sha1")
    p.add_argument("--get", metavar="FILE", help="download and verify the blob")
    p.set_defaults(func=cmd_blob)

    p = add("plan", "What the next ordinary cycle would do, without doing it.")
    p.set_defaults(func=cmd_plan)

    p = add("export", "Build archives for an ad-hoc selection of artifacts.")
    p.add_argument("--since", metavar="WHEN", help="ISO time, epoch, or a window like 7d")
    p.add_argument("--until", metavar="WHEN")
    p.add_argument(
        "--time-field",
        choices=actions.TIME_FIELDS,
        default="modified",
        help="which timestamp the window selects on (default: modified)",
    )
    p.add_argument(
        "--repo", action="append", metavar="KEY", help="restrict to this repository (repeatable)"
    )
    p.add_argument(
        "--artifact",
        action="append",
        metavar="REPO/PATH",
        help="ship one named artifact (repeatable)",
    )
    p.add_argument(
        "--from-snapshot",
        metavar="ID",
        help="ship a retained snapshot, or its delta when --to-snapshot is given",
    )
    p.add_argument("--to-snapshot", metavar="ID")
    p.add_argument(
        "--all",
        dest="everything",
        action="store_true",
        help="ship everything the current filters admit",
    )
    p.add_argument("--out", metavar="DIR", help="write archives here (default: the spool)")
    p.add_argument("--label", default="adhoc", help="label folded into the cycle id")
    p.add_argument("--max-archive-bytes", metavar="SIZE", help="chunk threshold, e.g. 1Gi")
    p.add_argument("--dry-run", action="store_true", help="report the plan and stop")
    p.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    p.set_defaults(func=cmd_export)

    p = add("cursor", "Show, clear, or move the sender's diff baseline.")
    cursor_sub = p.add_subparsers(dest="action", metavar="<action>")
    # Each action repeats the global options as a parent, since argparse only
    # offers a subcommand's own flags after that subcommand is named.
    cursor_sub.add_parser(
        "show", help="the current baseline", parents=[globals_parser]
    ).set_defaults(action="show")
    clear = cursor_sub.add_parser(
        "clear",
        help="force a cold start on the next cycle",
        parents=[globals_parser],
    )
    clear.add_argument("-y", "--yes", action="store_true")
    set_p = cursor_sub.add_parser(
        "set",
        help="point the baseline at a retained snapshot",
        parents=[globals_parser],
    )
    set_p.add_argument("cycle_id")
    set_p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_cursor, action="show")

    p = add("forget", "Drop a cycle from the receiver ledger so it reprocesses.")
    p.add_argument("cycle_id")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_forget)

    p = add("replay", "Move a processed archive back into the spool to run again.")
    p.add_argument("cycle_id")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = add("import", "Import a metadata tree directly, for isolating import failures.")
    p.add_argument("path", help="the directory holding per-repository directories")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_import)

    p = add("aql", "Run an AQL query, guarding the projection against row collapse.")
    p.add_argument("query", nargs="?", help='e.g. items.find({"repo":"x"})')
    p.add_argument("--count", metavar="CRITERIA", help="row count for a criteria object")
    p.add_argument("-n", "--limit", type=int, default=50, help="rows to print (0 = all)")
    p.add_argument(
        "--force", action="store_true", help="run a projection that omits the item key"
    )
    p.set_defaults(func=cmd_aql)

    return parser


# -- commands ---------------------------------------------------------------


def cmd_status(args) -> int:
    settings = common.load_settings(args)
    data = views.status(settings, offline=args.offline)
    if args.json:
        render.emit_json(data)
        return 0

    art = data["artifactory"]
    store = data["binarystore"]
    header = [
        ("mode", f"{data['mode']} ({data['instance_name']})"),
        ("version", data["version"]),
        ("daemon", _daemon_text(data["daemon_running"])),
        ("cycle", f"every {data['cycle_seconds']}s"),
    ]
    if art.get("checked"):
        header.append(
            (
                "artifactory",
                f"{render.status_mark(art.get('reachable'))} {data['artifactory_url']}"
                + (f" ({art['latency_ms']}ms)" if art.get("latency_ms") is not None else "")
                + (f" {art['error']}" if art.get("error") else ""),
            )
        )
    if store.get("checked"):
        header.append(
            (
                "binarystore",
                f"{render.status_mark(bool(store.get('backend')))} "
                + (store.get("detail") or store.get("error") or ""),
            )
        )

    spool = data["spool"]
    spool_lines = [
        ("pending", f"{spool['pending_count']} archive(s), {human_bytes(spool['pending_bytes'])}"),
        ("oldest", fmt_age(spool["oldest_pending_at"])),
        (
            "free",
            f"{human_bytes(spool['free_bytes'])}"
            + (" BELOW MINIMUM" if spool["free_below_minimum"] else ""),
        ),
    ]
    if spool["partials"]:
        spool_lines.append(("partials", str(spool["partials"])))

    parts = [
        render.section("airlift", render.kv(header)),
        render.section("spool", render.kv(spool_lines)),
    ]

    if "sender" in data:
        s = data["sender"]
        last = s.get("last_cycle") or {}
        rows = [
            ("baseline", f"{s['cursor_cycle_id'] or '(none, next cycle is a cold start)'}"),
            ("last success", fmt_age(s["last_success_at"])),
            (
                "last cycle",
                f"{last.get('status', '-')} {fmt_age(last.get('at'))}"
                + (f" ({last['note']})" if last.get("note") else "")
                if last
                else "(no ledger yet)",
            ),
            ("snapshots", f"{s['snapshot_count']} retained, newest {s['newest_snapshot_entries']} artifact(s)"),
            ("metadata trees", str(s["synthesised_trees"])),
            ("deferred blobs", str(s["deferred_blobs"])),
            ("delete brake", f"max_delete_fraction {s['max_delete_fraction']}"),
        ]
        if s["included_repos"]:
            rows.append(("allowlist", ", ".join(s["included_repos"])))
        if s["recent_statuses"]:
            rows.append(("recent", _counter_text(s["recent_statuses"])))
        parts.append(render.section("sender", render.kv(rows)))
    else:
        r = data["receiver"]
        last = r.get("last_cycle") or {}
        rows = [
            ("ledger rows", str(r["ledger_rows"])),
            (
                "last cycle",
                f"{last.get('cycle_id', '-')} {last.get('status', '-')} "
                f"{fmt_age(last.get('processed_at'))}",
            ),
            ("done archives", str(r["done_archives"])),
        ]
        if r["recent_statuses"]:
            rows.append(("recent", _counter_text(r["recent_statuses"])))
        for waiting in r.get("incomplete_chunk_sets", []):
            rows.append(
                (
                    "waiting",
                    f"{waiting['parent_cycle_id']} missing chunk(s) "
                    f"{waiting['missing']} of {waiting['chunk_total']}",
                )
            )
        parts.append(render.section("receiver", render.kv(rows)))

    print(render.blocks(*parts))
    return 0


def _daemon_text(running) -> str:
    if running is None:
        return "unknown (cannot read the lock)"
    return "running (holds the state lock)" if running else "not running"


def _counter_text(counts: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def cmd_config(args) -> int:
    settings = common.load_settings(args)
    data = views.effective_config(
        settings, common.config_file_values(args), common.config_path_for(args)
    )
    if args.changed:
        data["fields"] = [f for f in data["fields"] if f["origin"] != "default"]
    if args.json:
        render.emit_json(data)
        return 0

    rows = []
    for field in data["fields"]:
        note = ""
        if "masked_file_value" in field:
            note = f"masks file value {field['masked_file_value']!r}"
        rows.append([field["name"], field["value"], field["origin"], note])
    print(
        render.blocks(
            render.kv(
                [
                    ("config file", data["config_path"]),
                    ("present", "yes" if data["config_present"] else "no"),
                ]
            ),
            render.table(["setting", "value", "origin", "note"], rows),
        )
    )
    return 0


def cmd_doctor(args) -> int:
    settings = common.load_settings(args)
    data = views.doctor(settings, write_probe=args.write_probe)
    if args.json:
        render.emit_json(data)
        return 0 if data["ok"] else 2
    rows = [
        [render.status_mark(c["ok"]), c["name"], c["detail"]] for c in data["checks"]
    ]
    print(render.table(["", "check", "detail"], rows))
    if not data["ok"]:
        print(f"\n{data['failed']} check(s) failed.")
    return 0 if data["ok"] else 2


def cmd_repos(args) -> int:
    settings = common.load_settings(args)
    data = views.repos(settings, counts=args.counts)
    rows = data["repos"]
    if args.synced:
        rows = [r for r in rows if r["synced"]]
        data = {**data, "repos": rows}
    if args.json:
        render.emit_json(data)
        return 0
    headers = ["repository", "type", "package", "synced"]
    if args.counts:
        headers.append("artifacts")
    headers.append("reason")
    table_rows = []
    for r in rows:
        row = [r["key"], r["type"], r["package_type"], "yes" if r["synced"] else "no"]
        if args.counts:
            row.append(r.get("artifacts", ""))
        row.append(r["reason"])
        table_rows.append(row)
    summary = f"{data['synced_count']} of {data['total']} repositories sync"
    if data["allowlist_active"]:
        summary += " (allowlist active)"
    print(render.blocks(render.table(headers, table_rows), summary))
    return 0


def cmd_cycles(args) -> int:
    settings = common.load_settings(args)
    data = views.cycles(
        settings,
        limit=args.limit,
        since=common.parse_when(args.since) if args.since else None,
        until=common.parse_when(args.until) if args.until else None,
        status_filter=args.status,
        repo=args.repo,
        include_quiet=args.include_quiet,
    )
    if args.json:
        render.emit_json(data)
        return 0
    if not data["ledger_present"]:
        note = (
            "no ledger yet at "
            f"{data['ledger']}"
            + (
                "; the sender records one from 0.17.0 onwards"
                if settings.mode == "sender"
                else ""
            )
        )
        print(note)
        return 0
    rows = []
    for c in data["cycles"]:
        rows.append(
            [
                c["cycle_id"] or "-",
                fmt_ts(c["at"]),
                c["status"],
                c["chunk"],
                f"+{c['added']}" if c["added"] else "",
                f"-{c['removed']}" if c["removed"] else "",
                human_bytes(c["bytes"]) if c["bytes"] else "",
                ",".join(c["repos"][:3]) + ("..." if len(c["repos"]) > 3 else ""),
                c["note"],
            ]
        )
    footer = f"{data['shown']} of {data['matched']} cycle(s)"
    if not args.include_quiet:
        footer += "; --all includes cycles that did nothing"
    print(render.blocks(
        render.table(
            ["cycle", "when (utc)", "status", "chunk", "added", "removed", "size", "repos", "note"],
            rows,
        ),
        footer,
    ))
    return 0


def cmd_show(args) -> int:
    settings = common.load_settings(args)
    data = views.show_cycle(settings, args.cycle_id)
    if args.json:
        render.emit_json(data)
        return 0

    parts = [render.section("cycle", render.kv([("id", data["cycle_id"]), ("side", data["mode"])]))]
    for row in data["ledger_rows"]:
        pairs = [(k, _cell(v)) for k, v in sorted(row.items()) if k != "cycle_id"]
        parts.append(render.section("ledger", render.kv(pairs)))
    if "snapshot" in data:
        s = data["snapshot"]
        parts.append(
            render.section(
                "snapshot",
                render.kv([("path", s["path"]), ("entries", s["entries"]),
                           ("size", human_bytes(s["bytes"]))]),
            )
        )
    if "metadata_tree" in data:
        t = data["metadata_tree"]
        parts.append(
            render.section(
                "metadata tree",
                render.kv([("path", t["path"]), ("repos", ", ".join(t["repos"]) or "-")]),
            )
        )
    for entry in data["archives"]:
        manifest = entry.get("manifest", {})
        pairs = [
            ("path", entry["path"]),
            ("where", entry["where"]),
            ("size", human_bytes(entry["bytes"])),
        ]
        if manifest:
            pairs += [
                ("chunk", f"{manifest['chunk_seq']}/{manifest['chunk_total']}"),
                ("blobs", manifest["blob_count"]),
                ("uncompressed", human_bytes(manifest["total_bytes"])),
                ("repos", ", ".join(manifest["repos"]) or "-"),
                ("removed", manifest["removed_count"]),
                ("source", manifest["source_instance"]),
                ("created", fmt_ts(manifest["created_at"])),
            ]
        if entry.get("error"):
            pairs.append(("error", entry["error"]))
        parts.append(render.section("archive", render.kv(pairs)))
    print(render.blocks(*parts))
    return 0


def cmd_snapshots(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "sender", "snapshots")
    data = views.snapshots(settings)
    if args.json:
        render.emit_json(data)
        return 0
    rows = [
        [
            s["cycle_id"],
            fmt_ts(s["at"]),
            s["entries"],
            human_bytes(s["bytes"]),
            "baseline" if s["is_baseline"] else "",
        ]
        for s in data["snapshots"]
    ]
    retention = data["retention"]
    print(
        render.blocks(
            render.table(["snapshot", "written (utc)", "artifacts", "size", ""], rows),
            f"retention: {retention['hours']}h / {retention['days']}d / "
            f"{retention['months']}mo (union across tiers)",
        )
    )
    return 0


def cmd_diff(args) -> int:
    settings = common.load_settings(args)
    data = views.snapshot_diff(
        settings, args.previous, args.current, repo=args.repo, full=args.full
    )
    if args.json:
        render.emit_json(data)
        return 0
    parts = [
        render.section(
            "diff",
            render.kv(
                [
                    ("previous", f"{Path(data['previous']).name} ({data['baseline_entries']} artifacts)"),
                    ("current", f"{Path(data['current']).name} ({data['current_entries']} artifacts)"),
                    ("added", f"{data['added']} ({human_bytes(data['added_bytes'])})"),
                    ("removed", f"{data['removed']} ({data['delete_fraction']:.1%} of the baseline)"),
                    ("brake", f"limit {data['max_delete_fraction']}"
                              + (" WOULD REFUSE THIS CYCLE" if data["would_trip_brake"] else "")),
                ]
            ),
        )
    ]
    if data["repos_added"] or data["repos_removed"]:
        repos = sorted(set(data["repos_added"]) | set(data["repos_removed"]))
        rows = [
            [r, data["repos_added"].get(r, ""), data["repos_removed"].get(r, "")]
            for r in repos
        ]
        parts.append(render.section("per repository", render.table(["repository", "added", "removed"], rows)))
    if args.full:
        for label, key in (("added entries", "added_entries"), ("removed entries", "removed_entries")):
            rows = [[e["repo"], e["path"], e["sha1"][:12], human_bytes(e["size"])] for e in data.get(key, [])]
            if rows:
                parts.append(render.section(label, render.table(["repository", "path", "sha1", "size"], rows)))
    print(render.blocks(*parts))
    return 0


def cmd_archives(args) -> int:
    settings = common.load_settings(args)
    data = views.archives(settings, where=args.where)
    if args.json:
        render.emit_json(data)
        return 0
    rows = [
        [
            a["name"],
            a["where"],
            fmt_ts(a["at"]),
            a.get("chunk", ""),
            a.get("blob_count", ""),
            human_bytes(a["bytes"]),
            ",".join(a.get("repos", [])[:3]),
            a.get("error", ""),
        ]
        for a in data["archives"]
    ]
    print(render.table(["archive", "where", "written (utc)", "chunk", "blobs", "size", "repos", ""], rows))
    return 0


def cmd_archive(args) -> int:
    settings = common.load_settings(args)
    if args.unpack:
        path = views.find_archive(settings, args.ref)
        dest = Path(args.unpack)
        manifest = archive_mod.extract(path, dest)
        data = {
            "path": str(path),
            "unpacked_to": str(dest),
            "manifest": views._manifest_summary(manifest),
        }
        if args.json:
            render.emit_json(data)
        else:
            print(f"Extracted {path} to {dest}")
        return 0

    data = views.inspect_archive(
        settings, args.ref, entries=args.entries, verify=args.verify
    )
    if args.json:
        render.emit_json(data)
        return 0 if not args.verify or data["verification"]["ok"] else 2

    manifest = data["manifest"]
    parts = [
        render.section(
            "archive",
            render.kv(
                [
                    ("path", data["path"]),
                    ("size", human_bytes(data["bytes"])),
                    ("schema", manifest["schema"]),
                    ("cycle", manifest["cycle_id"]),
                    ("parent", manifest["parent_cycle_id"]),
                    ("chunk", f"{manifest['chunk_seq']}/{manifest['chunk_total']}"),
                    ("previous", manifest["prev_cycle_id"] or "-"),
                    ("source", manifest["source_instance"]),
                    ("created", fmt_ts(manifest["created_at"])),
                    ("blobs", f"{manifest['blob_count']} ({human_bytes(manifest['total_bytes'])} uncompressed)"),
                    ("entries", manifest["entry_count"]),
                    ("removed", manifest["removed_count"]),
                    ("repos", ", ".join(manifest["repos"]) or "-"),
                ]
            ),
        )
    ]
    if args.entries:
        rows = [[e.get("repo"), e.get("path"), (e.get("sha1") or "")[:12], human_bytes(e.get("size", 0))]
                for e in data.get("entries", [])]
        parts.append(render.section("entries", render.table(["repository", "path", "sha1", "size"], rows)))
        removed_rows = [[r.get("repo"), r.get("path")] for r in data.get("removed", [])]
        if removed_rows:
            parts.append(render.section("removed", render.table(["repository", "path"], removed_rows)))
    if args.verify:
        v = data["verification"]
        pairs = [("blobs checked", v["blobs_checked"]), ("result", "OK" if v["ok"] else "MISMATCH")]
        parts.append(render.section("verification", render.kv(pairs)))
        for line in v["mismatches"]:
            parts.append(f"  {line}")
    print(render.blocks(*parts))
    if args.verify and not data["verification"]["ok"]:
        return 2
    return 0


def cmd_blob(args) -> int:
    settings = common.load_settings(args)
    data = views.blob(settings, args.sha1, get=Path(args.get) if args.get else None)
    if args.json:
        render.emit_json(data)
        return 0 if data.get("present") else 1
    pairs = [
        ("sha1", data["sha1"]),
        ("backend", data["backend"]),
        ("location", data["location"]),
        ("present", "yes" if data.get("present") else "no"),
    ]
    if data.get("present"):
        pairs.append(("size", human_bytes(data["size"])))
    if "written_to" in data:
        pairs += [
            ("written to", data["written_to"]),
            ("sha1 verified", "yes" if data["sha1_verified"] else "NO, content does not match"),
        ]
    print(render.kv(pairs))
    if not data.get("present"):
        print(
            "\nNot found. A blob that is not where airlift looked and one Artifactory\n"
            "has not written yet both read the same way; check the location above\n"
            "against a blob known to exist before assuming the prefix is wrong."
        )
        return 1
    return 0


def cmd_aql(args) -> int:
    settings = common.load_settings(args)
    if not args.query and not args.count:
        raise CLIError("give a query, or --count with a criteria object")
    data = views.run_aql(
        settings,
        args.query,
        count_criteria=args.count,
        force=args.force,
        limit=args.limit,
    )
    if args.json:
        render.emit_json(data)
        return 0
    if args.count:
        print(f"{data['total']} row(s) match {data['criteria']}")
        return 0
    for warning in data["warnings"]:
        print(f"WARNING: {warning}")
    results = data["results"]
    if not results:
        print(f"0 rows for {data['query']}")
        return 0
    headers: list[str] = []
    for row in results:
        for key in row:
            if key not in headers:
                headers.append(key)
    ordered = [k for k in ("repo", "path", "name") if k in headers]
    ordered += [k for k in headers if k not in ordered]
    rows = [[_cell(row.get(h)) for h in ordered] for row in results]
    print(render.table(ordered, rows))
    print(f"\n{data['rows']} row(s)" + (" (output truncated, use -n 0)" if data["truncated"] else ""))
    return 0


def cmd_plan(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "sender", "plan")
    data = actions.plan_next_cycle(settings)
    if args.json:
        render.emit_json(data)
        return 0
    pairs = [
        ("baseline", f"{data['baseline'] or '(none)'} ({data['baseline_entries']} artifacts)"),
        ("source now", f"{data['source_entries']} artifacts"),
        ("would add", f"{data['added']} ({human_bytes(data['raw_bytes'])} raw, {data['chunks']} chunk(s))"),
        ("would remove", f"{data['removed']} ({data['delete_fraction']:.1%} of the baseline)"),
        ("outcome", data["outcome"]),
    ]
    if data["cold_start"]:
        pairs.append(("note", "cold start: no removals would be emitted"))
    if data["pending_archives"]:
        pairs.append(("pending", ", ".join(data["pending_archives"])))
    parts = [render.section("next cycle", render.kv(pairs))]
    if data["repos_added"] or data["repos_removed"]:
        repos = sorted(set(data["repos_added"]) | set(data["repos_removed"]))
        parts.append(
            render.section(
                "per repository",
                render.table(
                    ["repository", "added", "removed"],
                    [[r, data["repos_added"].get(r, ""), data["repos_removed"].get(r, "")] for r in repos],
                ),
            )
        )
    if data["outcome"] == "brake-refused":
        parts.append(
            f"The deletion brake would refuse this cycle (limit "
            f"{data['max_delete_fraction']}). If the scope was narrowed on "
            f"purpose, run 'airlift cursor clear' to make the next cycle a "
            f"cold start."
        )
    print(render.blocks(*parts))
    return 0


def cmd_export(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "sender", "export")
    out_dir = Path(args.out) if args.out else None
    max_bytes = None
    if args.max_archive_bytes:
        from ..config import _parse_byte_size

        max_bytes = _parse_byte_size(args.max_archive_bytes)

    label = "".join(c for c in args.label.lower() if c.isalnum())[:16] or "adhoc"
    selection = dict(
        since=common.parse_when(args.since) if args.since else None,
        until=common.parse_when(args.until) if args.until else None,
        time_field=args.time_field,
        repos=args.repo,
        artifacts=args.artifact,
        from_snapshot=args.from_snapshot,
        to_snapshot=args.to_snapshot,
        everything=args.everything,
    )

    if not args.dry_run:
        target = out_dir or settings.spool_dir
        note = (
            "Archives written to the spool are picked up by the transport, and "
            "hold the next ordinary cycle until they are drained."
            if target == settings.spool_dir
            else f"Archives will be written to {target}."
        )
        actions.confirm(settings, note, assume_yes=args.yes)

    with actions.cli_lock(settings):
        data = actions.export(
            settings,
            out_dir=out_dir,
            label=label,
            dry_run=args.dry_run,
            max_archive_bytes=max_bytes,
            **selection,
        )
    if args.json:
        render.emit_json(data)
        return 0

    pairs = [
        ("selection", _cell(data["selection"])),
        ("artifacts", f"{data['entries']} ({human_bytes(data['raw_bytes'])} raw)"),
        ("chunks", data["chunks"]),
        ("destination", data["out_dir"]),
    ]
    if data.get("dry_run"):
        print(render.blocks(render.section("export plan (dry run)", render.kv(pairs))))
        return 0
    pairs.insert(0, ("cycle", data["cycle_id"]))
    pairs.append(("archives", _cell(data["archives"])))
    pairs.append(("written", human_bytes(data["archive_bytes"])))
    if data.get("unresolved"):
        pairs.append(("dropped", f"{data['unresolved']} artifact(s) no longer on the source"))
    if data.get("deferred_blobs"):
        pairs.append(("deferred", f"{data['deferred_blobs']} blob(s) the store could not serve"))
    print(render.blocks(render.section("export", render.kv(pairs))))
    return 0


def cmd_cursor(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "sender", "cursor")
    action = getattr(args, "action", "show") or "show"

    if action == "show":
        data = actions.cursor_show(settings)
        if args.json:
            render.emit_json(data)
            return 0
        print(
            render.kv(
                [
                    ("path", data["path"]),
                    ("baseline", data["last_cycle_id"] or "(none, next cycle is a cold start)"),
                    ("last success", fmt_age(data["last_success_at"])),
                    (
                        "baseline snapshot",
                        f"{data['baseline_entries']} artifacts"
                        if data["baseline_snapshot_present"]
                        else "MISSING, so the next cycle is a cold start",
                    ),
                ]
            )
        )
        return 0

    if action == "clear":
        actions.confirm(
            settings,
            "Clearing the cursor makes the next cycle a cold start: it emits no "
            "removals and re-adds everything in scope.",
            assume_yes=args.yes,
        )
        with actions.cli_lock(settings):
            data = actions.cursor_clear(settings)
        if args.json:
            render.emit_json(data)
            return 0
        if not data["cleared"]:
            print("No cursor to clear; the next cycle was already a cold start.")
        else:
            print(f"Cleared the cursor (was {data['was']}). The next cycle is a cold start.")
        return 0

    actions.confirm(
        settings,
        f"Moving the baseline to {args.cycle_id} changes what the next diff "
        "compares against.",
        assume_yes=args.yes,
    )
    with actions.cli_lock(settings):
        data = actions.cursor_set(settings, args.cycle_id)
    if args.json:
        render.emit_json(data)
        return 0
    print(f"Baseline moved from {data['was']} to {data['now']} ({data['entries']} artifacts).")
    return 0


def cmd_forget(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "receiver", "forget")
    actions.confirm(
        settings,
        f"Forgetting {args.cycle_id} lets its archive be processed again if it "
        "is still in the spool.",
        assume_yes=args.yes,
    )
    with actions.cli_lock(settings):
        data = actions.ledger_forget(settings, args.cycle_id)
    if args.json:
        render.emit_json(data)
        return 0
    print(f"Dropped {data['rows_dropped']} ledger row(s) for {data['cycle_id']}.")
    return 0


def cmd_replay(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "receiver", "replay")
    actions.confirm(
        settings,
        f"Replaying {args.cycle_id} moves its archive back into the spool for "
        "the receiver to process again.",
        assume_yes=args.yes,
    )
    with actions.cli_lock(settings):
        data = actions.replay(settings, args.cycle_id)
    if args.json:
        render.emit_json(data)
        return 0
    print(
        render.blocks(
            render.kv(
                [
                    ("cycle", data["cycle_id"]),
                    ("requeued", _cell(data["archives"])),
                    ("ledger rows dropped", data["ledger_rows_dropped"]),
                ]
            ),
            "The receiver picks it up on its next cycle."
            if data["daemon_running"]
            else "The receiver is not running; it will be processed when it starts.",
        )
    )
    return 0


def cmd_import(args) -> int:
    settings = common.load_settings(args)
    common.require_mode(settings, "receiver", "import")
    actions.confirm(
        settings,
        f"Importing {args.path} writes artifacts into this Artifactory.",
        assume_yes=args.yes,
    )
    data = actions.import_tree(settings, Path(args.path))
    if args.json:
        render.emit_json(data)
        return 0 if data["status"] == "ok" else 2
    parts = [
        render.section(
            "import",
            render.kv(
                [
                    ("path", data["path"]),
                    ("repositories", _cell(data["repositories"])),
                    ("status", data["status"]),
                    ("absent-repo notices", data["absent_repo_notices"]),
                ]
            ),
        )
    ]
    for line in data["failures"]:
        parts.append(f"  {line}")
    print(render.blocks(*parts))
    return 0 if data["status"] == "ok" else 2


def _cell(value) -> str:
    """Render one value for a table cell or a key/value line.

    Lists of scalars read better joined than as a Python repr; a list of
    dicts (manifest entries, for instance) is summarised by length because
    the full thing belongs in --json, not in a column.
    """
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in sorted(value.items()))
    if isinstance(value, list):
        if all(isinstance(v, (str, int, float)) for v in value):
            return ", ".join(str(v) for v in value)
        return f"[{len(value)} item(s)]"
    return "" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    # Library code logs as it works (backend selection, retries). That is the
    # point in the daemon and noise here, so it is quietened to warnings and
    # sent to stderr, keeping stdout to the command's own output.
    log.configure("INFO" if args.verbose else "WARNING", stream=sys.stderr)
    try:
        return args.func(args)
    except CLIError as exc:
        print(f"airlift: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
