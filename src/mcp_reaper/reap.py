"""Group leaked stdio MCP servers per session and build a revalidation-safe plan.

Why grouping by session is the whole trick: an MCP server that looks like a
duplicate is not necessarily a leak. Two identical servers can belong to two
live client sessions, and killing the older one breaks a working session. So a
group is ``(nearest session-host ancestor, server label)`` and only duplicates
*beyond* ``keep`` inside one group are candidates -- a single session holding
forty copies of the same server is a leak, forty copies across forty sessions is
not.

Where no session host can be identified, the group is ambiguous and nothing is
reaped from it by default. Being unable to tell whose session a process belongs
to is not the same as being allowed to guess.
"""

from __future__ import annotations

import os
import re
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .proctable import Row, ancestry, identity as read_identity, read_table
from .signature import Rule, classify

DEFAULT_SESSION_HOSTS: tuple[str, ...] = (
    "claude",
    "codex",
    "gemini",
    "cursor-agent",
    "opencode",
    "aider",
    "goose",
    "openhands",
    "kimi",
)
DEFAULT_KEEP = 1
DEFAULT_MIN_AGE_SEC = 120


_BOUNDARIES = ("", "-", "_", ".", ":")


def _is_session_host(row: Row, patterns: Sequence[str]) -> bool:
    """Does this process look like an agent client that owns MCP servers?

    Matching is on whole-word boundaries, not prefixes. A prefix test on "goose"
    also matches ``gooseberry-mcp-server``, which would then be treated as a
    session host -- merging two sessions' servers into one group and letting the
    reaper SIGTERM a server that a live session is using. That is the worst
    failure this tool can have, so an inexact match is not a match.
    """
    command = row.command or ""
    low = command.lower()
    tokens = command.split()
    base = Path(tokens[0]).name.lower() if tokens else ""
    for pattern in patterns:
        needle = pattern.lower()
        if " " in needle or "/" in needle:
            if needle in low:
                return True
            continue
        if base == needle:
            return True
        if base.startswith(needle) and base[len(needle):len(needle) + 1] in ("-", "_", "."):
            return True
    return False


def session_root(pid: int, table: Mapping[int, Row], patterns: Sequence[str]) -> int | None:
    """Nearest ancestor that looks like an agent client session, or None."""
    for ancestor in ancestry(pid, table):
        row = table.get(ancestor)
        if row is not None and _is_session_host(row, patterns):
            return ancestor
    return None


_SECRETISH = re.compile(
    r"(?P<flag>--?(?:token|api[-_]?key|secret|password|auth|bearer))[= ](?P<val>\S+)",
    re.IGNORECASE,
)


def redact_command(command: str) -> str:
    """Strip credential-looking arguments before anything reaches stdout.

    ``ps`` shows the full command line, and MCP servers are routinely launched with
    ``--token`` or ``--api-key`` in argv. A reaper whose plan output ends up in a
    world-readable log or a CI artefact must not become a token collector.
    """
    def swap(match: "re.Match[str]") -> str:
        return match.group("flag") + "=[redacted]"

    return _SECRETISH.sub(swap, command or "")


@dataclass
class Target:
    pid: int
    type: str
    session_root: int | None
    age_sec: int
    command: str

    def key(self) -> tuple:
        return (self.pid, self.type, self.session_root)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "type": self.type,
            "session_root": self.session_root,
            "age_sec": self.age_sec,
            "command": redact_command(self.command)[:160],
        }


@dataclass
class Group:
    type: str
    session_root: int | None
    members: list[tuple[int, int]] = field(default_factory=list)  # (age_sec, pid), youngest first

    @property
    def ambiguous(self) -> bool:
        return self.session_root is None

    def kept(self, keep: int) -> list[tuple[int, int]]:
        return self.members[:keep]

    def excess(self, keep: int, min_age_sec: int) -> list[tuple[int, int]]:
        return [(age, pid) for age, pid in self.members[keep:] if age >= min_age_sec]

    def to_dict(self, keep: int, min_age_sec: int) -> dict:
        return {
            "type": self.type,
            "session_root": self.session_root,
            "ambiguous": self.ambiguous,
            "total": len(self.members),
            "keep": keep,
            "kept": [{"age_sec": age, "pid": pid} for age, pid in self.kept(keep)],
            "eligible": [{"age_sec": age, "pid": pid} for age, pid in self.excess(keep, min_age_sec)],
        }


@dataclass
class Plan:
    groups: list[Group]
    reap: list[Target]
    deferred_ambiguous: list[Target]
    too_young: list[Target]

    @property
    def eligible_count(self) -> int:
        return len(self.reap)

    def to_dict(self, keep: int, min_age_sec: int) -> dict:
        return {
            "keep": keep,
            "min_age_sec": min_age_sec,
            "groups": [g.to_dict(keep, min_age_sec) for g in self.groups],
            "reap": [t.to_dict() for t in self.reap],
            "deferred_ambiguous": [t.to_dict() for t in self.deferred_ambiguous],
            "too_young": [t.to_dict() for t in self.too_young],
            "eligible": len(self.reap),
            "ambiguous_excluded": len(self.deferred_ambiguous),
        }


def build_plan(
    table: Mapping[int, Row],
    *,
    rules: Sequence[Rule] = (),
    session_hosts: Sequence[str] = DEFAULT_SESSION_HOSTS,
    keep: int = DEFAULT_KEEP,
    min_age_sec: int = DEFAULT_MIN_AGE_SEC,
    reap_ambiguous: bool = False,
    marker: str = "mcp",
    uid: int | None = None,
    self_pid: int | None = None,
) -> Plan:
    own = os.getpid() if self_pid is None else self_pid
    protected = {own, *ancestry(own, table)}
    buckets: dict[tuple, Group] = {}

    for pid, row in table.items():
        if pid in protected:
            continue
        if uid is not None and row.uid != uid:
            continue
        if _is_session_host(row, session_hosts):
            # a client binary that mentions "mcp" in its own command line is not
            # a leaked server; reaping it would end the session it hosts
            continue
        label = classify(row.command, rules, marker=marker)
        if label is None:
            continue
        root = session_root(pid, table, session_hosts)
        group = buckets.setdefault((root, label), Group(type=label, session_root=root))
        group.members.append((row.age_sec, pid))

    groups: list[Group] = []
    reap: list[Target] = []
    deferred: list[Target] = []
    too_young: list[Target] = []
    for (root, label), group in sorted(buckets.items(), key=lambda kv: (str(kv[0][1]), kv[0][0] or 0)):
        group.members.sort()
        groups.append(group)
        for age, pid in group.members[keep:]:
            target = Target(pid=pid, type=label, session_root=root, age_sec=age,
                             command=table[pid].command)
            if age < min_age_sec:
                too_young.append(target)
            elif group.ambiguous and not reap_ambiguous:
                deferred.append(target)
            else:
                reap.append(target)
    return Plan(groups=groups, reap=reap, deferred_ambiguous=deferred, too_young=too_young)


@dataclass
class ReapResult:
    signalled: list[int] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.skipped and not self.failed

    def to_dict(self) -> dict:
        return {
            "signalled": self.signalled,
            "skipped": self.skipped,
            "failed": self.failed,
            "signalled_count": len(self.signalled),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
        }


def execute(
    plan: Plan,
    identities: Mapping[int, str | None],
    *,
    rules: Sequence[Rule] = (),
    session_hosts: Sequence[str] = DEFAULT_SESSION_HOSTS,
    keep: int = DEFAULT_KEEP,
    min_age_sec: int = DEFAULT_MIN_AGE_SEC,
    reap_ambiguous: bool = False,
    marker: str = "mcp",
    uid: int | None = None,
    read_table=read_table,
    identity=read_identity,
    terminate=None,
) -> ReapResult:
    """Signal the plan only after re-deriving it from a freshly read table.

    Three things must be unchanged for every PID: the process identity string
    (uid, absolute start time, command), its membership in the eligible set
    recomputed from the new table, and its identity again after that recomputation.
    A PID that was recycled, reparented, or reclassified drops out of the plan.
    """
    if terminate is None:
        def terminate(pid: int) -> None:
            os.kill(pid, signal.SIGTERM)

    result = ReapResult()
    for target in plan.reap:
        before = identity(target.pid)
        if before is None or before != identities.get(target.pid):
            result.skipped.append({"pid": target.pid, "reason": "identity_changed"})
            continue
        fresh = read_table(uid=uid)
        if fresh.get(target.pid) is None:
            result.skipped.append({"pid": target.pid, "reason": "gone"})
            continue
        replanned = build_plan(
            fresh,
            rules=rules,
            session_hosts=session_hosts,
            keep=keep,
            min_age_sec=min_age_sec,
            reap_ambiguous=reap_ambiguous,
            marker=marker,
            uid=uid,
        )
        if target.key() not in {t.key() for t in replanned.reap}:
            result.skipped.append({"pid": target.pid, "reason": "no_longer_eligible"})
            continue
        if identity(target.pid) != before:
            result.skipped.append({"pid": target.pid, "reason": "identity_changed"})
            continue
        try:
            terminate(target.pid)
            result.signalled.append(target.pid)
        except OSError as exc:
            result.failed.append({"pid": target.pid, "reason": type(exc).__name__})
    return result
