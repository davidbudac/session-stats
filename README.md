# session-stats

A Claude Code skill that reports what your current session actually cost in
tokens — input, output, cache write/read and hit rate, a per-model breakdown,
and the full tree of spawned subagents (including nested ones) with each one's
model and token usage.

Run `/session-stats` when you want it. The skill is user-invoked only — Claude
never triggers it on its own.

```
==========================================================================
Claude Code session stats
==========================================================================
Session                1c3bbb4c-0903-4ef8-9d1d-c7c991b05e04
Project                /Users/you/projects/example
Window                 2026-07-04 22:24 -> 2026-07-04 23:03  (39m00s)
User prompts           15

-- MAIN THREAD -----------------------------------------------------------
API requests                       71
Input (uncached)               22,859
Cache write                   389,816
Cache read                  8,387,757
Output                         56,550
Total input                 8,800,432
Cache hit rate                  95.3%

  model                       reqs       input    cache wr    cache rd      output
  claude-fable-5                71      22,859     389,816   8,387,757      56,550

-- SUBAGENTS (6) --------------------------------------------------------
  * general-purpose (essential-impl)  [claude-fable-5, claude-sonnet-5]
     Implement Essential rows preset
     id=a97302632  reqs=33  in=27,565  cw=250,561  cr=3,513,737  out=5,491  6m50s
     tools: Edit=18, Read=12, Bash=10, Agent=1, Write=1
      |- general-purpose  [claude-sonnet-5]
         id=aa5686597  reqs=7  in=19,451  cw=46,019  cr=356,319  out=190  1m24s
          |- general-purpose  [claude-sonnet-5]
             id=a3bad4b44  reqs=40  in=23,304  cw=169,346  cr=6,393,652  out=2,155
```

Plus a workflow-run summary when the session ran `Workflow`, and a grand total
across main thread and every subagent.

## Long-term history

Transcripts get cleaned up (`cleanupPeriodDays`, 30 by default), so the plugin
also ships a `SessionEnd` hook that appends one JSON line per finished session to
`~/.claude/session-stats/sessions.jsonl`. Nothing leaves your machine.

```bash
python3 ~/.claude/skills/session-stats/session_stats.py --backfill   # seed from transcripts on disk
python3 ~/.claude/skills/session-stats/session_stats.py --rollup     # aggregate everything
```

Upgrading from a log written by an older version (records without
`"schema": 2`)? Run `--backfill --force` once: it re-logs every session whose
transcript is still on disk, oldest first, and the new records supersede the
old ones on read. Nothing already in the log is rewritten.

```
-- TOTALS ------------------------------------------------------------------
API requests                   24,322
Input (uncached)            7,438,456
Cache write               122,122,510
Cache read              3,353,747,296
Output                     14,923,474
Cache hit rate                  96.3%
Subagents spawned                 250

  model                       reqs       input    cache wr    cache rd      output
  claude-opus-4-8            10,025   2,873,633  51,474,722 1,346,359,109   7,531,178
  claude-fable-5              8,466   1,847,046  44,025,830 1,347,947,408   5,943,224
  claude-sonnet-5             5,057   2,706,004  22,456,270 580,211,821   1,093,208

-- BY DAY / BY PROJECT / BIGGEST SESSIONS ...
```

`--days N` windows the report, `--json` emits it machine-readably (`--full`
includes every session record for your own analysis).

### Dashboard

`visualize.py` renders the same log as a standalone HTML page — one file, no
network, no dependencies, light and dark:

```bash
python3 ~/.claude/skills/session-stats/visualize.py --open
```

Hero total and KPI tiles, output tokens per day, input composition per day
(cache read / cache write / uncached, stacked), output by model and by project,
a daily table, and your biggest sessions — with 7/30/90/all range filters that
scope every chart at once. Hover any column or bar for exact numbers. It reads
the log from `--log-file`, else `$CLAUDE_SESSION_STATS_LOG`, else the default.

If you installed by hand rather than as a plugin, add the hook yourself — see
`hooks/hooks.json` for the exact command, using
`~/.claude/skills/session-stats/session_stats.py` as the path.

## Install

### As a plugin (recommended)

```
/plugin marketplace add davidbudac/session-stats
/plugin install session-stats@session-stats-marketplace
```

A local clone works too: `/plugin marketplace add /path/to/session-stats`.

### As a user-scope skill

```bash
git clone <this-repo> /tmp/session-stats
cp -r /tmp/session-stats/skills/session-stats ~/.claude/skills/
```

Both routes are equivalent — the plugin just keeps updates a `git pull` away.

## Use

```
/session-stats                 # current session
/session-stats --json          # machine-readable
/session-stats <session-id>    # some other session
```

Or run the script yourself:

```bash
python3 ~/.claude/skills/session-stats/session_stats.py            # current session
python3 ~/.claude/skills/session-stats/session_stats.py --json     # machine-readable
python3 ~/.claude/skills/session-stats/session_stats.py --session <session-id>
```

Requirements: Python 3.8+, standard library only.

## How it works

Everything comes from the JSONL transcripts Claude Code already writes under
`~/.claude/projects/`:

| what | where |
| --- | --- |
| main thread | `<project-slug>/<session-id>.jsonl` |
| subagents | `<project-slug>/<session-id>/subagents/agent-<id>.jsonl` (+ `.meta.json`) |
| workflow runs | `<project-slug>/<session-id>/workflows/wf_*.json` |

The history log (plus `dashboard.html` next to it, if you run `visualize.py`) is
all this tool writes: `~/.claude/session-stats/sessions.jsonl`
(`$CLAUDE_SESSION_STATS_LOG` overrides). Records are append-only and deduped by
session id on read: the newest `schema` wins, then the most output tokens.

The current session is identified via `$CLAUDE_CODE_SESSION_ID`, falling back to
the newest transcript for the working directory.

Details worth knowing, because naive transcript parsers get them wrong:

- **Usage is repeated per content block.** One API response with thinking, text
  and two tool calls writes four transcript lines sharing `(requestId,
  message.id)` and the usage object. Requests are deduped on that key,
  otherwise totals come out 2-4x too high. But `output_tokens` grows from line
  to line, so the *last* line's usage is the real one -- the first undercounts
  output by about a third.
- **Forks copy their parent's history.** A fork subagent's transcript starts
  with a copy of the parent's rows (same request ids, rewritten session and
  agent ids). Each request is counted once per session, for the transcript
  closest to the root that has it; tool calls and prompts in the copy are
  left out with it.
- **So do resumed sessions.** Resuming or forking a session into a new session
  id copies the old history into the new transcript. Each log record stores
  the requests it owns as `request_keys` (short sha1 hashes of
  `requestId:messageId`), and requests owned by another session in the log are
  excluded when a session is logged or reported on.
- **Subagent nesting is not in one field.** Depth comes from
  `parentAgentId` when present; otherwise the parent is found by looking up
  which transcript issued the spawning `toolUseId`. Forks hold a copy of that
  call too, so the main thread wins, then the shallowest `spawnDepth`. So a
  sub-subagent lands in the right place in the tree.

Tokens are reported, not dollars — no price table is baked in, so nothing goes
stale. Multiply by current rates if you want a cost estimate.

## Caveats

- Stats reflect what has been flushed to disk; an in-flight request may be missing.
- Resuming or forking a session creates a new session id; use `--session` to
  inspect the earlier one. Its copied history is only recognised once the
  earlier session is in the log, and copied prompts are skipped by position, so
  a trailing prompt that never got a reply can still be counted twice.
- Log records from before schema 2 whose transcripts are already deleted stay
  at schema 1: they undercount output and may double-count forks and resumes.
- Compaction does not truncate the transcript, so totals stay cumulative for the
  whole session.
- `SessionEnd` doesn't fire if the process is killed outright, so a session can
  be missing from the log — `--backfill` recovers it while the transcript lives.

## Tests

Standard library only; from the repo root:

```bash
python3 -m unittest discover tests
```

The tests build small synthetic transcripts and logs in temp dirs; they never
touch `~/.claude`.
