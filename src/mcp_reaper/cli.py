"""Command line interface.

Nothing signals a process unless you pass ``--yes`` to ``reap``. ``plan`` is safe
to run from a scheduled job, a dotfile repo, or an agent.

===== =====================================================================
exit  meaning
----- ---------------------------------------------------------------------
0     no eligible process (nothing to do)
1     eligible processes found but not signalled -- this is what tells a
      wrapper that a ``reap --yes`` run is warranted, replacing the habit of
      matching on human-readable output
2     reaped, every target signalled
3     reaped, but at least one target was skipped or failed
4     usage error, or the plan exceeded ``--max`` and was refused
===== =====================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence

from .proctable import identity, read_table
from .reap import (
    DEFAULT_KEEP,
    DEFAULT_MIN_AGE_SEC,
    DEFAULT_SESSION_HOSTS,
    build_plan,
    execute,
    redact_command,
    session_root,
)
from .signature import classify, load_rules


def _is_host(command: str, patterns: Sequence[str]) -> bool:
    """The same whole-word rule the planner uses, so `explain` cannot disagree.

    `explain` is the command an operator reads before they trust `reap`; if it
    listed a session host as a candidate while the planner protected it, the two
    outputs would contradict each other exactly where the tool claims certainty.
    """
    tokens = (command or "").split()
    base = os.path.basename(tokens[0]).lower() if tokens else ""
    for pattern in patterns or ():
        needle = pattern.lower()
        if (" " in needle or "/" in needle) and needle in (command or "").lower():
            return True
        if base == needle:
            return True
        if base.startswith(needle) and base[len(needle):len(needle) + 1] in ("-", "_", "."):
            return True
    return False


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help="servers to keep per (session, type) group (default %(default)s)")
    parser.add_argument("--min-age", type=int, default=DEFAULT_MIN_AGE_SEC,
                        help="never signal a server younger than this many seconds (default %(default)s)")
    parser.add_argument("--rules", metavar="FILE",
                        help="JSON file pinning exact labels for commands the shape heuristic cannot name")
    parser.add_argument("--marker", default="mcp",
                        help="substring a command must contain to count as an MCP server (default %(default)s)")
    parser.add_argument("--session-host", action="append", default=[],
                        help="pattern naming an agent client process; repeatable")
    parser.add_argument("--uid", type=int, help="restrict to a uid (default: current user)")
    parser.add_argument("--reap-ambiguous", action="store_true",
                        help="also reap groups with no identifiable session host (unsafe)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def _collect(args: argparse.Namespace):
    return load_rules(args.rules), tuple(args.session_host) or DEFAULT_SESSION_HOSTS, read_table(uid=args.uid)


def _plan(args: argparse.Namespace, rules, session_hosts, table):
    return build_plan(
        table,
        rules=rules,
        session_hosts=session_hosts,
        keep=args.keep,
        min_age_sec=args.min_age,
        marker=args.marker,
        reap_ambiguous=args.reap_ambiguous,
        uid=args.uid,
    )


def _show_plan(args: argparse.Namespace, plan) -> None:
    if args.json:
        print(json.dumps(plan.to_dict(args.keep, args.min_age), ensure_ascii=False))
        return
    for group in plan.groups:
        flag = "  [no session host -> left alone]" if group.ambiguous else ""
        print(f"[{group.type}] session_root={group.session_root} total={len(group.members)}{flag}")
        print(f"  keep     : {[pid for _age, pid in group.kept(args.keep)]}")
        print(f"  eligible : {[pid for _age, pid in group.excess(args.keep, args.min_age)]}")
    if not plan.reap:
        print("nothing eligible to reap")
        if plan.deferred_ambiguous:
            print(f"{len(plan.deferred_ambiguous)} left alone: no session host could be identified")
        if plan.too_young:
            print(f"{len(plan.too_young)} left alone: younger than --min-age")
        return
    print(f"\nwould SIGTERM {len(plan.reap)}: {[t.pid for t in plan.reap]}")


def cmd_plan(args: argparse.Namespace) -> int:
    rules, session_hosts, table = _collect(args)
    plan = _plan(args, rules, session_hosts, table)
    _show_plan(args, plan)
    return 1 if plan.reap else 0


def cmd_reap(args: argparse.Namespace) -> int:
    rules, session_hosts, table = _collect(args)
    plan = _plan(args, rules, session_hosts, table)
    if len(plan.reap) > args.max:
        print(
            f"refusing: plan would signal {len(plan.reap)} processes, over --max {args.max}. "
            "Inspect with `plan` first, then raise --max deliberately.",
            file=sys.stderr,
        )
        return 4
    if not plan.reap:
        _show_plan(args, plan)
        return 0
    if not args.yes:
        _show_plan(args, plan)
        print("\nnot signalled: re-run with --yes", file=sys.stderr)
        return 1
    identities = {target.pid: identity(target.pid) for target in plan.reap}
    result = execute(
        plan,
        identities,
        rules=rules,
        session_hosts=session_hosts,
        keep=args.keep,
        min_age_sec=args.min_age,
        marker=args.marker,
        reap_ambiguous=args.reap_ambiguous,
        uid=args.uid,
        read_table=read_table,
        identity=identity,
    )
    if args.json:
        payload = result.to_dict()
        payload["eligible"] = len(plan.reap)
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for pid in result.signalled:
            print(f"SIGTERM {pid}")
        for item in result.skipped:
            print(f"skip {item['pid']}: {item['reason']}")
        for item in result.failed:
            print(f"failed {item['pid']}: {item['reason']}")
    if result.signalled and result.clean:
        return 2
    return 3 if (result.signalled or result.skipped or result.failed) else 0


def cmd_explain(args: argparse.Namespace) -> int:
    """List every process that would be considered a server, and its session root."""
    rules, session_hosts, table = _collect(args)
    rows = []
    protected = {os.getpid()}
    for pid, row in sorted(table.items()):
        label = classify(row.command, rules, marker=args.marker)
        if label is None or pid in protected:
            continue
        if _is_host(row.command, session_hosts):
            continue
        rows.append({
            "pid": pid,
            "type": label,
            "age_sec": row.age_sec,
            "session_root": session_root(pid, table, session_hosts),
            "command": redact_command(row.command)[:180],
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            root = row["session_root"]
            print(f"{row['pid']:>7}  {row['type']:<28} root={root if root else '-'}  {row['command']}")
        if not rows:
            print(f"no process command contains {args.marker!r} or matches --rules")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-reaper",
        description="Reap duplicated stdio MCP server processes left behind by agent clients.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="show what would be signalled (read-only)")
    _add_common(plan)
    plan.set_defaults(func=cmd_plan)

    reap = sub.add_parser("reap", help="signal the plan, only with --yes")
    _add_common(reap)
    reap.add_argument("--yes", action="store_true", help="actually send SIGTERM")
    reap.add_argument("--max", type=int, default=200,
                      help="refuse to signal more than this many processes (default %(default)s)")
    reap.set_defaults(func=cmd_reap)

    explain = sub.add_parser("explain", help="list candidate processes and their session roots")
    _add_common(explain)
    explain.set_defaults(func=cmd_explain)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.keep < 1:
        print("error: --keep must be at least 1", file=sys.stderr)
        return 4
    if len(args.marker or "") < 2:
        print("error: --marker must be at least 2 characters; an empty marker would make "
              "every process on the machine a candidate", file=sys.stderr)
        return 4
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, ValueError) as exc:
        # a bad rules file must not exit 1, which is the wrapper's signal that
        # there is something to reap
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
