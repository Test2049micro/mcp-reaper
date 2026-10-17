"""Reap duplicated stdio MCP server processes left behind by agent clients."""

from .proctable import Row, etime_to_sec, identity, parse_ps, read_table
from .reap import (
    DEFAULT_KEEP,
    DEFAULT_MIN_AGE_SEC,
    DEFAULT_SESSION_HOSTS,
    Group,
    Plan,
    ReapResult,
    Target,
    build_plan,
    execute,
    session_root,
)
from .signature import Rule, classify, derive, load_rules

__all__ = [
    "Row",
    "Target",
    "Group",
    "Plan",
    "ReapResult",
    "Rule",
    "build_plan",
    "execute",
    "session_root",
    "classify",
    "derive",
    "load_rules",
    "parse_ps",
    "read_table",
    "identity",
    "etime_to_sec",
    "DEFAULT_KEEP",
    "DEFAULT_MIN_AGE_SEC",
    "DEFAULT_SESSION_HOSTS",
    "__version__",
]

__version__ = "0.1.0"
