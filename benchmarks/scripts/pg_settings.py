#!/usr/bin/env python3
"""Snapshot / diff / re-apply Postgres configuration for the dual-arm harness.

This keeps the arm-isolation logic in one dependency-free (stdlib only) place.
It shells out to ``docker exec ... psql`` exactly like ``docker.sh`` does, so it
never needs a Python driver and works against the production container.

A "snapshot" is the set of non-default ``pg_settings`` rows (name/setting/source/
sourcefile/context). Defaults, session/override values and internal settings are
excluded so a snapshot reflects real configuration only.

Subcommands:
  snapshot --container C --output PATH
      Write the current configuration snapshot to PATH.

  reset --container C --db DB
      ALTER SYSTEM RESET ALL, ALTER DATABASE DB RESET ALL, pg_reload_conf().
      Postmaster-level knobs still need a container restart to clear.

  delta --before A.json --after B.json --output PATH
      Write {"changes": {name: {"before": row|null, "after": row|null}}}
      for entries whose (setting, source, sourcefile, context) differ, or which
      were added/removed.

  count --delta PATH
      Print the number of changed/added/removed settings (integer).

  sql --delta PATH
      Print ALTER SYSTEM SET/RESET statements that reproduce the "after" state
      of the delta. Pipe into ``psql`` then reload/restart.

  equals --left A.json --right B.json
      Exit 0 when both snapshots describe the same settings; else print the
      differing names to stderr and exit 1.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

FIELDS = ("setting", "source", "sourcefile", "context")

_QUERY = (
    "SELECT name, setting, source, "
    "coalesce(sourcefile, ''), context "
    "FROM pg_settings "
    "WHERE source NOT IN ('default', 'client', 'session', 'override') "
    "AND context <> 'internal' "
    "ORDER BY name;"
)


def _psql(container: str, sql: str, db: str | None = None) -> str:
    cmd = [
        "docker",
        "exec",
        "-u",
        "postgres",
        container,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-A",
        "-t",
        "-q",
        "-F",
        "\t",
    ]
    if db:
        cmd += ["-d", db]
    cmd += ["-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"psql failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )
    return proc.stdout


def _parse_rows(text: str) -> dict[str, dict[str, str]]:
    settings: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name = parts[0].strip()
        settings[name] = {
            "setting": parts[1],
            "source": parts[2],
            "sourcefile": parts[3],
            "context": parts[4],
        }
    return settings


def snapshot(container: str, output: str) -> dict:
    data = {
        "container": container,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "settings": _parse_rows(_psql(container, _QUERY)),
    }
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    print(f"snapshot: {len(data['settings'])} setting(s) -> {output}")
    return data


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise SystemExit(f"cannot read snapshot {path}: {exc}")


def _row_key(row: dict[str, str] | None) -> tuple | None:
    if row is None:
        return None
    return tuple(row.get(field, "") for field in FIELDS)


def reset(container: str, db: str | None) -> None:
    _psql(container, "ALTER SYSTEM RESET ALL;")
    if db:
        _psql(container, f"ALTER DATABASE {db} RESET ALL;", db=db)
    _psql(container, "SELECT pg_reload_conf();")
    print(f"reset: ALTER SYSTEM RESET ALL and ALTER DATABASE {db or '(none)'} RESET ALL")


def delta(before_path: str, after_path: str, output: str | None) -> dict:
    before = load(before_path).get("settings", {})
    after = load(after_path).get("settings", {})
    changes: dict[str, dict] = {}
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        if _row_key(old) == _row_key(new):
            continue
        changes[name] = {"before": old, "after": new}
    result = {
        "before": before_path,
        "after": after_path,
        "changes": changes,
    }
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
    for name, change in changes.items():
        old = change["before"]
        new = change["after"]
        old_desc = f"{old['setting']} ({old['sourcefile'] or old['source']})" if old else "-"
        new_desc = f"{new['setting']} ({new['sourcefile'] or new['source']})" if new else "-"
        print(f"  {name}: {old_desc} -> {new_desc}")
    print(f"delta: {len(changes)} changed setting(s)")
    if output:
        print(f"wrote {output}")
    return result


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def emit_sql(delta_path: str) -> None:
    changes = load(delta_path).get("changes", {})
    for name, change in sorted(changes.items()):
        new = change.get("after")
        if new is None:
            print(f"ALTER SYSTEM RESET {name};")
        else:
            print(f"ALTER SYSTEM SET {name} = {_sql_literal(new['setting'])};")


def equals(left_path: str, right_path: str) -> int:
    left = load(left_path).get("settings", {})
    right = load(right_path).get("settings", {})
    names = sorted(set(left) | set(right))
    mismatches = [name for name in names if _row_key(left.get(name)) != _row_key(right.get(name))]
    if not mismatches:
        print(f"OK: {left_path} == {right_path} ({len(left)} settings)")
        return 0
    print(
        f"MISMATCH: {len(mismatches)} setting(s) differ between "
        f"{left_path} and {right_path}",
        file=sys.stderr,
    )
    for name in mismatches:
        print(f"  {name}: {left.get(name)} != {right.get(name)}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot")
    p.add_argument("--container", required=True)
    p.add_argument("--output", required=True)

    p = sub.add_parser("reset")
    p.add_argument("--container", required=True)
    p.add_argument("--db", default=None)

    p = sub.add_parser("delta")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--output", default=None)

    p = sub.add_parser("count")
    p.add_argument("--delta", required=True)

    p = sub.add_parser("sql")
    p.add_argument("--delta", required=True)

    p = sub.add_parser("equals")
    p.add_argument("--left", required=True)
    p.add_argument("--right", required=True)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "snapshot":
        snapshot(args.container, args.output)
    elif args.command == "reset":
        reset(args.container, args.db)
    elif args.command == "delta":
        delta(args.before, args.after, args.output)
    elif args.command == "count":
        changes = load(args.delta).get("changes", {})
        print(len(changes))
    elif args.command == "sql":
        emit_sql(args.delta)
    elif args.command == "equals":
        return equals(args.left, args.right)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
