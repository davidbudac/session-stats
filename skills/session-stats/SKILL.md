---
name: session-stats
description: Report token, cache, and subagent statistics for a Claude Code session — input/output tokens, cache write/read and hit rate, per-model breakdown, and the tree of spawned subagents (including nested ones) with each one's model and token usage.
argument-hint: "[session-id] [--json] [--rollup] [--backfill [--force]]"
disable-model-invocation: true
---

# Session stats

Reads the session transcripts under `~/.claude/projects/` and prints a usage
report for the current (or a named) session.

Invoked only by the user via `/session-stats`. An argument, if given, is a
session id (pass it as `--session <id>`); `--json` switches to JSON output.
`--rollup`, `--backfill` and `--force` are passed straight through (see History
below).

## Usage

Run the script directly:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/skills/session-stats/session_stats.py"
```

If `$CLAUDE_PLUGIN_ROOT` is not set (skill installed by hand under
`~/.claude/skills/`), use:

```bash
python3 ~/.claude/skills/session-stats/session_stats.py
```

Options:

- `--session <id>` — report on another session instead of the current one.
  Without it, the script uses `$CLAUDE_CODE_SESSION_ID`, falling back to the
  newest transcript for the current working directory.
- `--project-dir <dir>` — resolve the session from a different working directory.
- `--json` — machine-readable output (same numbers, full per-agent detail).

Then relay the report to the user. Print the table as-is (it is already
formatted); add a sentence or two of interpretation only where it helps — e.g.
which subagent dominated output tokens, or an unusually low cache hit rate.

## History across sessions

A `SessionEnd` hook appends one JSON line per finished session to
`~/.claude/session-stats/sessions.jsonl` (override with
`$CLAUDE_SESSION_STATS_LOG`). That log outlives the transcripts, which
`cleanupPeriodDays` eventually deletes.

```bash
python3 <script> --rollup                # totals, per-model, by day, by project
python3 <script> --rollup --days 7       # window it
python3 <script> --rollup --json --full  # every record, machine-readable
python3 <script> --backfill              # import sessions still on disk
python3 <script> --backfill --force      # re-log every session still on disk
```

`--rollup` reports one row per session, deduped by session id (a session can be
logged more than once -- `/clear` fires `SessionEnd` while the id lives on, and
a backfill may re-log it). The record with the newest `schema` wins, then the
one with the most output tokens.

`--backfill` sweeps every transcript under `~/.claude/projects/` and logs the
ones missing from the log. Run it after installing the hook to seed history, and
any time a session ended without the hook firing (crash, `kill -9`). Sessions
are logged oldest first (by their first row's timestamp), so a session is
logged before any session resumed from it.

`--backfill --force` re-logs every main transcript still on disk, whether or
not it is already logged. Run it once after upgrading from a schema-1 log: the
new records supersede the old ones on read. The log stays append-only -- old
lines are never rewritten.

Each record lists the requests it owns in `request_keys` (12-hex-char sha1
prefixes of `requestId:messageId`). When a session is logged, or reported on
interactively, requests already owned by *another* session in the log are left
out: a resumed or `--fork-session` session starts with a verbatim copy of the
old session's history, and would otherwise count it twice. The report shows an
**Inherited requests** line (JSON: `inherited_requests`) when that happens.

## Visual dashboard

`visualize.py` (same directory) turns the log into a standalone HTML page —
hero total, KPI tiles, output per day, input composition per day, output by model
and by project, a daily table and the biggest sessions, with 7/30/90/all range
filters. No network, no dependencies, light and dark.

```bash
python3 <dir>/visualize.py --open          # write ~/.claude/session-stats/dashboard.html and open it
python3 <dir>/visualize.py --out /tmp/usage.html
```

It reads the same log (`--log-file`, else `$CLAUDE_SESSION_STATS_LOG`, else the
default) with the same per-session dedupe as `--rollup`.

Prefer this when the user asks to *see*, chart, or graph their usage; use
`--rollup` when they want numbers in the terminal.

## What the numbers mean

- **Input (uncached)** — fresh input tokens billed at full rate.
- **Cache write** — tokens written to the prompt cache this session
  (`cache_creation_input_tokens`); split into 1h / 5m TTL buckets when both occur.
- **Cache read** — tokens served from cache (`cache_read_input_tokens`). Large
  values are normal and cheap: every API request re-sends the whole conversation.
- **Total input** — uncached + cache write + cache read.
- **Cache hit rate** — cache read / total input.
- **API requests** — deduped per assistant message: usage is repeated on each
  content block (thinking / text / tool_use) in the transcript, so a naive
  line count over-reports by 2-4x. `output_tokens` grows across those rows, so
  the last row's usage is the one counted.
- **User prompts** — what the user sent, slash commands included. Skill bodies
  and other `isMeta` rows, compaction summaries, local-command / bash output,
  interrupt markers, task notifications and messages from other agents are not
  prompts.
- Per-model tables show the model that *actually served* each request, so a
  subagent that was retried on a different model shows up under both.
- Each request is counted once per session. A fork subagent's transcript
  starts with a copy of its parent's history; those requests (and their tool
  calls) stay with the parent, and the fork's time window starts where its own
  rows do.

Tokens are reported, not money — no price table is baked in, so nothing goes
stale. Multiply by current per-model rates if the user wants a cost estimate.

## Where the data comes from

- Main thread: `~/.claude/projects/<project-slug>/<session-id>.jsonl`
- Subagents: `<project-slug>/<session-id>/subagents/agent-<id>.jsonl` plus a
  sibling `agent-<id>.meta.json` holding `agentType`, `name`, `description`,
  `spawnDepth`, `parentAgentId`, and the requested `model`.
- Nesting is reconstructed from `parentAgentId`, or by finding which transcript
  issued the spawning `toolUseId` — so sub-subagents nest correctly. Forks copy
  the spawning call too, so the main thread wins, then the shallowest
  `spawnDepth`; an agent is never its own parent, and one whose parent can't be
  resolved is shown at the top level.
- Legacy layout (`<project-slug>/agent-<id>.jsonl` with a matching `sessionId`)
  is also picked up.
- Workflow runs (`<session-id>/workflows/wf_*.json`) are listed with status,
  agent count, and default model. Their agents are counted in the subagent tree.

## Caveats

- Stats reflect what has been flushed to the transcript, so the last in-flight
  request may be missing.
- `SessionEnd` does not fire if the process is killed outright, so the log can
  miss a session; `--backfill` recovers it while the transcript still exists.
- Resuming or forking a session starts a new session id; stats cover one id.
  Pass `--session` to look at the earlier one. The copied history is only
  recognised once the earlier session is in the log; until then the new id
  counts it too. Prompts and the time window skip the copy by position (it is
  always a prefix of the transcript), so a copied prompt that got no reply
  before the resume can still be counted.
- If a resume is logged before its source (source still running), the shared
  requests are attributed to the resume. Totals are still counted once.
- Records written before schema 2 whose transcripts were deleted stay at
  schema 1: they undercount output (first content block's usage) and may
  double-count forks and resumes. `--backfill --force` can only fix sessions
  whose transcripts still exist.
- Compaction does not erase history from the transcript, so totals are
  cumulative for the session even after a compact.
