"""Parse the local process table into rows the reaper can reason about.

Only POSIX ``ps`` output is used, so there is no dependency and no /proc
assumption: the same code runs on macOS and Linux.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence

_PS_HEADER_RE = re.compile(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)")
ANCESTRY_GUARD = 64


class Row(Mapping):
    """One process: pid, ppid, age in seconds, command, uid."""

    __slots__ = ("pid", "ppid", "age_sec", "command", "uid")

    def __init__(self, pid: int, ppid: int, age_sec: int, command: str, uid: int) -> None:
        self.pid, self.ppid, self.age_sec, self.command, self.uid = pid, ppid, age_sec, command, uid

    def __getitem__(self, key):
        return getattr(self, key)

    def __iter__(self):
        return iter(("pid", "ppid", "age_sec", "command", "uid"))

    def __len__(self):
        return 5

    def __repr__(self):
        return f"Row(pid={self.pid}, ppid={self.ppid}, age_sec={self.age_sec}, command={self.command!r})"


def etime_to_sec(etime: str) -> int:
    """``ps`` elapsed time (``01:30``, ``1-02:03:04``, ``02:03:04``) to seconds."""
    days = 0
    rest = etime
    if "-" in rest:
        day_part, rest = rest.split("-", 1)
        days = int(day_part) * 86400
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days + parts[-3] * 3600 + parts[-2] * 60 + parts[-1]


def parse_ps(stdout: str, uid: int | None = None) -> dict[int, Row]:
    """Turn ``ps -Ao uid,pid,ppid,etime,command`` into {pid: Row}.

    ``uid`` defaults to the current user: a reaper that can see other users'
    processes can still only signal its own, so filtering here also keeps the
    plan honest.
    """
    wanted = os.getuid() if uid is None else uid
    rows: dict[int, Row] = {}
    for line in stdout.splitlines()[1:]:
        match = _PS_HEADER_RE.match(line)
        if not match:
            continue
        owner, pid, ppid, etime, command = match.groups()
        owner, pid, ppid = int(owner), int(pid), int(ppid)
        if owner != wanted:
            continue
        rows[pid] = Row(pid, ppid, etime_to_sec(etime), command, owner)
    return rows


def read_table(uid: int | None = None) -> dict[int, Row]:
    out = subprocess.run(
        ["ps", "-Ao", "uid,pid,ppid,etime,command"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return parse_ps(out, uid=uid)


def identity(pid: int) -> str | None:
    """uid + absolute start time + command, for re-checking a PID before a signal.

    A PID is reusable. Between planning and signalling, the process this slot
    referred to may have exited and been replaced, so the plan is only executed
    when this string is byte-identical to what it was at plan time.
    """
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,lstart=,command="],
        capture_output=True,
        text=True,
    )
    text = (result.stdout or "").strip()
    if result.returncode != 0 or not text:
        return None
    return text


def ancestry(pid: int, table: Mapping[int, Row], guard: int = ANCESTRY_GUARD) -> list[int]:
    """pids from ``pid`` up, nearest first, stopping at the root of the chain."""
    chain, current, steps = [], pid, 0
    while steps < guard and current in table:
        steps += 1
        chain.append(current)
        current = table[current].ppid
        if current == chain[-1]:
            break
    return chain
