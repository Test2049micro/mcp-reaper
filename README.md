# mcp-reaper

**Clean up the stdio MCP servers your agent client leaked — without killing the
ones a live session is using.**

Agent clients spawn a `node`/`python`/`uvx` process per MCP server and often do
not reap them when the session ends, the model switches tools, or the window
closes. The accumulation is not cosmetic; it is reported upstream as a memory
problem:

- openai/codex#12491 — *MCP child processes not reaped after task completion —
  1300+ zombies, 37GB memory leak*
- openai/codex#18881 — child processes leak when the connection manager is replaced
- anthropics/claude-code#35673, #33947 — MCP subprocesses survive session end and pile up

(Links as observed on 2026-10-17; the client-side fix is the right one, and it
has not landed. This is the interim janitor.)

## The hard part is not finding duplicates

Two identical `context7-mcp` processes are not necessarily a leak — they can
belong to two live sessions. Killing "all but one per command line" breaks the
older session.

So the unit of reasoning here is the pair **(session host ancestor, server
label)**:

```
node …/context7-mcp/dist/index.js     ├─ session A ─┐  → keep newest, reap the rest
node …/context7-mcp/dist/index.js     ┘             │
node …/context7-mcp/dist/index.js        session B  ┘  → untouched, it is B's only
```

The session host is the nearest ancestor whose command looks like an agent
client (`claude`, `codex`, `gemini`, `cursor-agent`, `opencode`, `aider`,
`goose`, `openhands`, … — extend with `--session-host`). **If no session host can
be identified, nothing is reaped from that group.** Not being able to attribute a
process is not licence to guess.

## Safety model, five layers

| layer | default | flag |
|---|---|---|
| identification | a process is a candidate only if a rule names it or its command mentions `mcp` | `--marker`, `--rules` |
| attribution | groups without a session host are deferred, never reaped | `--reap-ambiguous` to override |
| volume | refuses to signal more than 200 processes in one run | `--max` |
| youth | a server younger than 120s is left alone, so a server that is mid-startup is never collected | `--min-age` |
| identity | re-reads the table and re-derives the plan before each signal; the PID must match `uid + lstart + command` byte-for-byte both before and after | — |

`SIGTERM` only. Nothing is reaped that the reaper itself descends from.

## Install

```bash
pipx install mcp-reaper            # or: pip install mcp-reaper

# Not on PyPI yet (name verified free as of 2026-10-17). Until the first release:
git clone https://github.com/Test2049micro/mcp-reaper && cd mcp-reaper && pipx install .
```

## Usage

```bash
mcp-reaper plan                    # read-only: what would be signalled
mcp-reaper explain                 # which processes were admitted, and their session root
mcp-reaper reap --yes              # actually signal
mcp-reaper plan --json | jq '.groups[] | {type, eligible}'
mcp-reaper reap --yes --keep 2 --min-age 600 --max 50
```

```
$ mcp-reaper plan
[context7-mcp] session_root=48112 total=43  keep=[48900]  eligible=[48201, 48205, …]
[github-mcp-server] session_root=48112 total=3 keep=[48902] eligible=[48113, 48120]

would SIGTERM 43: [48201, 48205, …]
```

### Exit codes

| code | meaning |
|---|---|
| 0 | nothing eligible |
| 1 | eligible processes found, **not signalled** (`plan` found work, or `reap` without `--yes`) |
| 2 | reaped, every target signalled |
| 3 | reaped, but some targets were skipped or failed |
| 4 | usage error, or the plan exceeded `--max` |

Code `1` is the reason a scheduled job no longer has to grep human output:

```bash
mcp-reaper plan >"$LOG" 2>&1
case $? in
  0) : ;;                       # nothing to do
  1) mcp-reaper reap --yes >>"$LOG" 2>&1 ;;
  *) echo "unexpected" >&2 ;;
esac
```

## Labelling servers you cannot name

Shape-based labelling reads `npx -y pkg`, `uvx --from pkg exe`,
`node …/pkg/dist/index.js`. Some clients violate that shape — a desktop app
spawning `/opt/cua_node/bin/node ./server.mjs` has no `mcp` in its command line
at all, and would be invisible. Pin those with a rules file:

```bash
mcp-reaper plan --rules examples/rules.json
mcp-reaper reap --yes --rules examples/rules.json
```

```json
{"rules": [{"name": "cua-server", "contains": ["cua_node/bin/node", "server.mjs"]}]}
```

A rule both **admits** a process (bypasses the marker test) and **names** it
(becomes the group key). Deliberately there is no built-in vendor list: an
unidentified process is left running, which is the correct default for a tool
that kills things.

## Scheduling

`examples/mcp-reaper.plist.example` is a macOS LaunchAgent template. Replace
`ProgramArguments[0]` with the output of `command -v mcp-reaper` first: pipx puts
it in `~/.local/bin`, Homebrew in `/opt/homebrew/bin`, and a wrong path makes the
job fail silently every 30 minutes. Dry-run by default — flip the `--reap --yes` argument when you trust it
on your machine). On Linux, any cron or systemd timer works; the exit-code
branch above is the pattern.

## Flags that change the safety envelope

| flag | what it relaxes, and the guard on it |
|---|---|
| `--marker` | the substring that admits a command as a server. Must be at least 2 characters: `--marker ""` would make every process on the machine a candidate, and the CLI refuses it with exit 4 |
| `--rules FILE` | admits commands the marker cannot see (a helper with no `mcp` in argv). A malformed file exits **4, not 1** -- `1` tells a wrapper "there is something to reap", so a config error that fell through as `1` would start killing processes on a file that never parsed |
| `--reap-ambiguous` | opts in to reaping groups with no identifiable session host. Off by default: not knowing whose session a server belongs to is not licence to guess |
| `--uid` | plans against one uid and re-validates the freshly read table against that same uid; dropping it made every target report `gone` |
| `--max N` | refuses to signal more than N processes (default 200) |
| `--keep N` | copies kept per (session, type) group; `--keep 0` is rejected because a reaper with no keep is a mass kill |

## Command lines are masked

`ps` shows full argv, and MCP servers are routinely launched with `--token` or
`--api-key` in it. Every `command` field in `plan --json`, `reap --json` and
`explain` passes through a mask that replaces credential-shaped arguments with
`--token=[redacted]` while keeping the identifying path visible. Keep the output
out of world-readable paths anyway: `examples/scheduled.sh` logs to
`~/.local/state/mcp-reaper/` for that reason.

## Limitations, stated plainly

- **It is a janitor, not a fix.** The leak is in the client. Reaping periodically
  is a workaround; the upstream issue is where it gets solved.
- **`SIGTERM` is a request.** A server that ignores it survives; nothing here
  escalates to `SIGKILL`, deliberately — an unresponsive MCP server may be
  mid-write on a socket its client still expects.
- **Session attribution is a name heuristic.** If your client is not in the
  default list and you pass no `--session-host`, groups go ambiguous and
  nothing is reaped. That failure is loud (`plan` says so) rather than silent.
- **Depends on `ps`** with `-Ao uid,pid,ppid,etime,command` (macOS and Linux).
  No `/proc` assumption, no dependency.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v     # zero dependencies
```

The suite is organised around what actually broke in the field, not around
coverage:

- `test_keeps_newest_and_reaps_older_duplicates` — an inverted sort once kept the
  *oldest* copy and killed the live one. Ordering is now pinned by test, and the
  fixtures make pid order and age order agree on purpose.
- `test_groups_do_not_mix_across_sessions` — the cross-session false positive.
- `test_unidentifiable_session_defers_instead_of_guessing` — the safety default.
- `test_generic_script_stem_climbs_out_of_build_directories` — every npm package
  ships `dist/index.js`; grouping by filename would merge unrelated servers.
- `ExecuteTest` — recycled PID, already-exited PID, a target that lost its
  session host between plan and signal, and signal failure surfaced rather than
  swallowed.

## 中文摘要

Agent 客户端每个 MCP server 起一个子进程，会话结束后经常不收，实测单个存活 26
小时的分区下堆了 43 个同名 context7；上游多个 issue 报了内存代价但没修。难点不是
找重复进程，而是**两个同名进程未必是泄漏**（可能分属两个在跑的会话），杀掉旧的会
打断正在用的那个。所以分组键是「最近的 agent 客户端祖先 + server 标签」，同一会话
内只保留最新一个；认不出会话主的分组一律不动。默认只碰命令行里带 `mcp` 或有规则
指认的进程，120 秒内启动的不碰，一次性超过 200 个直接拒绝执行，发信号前重读进程表、
重算计划、并用 uid+启动时刻+命令行逐字节复核 PID 没被复用。默认 dry-run，`--yes`
才发 SIGTERM；退出码 `1` 专给定时任务做分支，包装脚本不必再匹配人读输出。零依赖，
macOS/Linux。
