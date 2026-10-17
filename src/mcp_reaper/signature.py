"""Derive a stable "which server is this" label from a command line.

The shipped code contains no vendor list. Types come from the shape of the
command (``npx -y pkg``, ``uvx --from pkg exe``, ``node .../pkg/dist/index.js``),
so a new MCP server is grouped correctly without anyone editing source. Optional
user rules pin an exact label when a shape is ambiguous.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

INTERPRETERS = ("node", "nodejs", "bun", "deno", "python", "python3", "uv", "npx", "tsx")
GENERIC_STEMS = ("index", "main", "server", "app", "cli", "run", "start", "__main__", "appserver")
# Layout noise: a package directory is the label, not its build output.
SKIP_DIR_NAMES = (
    "dist", "bin", "out", "build", "lib", "src", "node_modules", ".bin",
    "opt", "usr", "local", "var", "home", "users", "private", "tmp", "applications",
    "contents", "macos", "resources", "venv", "site-packages", ".", "..", "/", "",
)
# Names too common to identify a package: "/A/mcp/dist/index.js" and
# "/B/mcp/index.js" are different servers, and grouping them together makes the
# reaper kill one of them as a "duplicate".
GENERIC_LABELS = ("mcp", "server", "servers", "index", "main", "app", "bin", "dist",
                  "src", "lib", "out", "build", "node_modules", ".bin", "run")
_RUNNERS = ("npx", "uvx", "pnpx", "dnx")
# ``npm exec pkg`` / ``pnpm dlx pkg`` are the same shape as ``npx pkg``, but the
# runner is not the token that carries the package name.
_PACKAGE_SUBCOMMANDS = ("exec", "dlx", "x", "init")
_PACKAGE_RUNNERS = ("npm", "pnpm", "yarn", "bun")
_SOURCE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".py", ".sh", ".bash")
_VERSION_SUFFIX = re.compile(r"[-@]v?\d[\d.]*$")


class Rule:
    def __init__(self, name: str, contains: Sequence[str]) -> None:
        self.name = name
        self.contains = tuple(c.lower() for c in contains if c)

    def matches(self, command: str) -> bool:
        low = command.lower()
        return any(fragment in low for fragment in self.contains)


def load_rules(path: str | Path | None) -> list[Rule]:
    """Read ``{"rules": [{"name": ..., "contains": [...]}]}`` or a bare list.

    Rules from a file take precedence over generic derivation, which is the
    point of having them: they let you say what a weird invocation means.
    """
    if path is None:
        return []
    name = str(path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name}: not valid JSON ({exc})") from exc
    entries = data.get("rules", data) if isinstance(data, Mapping) else data
    if not isinstance(entries, list):
        raise ValueError(f"{name}: expected a list of rules, got {type(entries).__name__}")
    rules = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: rule #{index + 1} is {type(entry).__name__}, expected an object")
        if not entry.get("name"):
            raise ValueError(f"{name}: rule #{index + 1} has no name")
        contains = entry.get("contains") or entry.get("patterns") or []
        if isinstance(contains, str):
            contains = [contains]
        if not isinstance(contains, list) or not contains or not all(isinstance(c, str) and c for c in contains):
            raise ValueError(f"{name}: rule {entry.get('name')!r} needs a non-empty list of strings")
        rules.append(Rule(str(entry["name"]), contains))
    return rules


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _first_positional(tokens: Sequence[str], start: int) -> str | None:
    for token in tokens[start:]:
        if not token.startswith("-"):
            return token
    return None


def _label_from_path(path: str) -> str:
    """Name a server after its package directory when the file itself is generic.

    ``context7-mcp/dist/index.js`` and ``some-mcp/dist/index.js`` are different
    servers even though the executable filename is identical, so the label has to
    climb out of build-output directories.
    """
    candidate = Path(path)
    stem = candidate.name
    if "." in stem and not stem.startswith("."):
        suffix = "." + stem.rsplit(".", 1)[1].lower()
        if suffix in _SOURCE_SUFFIXES or suffix in (".node", ".bin", ".exe"):
            if Path(stem).stem.lower() in GENERIC_STEMS:
                for part in reversed(candidate.parent.parts):
                    lowered = part.lower().rstrip("/")
                    if lowered in SKIP_DIR_NAMES or lowered in GENERIC_LABELS:
                        continue
                    return part
                return Path(stem).stem
            return Path(stem).stem
    return stem


def _package_name(token: str) -> str:
    """``@scope/name`` -> ``name``; version suffixes dropped."""
    name = token.split()[0]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return _VERSION_SUFFIX.sub("", name)


def _sanitize(label: str) -> str:
    label = (label or "").strip().strip("/").lower()
    label = re.sub(r"[^a-z0-9._@-]+", "-", label)
    return label.strip("-") or "unknown"


def derive(command: str) -> str:
    """Best-effort server label for a command line, without any lookup table."""
    tokens = _tokens(command)
    if not tokens:
        return "unknown"

    for index, token in enumerate(tokens):
        head = Path(token).name.lower()

        if head in _RUNNERS:
            package = _first_positional(tokens, index + 1)
            if package:
                return _sanitize(_package_name(package))

        if head in _PACKAGE_RUNNERS and tokens[index + 1: index + 2] and tokens[index + 1] in _PACKAGE_SUBCOMMANDS:
            package = _first_positional(tokens, index + 2)
            if package:
                return _sanitize(_package_name(package))

        if head == "uv" and tokens[index + 1: index + 2] and tokens[index + 1] in ("tool", "run"):
            package = _first_positional(tokens, index + 2)
            if package:
                return _sanitize(_package_name(package))

        if head == "pipx" and tokens[index + 1: index + 2] == ["run"]:
            package = _first_positional(tokens, index + 2)
            if package:
                return _sanitize(_package_name(package))

        if head in INTERPRETERS:
            following = _first_positional(tokens, index + 1)
            if following and Path(following).name.lower() not in ("run", "tool", "exec", "-e"):
                return _sanitize(_label_from_path(following))

    return _sanitize(_package_name(tokens[0]))


MCP_MARKER = "mcp"


def classify(
    command: str,
    rules: Sequence[Rule] = (),
    *,
    marker: str = MCP_MARKER,
) -> str | None:
    """Label this command, or ``None`` when it cannot be identified as a server.

    A reaper that guesses kills working sessions. So a process is only a
    candidate when a user rule names it, or its command line mentions
    ``marker`` (``mcp`` by default) and a label can be derived from its shape.
    Unidentifiable processes are left alone -- that is the whole safety model.
    """
    for rule in rules:
        if rule.matches(command):
            return rule.name
    if marker.lower() not in (command or "").lower():
        return None
    label = derive(command)
    return label if label != "unknown" else None
