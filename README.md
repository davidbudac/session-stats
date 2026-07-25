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

The current session is identified via `$CLAUDE_CODE_SESSION_ID`, falling back to
the newest transcript for the working directory.

Two details worth knowing, because naive transcript parsers get them wrong:

- **Usage is repeated per content block.** One API response with thinking, text
  and two tool calls writes four transcript lines carrying the *same* usage
  object. Requests are deduped on `(requestId, message.id)`, otherwise totals
  come out 2-4x too high.
- **Subagent nesting is not in one field.** Depth comes from
  `parentAgentId` when present; otherwise the parent is found by looking up
  which transcript issued the spawning `toolUseId`. So a sub-subagent lands in
  the right place in the tree.

Tokens are reported, not dollars — no price table is baked in, so nothing goes
stale. Multiply by current rates if you want a cost estimate.

## Caveats

- Stats reflect what has been flushed to disk; an in-flight request may be missing.
- Resuming or forking a session creates a new session id; use `--session` to
  inspect the earlier one.
- Compaction does not truncate the transcript, so totals stay cumulative for the
  whole session.
