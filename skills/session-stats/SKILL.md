---
name: session-stats
description: Report token, cache, and subagent statistics for a Claude Code session — input/output tokens, cache write/read and hit rate, per-model breakdown, and the tree of spawned subagents (including nested ones) with each one's model and token usage.
argument-hint: "[session-id] [--json] [--rollup] [--backfill]"
disable-model-invocation: true
---

# Session stats

Reads the session transcripts under `~/.claude/projects/` and prints a usage
report for the current (or a named) session.

Invoked only by the user via `/session-stats`. An argument, if given, is a
session id (pass it as `--session <id>`); `--json` switches to JSON output.
`--rollup` and `--backfill` are passed straight through (see History below).

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
```

`--rollup` reports one row per session, deduped by session id (a session can be
logged more than once -- `/clear` fires `SessionEnd` while the id lives on, and
a backfill may re-log it -- so the record with the most output tokens wins).

`--backfill` sweeps every transcript under `~/.claude/projects/` and logs the
ones missing from the log. Run it after installing the hook to seed history, and
any time a session ended without the hook firing (crash, `kill -9`).

## Visual dashboard

`visualize.py` (same directory) turns the log into a standalone HTML page —
hero total, KPI tiles, output per day, input composition per day, output by model
and by project, a daily table and the biggest sessions, with 7/30/90/all range
filters. No network, no dependencies, light and dark.

```bash
python3 <dir>/visualize.py --open          # write ~/.claude/session-stats/dashboard.html and open it
python3 <dir>/visualize.py --out /tmp/usage.html
```

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
  line count over-reports by 2-4x.
- Per-model tables show the model that *actually served* each request, so a
  subagent that was retried on a different model shows up under both.

Tokens are reported, not money — no price table is baked in, so nothing goes
stale. Multiply by current per-model rates if the user wants a cost estimate.

## Where the data comes from

- Main thread: `~/.claude/projects/<project-slug>/<session-id>.jsonl`
- Subagents: `<project-slug>/<session-id>/subagents/agent-<id>.jsonl` plus a
  sibling `agent-<id>.meta.json` holding `agentType`, `name`, `description`,
  `spawnDepth`, `parentAgentId`, and the requested `model`.
- Nesting is reconstructed from `parentAgentId`, or by finding which transcript
  issued the spawning `toolUseId` — so sub-subagents nest correctly.
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
  Pass `--session` to look at the earlier one.
- Compaction does not erase history from the transcript, so totals are
  cumulative for the session even after a compact.
