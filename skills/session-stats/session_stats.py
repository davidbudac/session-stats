#!/usr/bin/env python3
"""Token / cache / subagent statistics for Claude Code sessions.

Reads the session transcript(s) under ~/.claude/projects/ and reports input,
output and cache token usage for the main thread plus every subagent
(recursively), including the model each one actually ran on.

Modes:
    session_stats.py                     report on the current session
    session_stats.py --session <id>       report on another session
    session_stats.py --json              machine-readable report
    session_stats.py --log               append one record to the history log
                                         (SessionEnd hook entry point)
    session_stats.py --rollup            aggregate the history log
    session_stats.py --backfill          log every past session not yet logged
    session_stats.py --backfill --force  re-log every session still on disk

With no --session it uses $CLAUDE_CODE_SESSION_ID, falling back to the most
recently modified transcript for the current working directory.

Each API request is counted once per session (forks copy their parent's
history) and once across the log (a resumed session copies the old one's
history); log records carry the request keys they own for that.

History log (JSONL, one record per session):
    ~/.claude/session-stats/sessions.jsonl   ($CLAUDE_SESSION_STATS_LOG overrides)
"""

import argparse
import glob
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

PROJECTS = os.path.expanduser("~/.claude/projects")
DEFAULT_LOG = os.path.expanduser("~/.claude/session-stats/sessions.jsonl")
SCHEMA_VERSION = 2

USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")
SUM_FIELDS = USAGE_FIELDS + ("requests", "ephemeral_1h", "ephemeral_5m",
                             "web_search", "web_fetch", "api_errors")

# user rows that are output, notices or injected context, not something typed
NOT_PROMPTS = ("<local-command-stdout>", "<local-command-stderr>",
               "<local-command-caveat>", "<bash-stdout>", "<bash-stderr>",
               "<task-notification>", "[Request interrupted",
               "Another Claude session sent a message:")
NOT_PROMPT_ORIGINS = ("task-notification", "peer")


def log_path():
    return os.environ.get("CLAUDE_SESSION_STATS_LOG") or DEFAULT_LOG


# ---------------------------------------------------------------- parsing


def iter_rows(path):
    """Stream transcript rows; tolerate a half-written trailing line."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def blank():
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "ephemeral_1h": 0,
        "ephemeral_5m": 0,
        "web_search": 0,
        "web_fetch": 0,
        "by_model": defaultdict(lambda: defaultdict(int)),
        "tools": Counter(),
        "user_turns": 0,
        "first_ts": None,
        "last_ts": None,
        "api_errors": 0,
    }


def request_key(row, msg):
    """Short id of one API request -- the form the log's request_keys hold."""
    raw = "%s:%s" % (row.get("requestId"), msg.get("id"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def is_prompt(row, content):
    """True for a user row the human sent (slash commands included)."""
    origin = row.get("origin")
    if (row.get("isMeta") or row.get("isCompactSummary") or
            (isinstance(origin, dict) and origin.get("kind") in NOT_PROMPT_ORIGINS)):
        return False
    text = content
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return False
        text = next((b.get("text") for b in blocks if b.get("type") == "text"), "")
    return not (text or "").lstrip().startswith(NOT_PROMPTS)


def scan(path, skip_sidechain=False):
    """Index one transcript in a single streaming pass; nothing is summed yet.

    Returns {"requests": {key: {model, usage, tools, row}}, "prompts": [row],
    "errors": [row], "stamps": [(row, ts)], "tool_ids": set, "info": {}}, rows
    numbered in file order. One API message is written as one row per content
    block, all sharing (requestId, message.id) and the usage object -- except
    output_tokens, which grows across them, so the last row's usage is final.
    """
    t = {"requests": {}, "prompts": [], "errors": [], "stamps": [],
         "tool_ids": set(), "info": {}}
    info, reqs = t["info"], t["requests"]

    for i, row in enumerate(iter_rows(path)):
        if skip_sidechain and row.get("isSidechain"):
            continue
        if "cwd" in row and "cwd" not in info:
            info["cwd"] = row["cwd"]
        if "version" in row:
            info["version"] = row["version"]
        if "gitBranch" in row and row.get("gitBranch"):
            info["gitBranch"] = row["gitBranch"]
        if row.get("timestamp"):
            t["stamps"].append((i, row["timestamp"]))

        msg = row.get("message") or {}
        content = msg.get("content")
        if row.get("type") == "user" and isinstance(content, (str, list)):
            if is_prompt(row, content):
                t["prompts"].append(i)
            continue
        if row.get("type") != "assistant":
            continue

        if row.get("isApiErrorMessage"):
            t["errors"].append(i)
        model = msg.get("model") or "unknown"
        if model == "<synthetic>":
            continue

        key = request_key(row, msg)
        req = reqs.get(key)
        if req is None:
            req = reqs[key] = {"model": model, "usage": {}, "tools": {}}
        req["row"] = i
        if msg.get("usage"):
            req["usage"] = msg["usage"]
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    req["tools"][b.get("id") or len(req["tools"])] = b.get("name", "?")
                    t["tool_ids"].add(b.get("id"))
    return t


def tally(t):
    """Stats for a scanned transcript over the requests it owns (t["owned"]).

    Copied history (fork, resume) is always a prefix of the file and keeps its
    original timestamps, so prompts, API errors and the time window only count
    rows after the last request owned elsewhere.
    """
    s = blank()
    reqs, owned = t["requests"], t["owned"]
    cut = max([r["row"] for k, r in reqs.items() if k not in owned] or [-1])
    for k in owned:
        r = reqs[k]
        usage, m = r["usage"], s["by_model"][r["model"]]
        s["requests"] += 1
        m["requests"] += 1
        for f in USAGE_FIELDS:
            v = usage.get(f) or 0
            s[f] += v
            m[f] += v
        cc = usage.get("cache_creation") or {}
        s["ephemeral_1h"] += cc.get("ephemeral_1h_input_tokens") or 0
        s["ephemeral_5m"] += cc.get("ephemeral_5m_input_tokens") or 0
        stu = usage.get("server_tool_use") or {}
        s["web_search"] += stu.get("web_search_requests") or 0
        s["web_fetch"] += stu.get("web_fetch_requests") or 0
        s["tools"].update(r["tools"].values())
    s["user_turns"] = sum(1 for i in t["prompts"] if i > cut)
    s["api_errors"] = sum(1 for i in t["errors"] if i > cut)
    stamps = [ts for i, ts in t["stamps"] if i > cut]
    if stamps:
        s["first_ts"], s["last_ts"] = min(stamps), max(stamps)
    return s


def add_into(total, s):
    for f in SUM_FIELDS:
        total[f] += s[f]
    total["tools"].update(s["tools"])
    for model, m in s["by_model"].items():
        for k, v in m.items():
            total["by_model"][model][k] += v
    for f in ("first_ts", "last_ts"):
        if s[f] and (total[f] is None or
                     (s[f] < total[f] if f == "first_ts" else s[f] > total[f])):
            total[f] = s[f]
    return total


# ------------------------------------------------------------- discovery


def find_session(session_id, cwd):
    """Return (transcript_path, session_id)."""
    if session_id:
        hits = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
        if not hits:
            sys.exit("No transcript found for session %s under %s"
                     % (session_id, PROJECTS))
        return hits[0], session_id

    env_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env_id:
        hits = glob.glob(os.path.join(PROJECTS, "*", env_id + ".jsonl"))
        if hits:
            return hits[0], env_id

    slug = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
    cands = glob.glob(os.path.join(PROJECTS, slug, "*.jsonl"))
    if not cands:
        sys.exit("Cannot locate a transcript for %s (set --session)." % cwd)
    path = max(cands, key=os.path.getmtime)
    return path, os.path.basename(path)[:-6]


def collect_agents(project_dir, session_id):
    """Every subagent transcript belonging to this session, at any depth."""
    agents = []

    # current layout: <project>/<session-id>/subagents/agent-<id>.jsonl (+ .meta.json)
    sub = os.path.join(project_dir, session_id, "subagents")
    for path in sorted(glob.glob(os.path.join(sub, "agent-*.jsonl"))):
        agent_id = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        meta = {}
        meta_path = path[:-len(".jsonl")] + ".meta.json"
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as fh:
                    meta = json.load(fh)
            except (OSError, json.JSONDecodeError):
                meta = {}
        agents.append({"id": agent_id, "path": path, "meta": meta})

    # legacy layout: <project>/agent-<id>.jsonl carrying sessionId inside
    for path in sorted(glob.glob(os.path.join(project_dir, "agent-*.jsonl"))):
        first = next(iter_rows(path), None)
        if not first or first.get("sessionId") != session_id:
            continue
        agent_id = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        agents.append({"id": agent_id, "path": path, "meta": {}})

    for a in agents:
        a["scan"] = scan(a["path"])
    return agents


def spawn_rank(a):
    depth = a["meta"].get("spawnDepth")
    return (depth if isinstance(depth, int) else 1 << 30,
            bool(a["meta"].get("isFork")), a["id"])


def link_parents(agents, main_tool_ids):
    """Attach parent_id ("" = main thread) from meta.parentAgentId, else from
    the transcript that issued the spawning toolUseId.

    A fork's transcript starts with a copy of its parent's history, spawning
    tool_use included, so several transcripts can hold that id: prefer the
    main thread, then the shallowest spawnDepth, never the agent itself.
    Anything unresolved goes to the top level, and cycles are cut, so every
    agent is reachable from the main thread.
    """
    known = {a["id"] for a in agents}
    holders = defaultdict(list)
    for a in agents:
        for tid in a["scan"]["tool_ids"]:
            holders[tid].append(a)

    for a in agents:
        parent = a["meta"].get("parentAgentId")
        if parent not in known or parent == a["id"]:
            tid = a["meta"].get("toolUseId")
            others = [h for h in holders.get(tid, ()) if h is not a]
            if not tid or tid in main_tool_ids or not others:
                parent = ""
            else:
                parent = min(others, key=spawn_rank)["id"]
        a["parent_id"] = parent

    by_id = {a["id"]: a for a in agents}
    for a in agents:
        p, hops = a["parent_id"], 0
        while p and p != a["id"] and hops < len(agents):
            p, hops = by_id[p]["parent_id"], hops + 1
        if p == a["id"]:
            a["parent_id"] = ""
    return agents


def tree_order(agents):
    """Agents breadth first from the main thread: parents before children."""
    kids = defaultdict(list)
    for a in agents:
        kids[a["parent_id"]].append(a)
    out, level = [], kids.pop("", [])
    while level:
        out += level
        level = [c for a in level for c in kids.pop(a["id"], [])]
    return out + [a for rest in kids.values() for a in rest]  # unreachable; never expected


def read_workflows(project_dir, session_id):
    out = []
    for path in sorted(glob.glob(os.path.join(
            project_dir, session_id, "workflows", "wf_*.json"))):
        try:
            with open(path) as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "runId": d.get("runId"),
            "name": d.get("workflowName"),
            "status": d.get("status"),
            "agentCount": d.get("agentCount"),
            "durationMs": d.get("durationMs"),
            "defaultModel": d.get("defaultModel"),
        })
    return out


def build_report(path, session_id, fallback_cwd, owners=None):
    """owners maps request key -> the session id that logged it (see
    log_owners). Requests logged by another session were copied in by a
    resume or fork and are left out as inherited."""
    project_dir = os.path.dirname(path)
    main = scan(path, skip_sidechain=True)
    agents = link_parents(collect_agents(project_dir, session_id), main["tool_ids"])

    # each request belongs to the transcript closest to the root that has it
    owners = owners or {}
    ordered = [main] + [a["scan"] for a in tree_order(agents)]
    inherited = {k for t in ordered for k in t["requests"]
                 if owners.get(k, session_id) != session_id}
    taken = set(inherited)
    for t in ordered:
        t["owned"] = set(t["requests"]) - taken
        taken |= t["owned"]

    main_stats = tally(main)
    agent_total = blank()
    for a in agents:
        a["stats"] = tally(a["scan"])
        add_into(agent_total, a["stats"])
    grand = add_into(add_into(blank(), main_stats), agent_total)

    info = main["info"]
    return {
        "session_id": session_id,
        "transcript": path,
        "cwd": info.get("cwd") or fallback_cwd,
        "version": info.get("version"),
        "git_branch": info.get("gitBranch"),
        "main": main_stats,
        "agents": agents,
        "agent_total": agent_total,
        "grand_total": grand,
        "workflows": read_workflows(project_dir, session_id),
        "inherited_requests": len(inherited),
        "request_keys": sorted(taken - inherited),
    }


# ------------------------------------------------------------- rendering


def n(v):
    return "{:,}".format(int(v or 0))


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def secs_between(first, last):
    a, b = parse_ts(first), parse_ts(last)
    return int((b - a).total_seconds()) if a and b else None


def fmt_secs(secs):
    if secs is None:
        return "-"
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return ("%dh%02dm" % (h, m)) if h else ("%dm%02ds" % (m, s)) if m else ("%ds" % s)


def dur(first, last):
    return fmt_secs(secs_between(first, last))


def local(ts):
    d = parse_ts(ts)
    return d.astimezone().strftime("%Y-%m-%d %H:%M") if d else (ts or "-")


def token_block(s, indent=""):
    total_in = s["input_tokens"] + s["cache_creation_input_tokens"] + s["cache_read_input_tokens"]
    hit = (100.0 * s["cache_read_input_tokens"] / total_in) if total_in else 0.0
    lines = [
        "%s%-22s %14s" % (indent, "API requests", n(s["requests"])),
        "%s%-22s %14s" % (indent, "Input (uncached)", n(s["input_tokens"])),
        "%s%-22s %14s" % (indent, "Cache write", n(s["cache_creation_input_tokens"])),
        "%s%-22s %14s" % (indent, "Cache read", n(s["cache_read_input_tokens"])),
        "%s%-22s %14s" % (indent, "Output", n(s["output_tokens"])),
        "%s%-22s %14s" % (indent, "Total input", n(total_in)),
        "%s%-22s %13.1f%%" % (indent, "Cache hit rate", hit),
    ]
    if s["ephemeral_1h"] or s["ephemeral_5m"]:
        lines.append("%s%-22s %14s" % (
            indent, "Cache write 1h / 5m",
            "%s / %s" % (n(s["ephemeral_1h"]), n(s["ephemeral_5m"]))))
    if s["web_search"] or s["web_fetch"]:
        lines.append("%s%-22s %14s" % (
            indent, "Server tools s/f",
            "%s / %s" % (n(s["web_search"]), n(s["web_fetch"]))))
    return lines


def model_table(by_model, indent=""):
    rows = sorted(by_model.items(), key=lambda kv: -kv[1]["output_tokens"])
    out = ["%s%-26s %5s %11s %11s %11s %11s" % (
        indent, "model", "reqs", "input", "cache wr", "cache rd", "output")]
    for model, m in rows:
        out.append("%s%-26s %5s %11s %11s %11s %11s" % (
            indent, model[:26], n(m.get("requests")), n(m.get("input_tokens")),
            n(m.get("cache_creation_input_tokens")),
            n(m.get("cache_read_input_tokens")), n(m.get("output_tokens"))))
    return out


def agent_label(a):
    meta = a["meta"]
    bits = [meta.get("agentType") or "agent"]
    if meta.get("name"):
        bits.append("(%s)" % meta["name"])
    return " ".join(bits), meta.get("description")


def render(report):
    L = []
    W = 74
    L.append("=" * W)
    L.append("Claude Code session stats")
    L.append("=" * W)
    L.append("%-22s %s" % ("Session", report["session_id"]))
    L.append("%-22s %s" % ("Project", report["cwd"]))
    L.append("%-22s %s -> %s  (%s)" % (
        "Window", local(report["main"]["first_ts"]),
        local(report["main"]["last_ts"]),
        dur(report["main"]["first_ts"], report["main"]["last_ts"])))
    L.append("%-22s %s" % ("User prompts", n(report["main"]["user_turns"])))
    if report["inherited_requests"]:
        L.append("%-22s %s  (already logged under another session)" % (
            "Inherited requests", n(report["inherited_requests"])))
    L.append("")
    L.append("-- MAIN THREAD " + "-" * (W - 15))
    L += token_block(report["main"])
    L.append("")
    L += model_table(report["main"]["by_model"], "  ")

    tools = report["main"]["tools"]
    if tools:
        L.append("")
        L.append("  tool calls: " + ", ".join(
            "%s=%d" % (k, v) for k, v in tools.most_common()))

    agents = report["agents"]
    L.append("")
    L.append("-- SUBAGENTS (%d) %s" % (len(agents), "-" * (W - 18)))
    if not agents:
        L.append("  none spawned in this session")
    else:
        by_parent = defaultdict(list)
        for a in agents:
            by_parent[a["parent_id"]].append(a)
        shown = set()

        def show(a, depth):
            if a["id"] in shown:
                return
            shown.add(a["id"])
            s = a["stats"]
            pad = "  " + "    " * depth
            label, desc = agent_label(a)
            models = ", ".join(sorted(s["by_model"])) or "-"
            L.append("%s%s %s  [%s]" % (
                pad, "|-" if depth else "*", label, models))
            if desc:
                L.append("%s   %s" % (pad, desc[:60]))
            L.append("%s   id=%s  reqs=%s  in=%s  cw=%s  cr=%s  out=%s  %s"
                     % (pad, a["id"][:9], n(s["requests"]),
                        n(s["input_tokens"]),
                        n(s["cache_creation_input_tokens"]),
                        n(s["cache_read_input_tokens"]),
                        n(s["output_tokens"]),
                        dur(s["first_ts"], s["last_ts"])))
            if s["tools"]:
                L.append("%s   tools: %s" % (pad, ", ".join(
                    "%s=%d" % (k, v) for k, v in s["tools"].most_common(6))))
            for c in by_parent.get(a["id"], []):
                show(c, depth + 1)

        for a in by_parent.get("", []):
            show(a, 0)
        for a in agents:  # link_parents leaves none behind; never hide one anyway
            show(a, 0)

        L.append("")
        L.append("  subagent totals:")
        L += token_block(report["agent_total"], "    ")
        L.append("")
        L += model_table(report["agent_total"]["by_model"], "    ")

    if report["workflows"]:
        L.append("")
        L.append("-- WORKFLOWS " + "-" * (W - 13))
        for w in report["workflows"]:
            L.append("  %-28s %-10s agents=%s model=%s" % (
                (w["name"] or w["runId"] or "?")[:28], w["status"] or "?",
                w["agentCount"], w["defaultModel"]))

    L.append("")
    L.append("-- GRAND TOTAL (main + subagents) " + "-" * (W - 34))
    L += token_block(report["grand_total"])
    L.append("")
    L += model_table(report["grand_total"]["by_model"], "  ")
    L.append("=" * W)
    return "\n".join(L)


def jsonable(s):
    out = {k: v for k, v in s.items() if k not in ("by_model", "tools")}
    out["by_model"] = {m: dict(v) for m, v in s["by_model"].items()}
    out["tools"] = dict(s["tools"])
    total_in = s["input_tokens"] + s["cache_creation_input_tokens"] + s["cache_read_input_tokens"]
    out["total_input_tokens"] = total_in
    out["cache_hit_rate"] = round(
        100.0 * s["cache_read_input_tokens"] / total_in, 2) if total_in else 0.0
    return out


def report_json(report):
    return {
        "session_id": report["session_id"],
        "transcript": report["transcript"],
        "cwd": report["cwd"],
        "version": report["version"],
        "git_branch": report["git_branch"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "main": jsonable(report["main"]),
        "agents": [{
            "id": a["id"],
            "parent_id": a["parent_id"] or None,
            "agent_type": a["meta"].get("agentType"),
            "name": a["meta"].get("name"),
            "description": a["meta"].get("description"),
            "requested_model": a["meta"].get("model"),
            "spawn_depth": a["meta"].get("spawnDepth"),
            "stats": jsonable(a["stats"]),
        } for a in report["agents"]],
        "agent_total": jsonable(report["agent_total"]),
        "grand_total": jsonable(report["grand_total"]),
        "workflows": report["workflows"],
        "inherited_requests": report["inherited_requests"],
    }


# ------------------------------------------------------------ history log


def log_record(report, reason=None):
    """Compact one-line-per-session record for the history log."""
    g, m = report["grand_total"], report["main"]
    return {
        "schema": SCHEMA_VERSION,
        "session_id": report["session_id"],
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "cwd": report["cwd"],
        "git_branch": report["git_branch"],
        "cc_version": report["version"],
        "started_at": m["first_ts"],
        "ended_at": g["last_ts"],
        "duration_s": secs_between(g["first_ts"], g["last_ts"]),
        "user_prompts": m["user_turns"],
        "agent_count": len(report["agents"]),
        "workflow_count": len(report["workflows"]),
        "main": jsonable(m),
        "agent_total": jsonable(report["agent_total"]),
        "grand_total": jsonable(g),
        "agents": [{
            "id": a["id"],
            "parent_id": a["parent_id"] or None,
            "agent_type": a["meta"].get("agentType"),
            "name": a["meta"].get("name"),
            "models": sorted(a["stats"]["by_model"]),
            "requests": a["stats"]["requests"],
            "input_tokens": a["stats"]["input_tokens"],
            "output_tokens": a["stats"]["output_tokens"],
            "cache_creation_input_tokens": a["stats"]["cache_creation_input_tokens"],
            "cache_read_input_tokens": a["stats"]["cache_read_input_tokens"],
        } for a in report["agents"]],
        "inherited_requests": report["inherited_requests"],
        "request_keys": report["request_keys"],
    }


def append_record(record, path=None):
    path = path or log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def record_rank(rec):
    return (rec.get("schema") or 0,
            (rec.get("grand_total") or {}).get("output_tokens") or 0)


def read_log(path=None):
    """Records from the log, deduped per session id.

    A session can be logged more than once -- /clear fires SessionEnd while the
    id lives on, and a backfill may re-log a session. The newest schema wins
    (older ones over-count forks and undercount output), then the record with
    the most output tokens, which is the most complete one.
    """
    path = path or log_path()
    best = {}
    for rec in iter_rows(path):
        sid = rec.get("session_id")
        if not sid:
            continue
        prev = best.get(sid)
        if prev is None or record_rank(rec) > record_rank(prev):
            best[sid] = rec
    return sorted(best.values(), key=lambda r: r.get("ended_at") or "")


def log_owners(path=None):
    """One pass over the log: (request key -> first session id that logged
    it, set of every session id logged)."""
    owners, logged = {}, set()
    for rec in iter_rows(path or log_path()):
        sid = rec.get("session_id")
        if not sid:
            continue
        logged.add(sid)
        for k in rec.get("request_keys") or ():
            owners.setdefault(k, sid)
    return owners, logged


def hook_payload():
    """SessionEnd hooks receive JSON on stdin; tolerate its absence."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def do_log(args):
    payload = hook_payload()
    session_id = args.session or payload.get("session_id")
    transcript = payload.get("transcript_path")
    cwd = args.project_dir or payload.get("cwd") or os.getcwd()

    if transcript and os.path.exists(transcript):
        path = transcript
        session_id = session_id or os.path.basename(path)[:-6]
    else:
        path, session_id = find_session(session_id, cwd)

    owners, _ = log_owners(args.log_file)
    report = build_report(path, session_id, cwd, owners)
    if not report["grand_total"]["requests"]:
        return 0  # nothing happened; don't clutter the log

    rec = log_record(report, reason=payload.get("reason") or args.reason)
    dest = append_record(rec, args.log_file)
    if args.verbose:
        g = rec["grand_total"]
        print("session-stats: logged %s to %s (%s in / %s out)" % (
            session_id, dest, n(g["total_input_tokens"]), n(g["output_tokens"])))
    return 0


def started(path):
    """Sort key putting a session before any session resumed from it.

    A resume's copied history keeps its original timestamps but follows the
    resume's own first rows, so the first timestamped row in file order is
    when the session began. File creation time breaks ties.
    """
    ts = next((r["timestamp"] for r in iter_rows(path) if r.get("timestamp")), "")
    st = os.stat(path)
    return ts, getattr(st, "st_birthtime", st.st_mtime)


def do_backfill(args):
    owners, logged = log_owners(args.log_file)
    todo = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        name = os.path.basename(path)
        sid = name[:-len(".jsonl")]
        if name.startswith("agent-") or (sid in logged and not args.force):
            continue
        todo.append((started(path), path, sid))

    added = skipped = 0
    for _, path, sid in sorted(todo):
        try:
            report = build_report(path, sid, os.path.dirname(path), owners)
        except Exception as exc:  # a corrupt transcript must not abort the sweep
            print("session-stats: skipped %s (%s)" % (sid, exc), file=sys.stderr)
            skipped += 1
            continue
        # an empty record still has to supersede an older, double-counted one
        if not report["grand_total"]["requests"] and sid not in logged:
            skipped += 1
            continue
        rec = log_record(report, reason="backfill")
        append_record(rec, args.log_file)
        for k in rec["request_keys"]:
            owners.setdefault(k, sid)
        added += 1
    print("session-stats: backfilled %d session(s), skipped %d, log: %s"
          % (added, skipped, args.log_file or log_path()))
    return 0


def bucket_totals(records, keyfn):
    out = defaultdict(lambda: {"sessions": 0, "requests": 0, "input_tokens": 0,
                               "output_tokens": 0,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "agents": 0})
    for rec in records:
        key = keyfn(rec)
        if key is None:
            continue
        b = out[key]
        b["sessions"] += 1
        b["agents"] += rec.get("agent_count") or 0
        g = rec.get("grand_total") or {}
        for f in ("requests",) + USAGE_FIELDS:
            b[f] += g.get(f) or 0
    return out


def rollup_row(label, b, width=24):
    total_in = (b["input_tokens"] + b["cache_creation_input_tokens"]
                + b["cache_read_input_tokens"])
    return "  %-*s %6s %5s %10s %10s %12s %11s" % (
        width, label[:width], n(b["sessions"]), n(b["agents"]),
        n(b["input_tokens"]), n(b["cache_creation_input_tokens"]),
        n(total_in), n(b["output_tokens"]))


def rollup_header(what, width=24):
    return "  %-*s %6s %5s %10s %10s %12s %11s" % (
        width, what, "sess", "agts", "input", "cache wr", "total in", "output")


def do_rollup(args):
    path = args.log_file or log_path()
    records = read_log(path)
    if args.days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        records = [r for r in records
                   if (parse_ts(r.get("ended_at")) or parse_ts(r.get("logged_at"))
                       or datetime.now(timezone.utc)) >= cutoff]

    if args.json:
        print(json.dumps({
            "log": path,
            "days": args.days,
            "sessions": len(records),
            "totals": dict(bucket_totals(records, lambda r: "all")["all"]),
            "by_day": {k: dict(v) for k, v in
                       bucket_totals(records, lambda r: (r.get("ended_at") or "")[:10]).items()},
            "by_project": {k: dict(v) for k, v in
                           bucket_totals(records, lambda r: r.get("cwd") or "?").items()},
            "records": records if args.full else None,
        }, indent=2, default=str))
        return 0

    W = 88
    L = ["=" * W, "Claude Code usage history", "=" * W,
         "%-16s %s" % ("Log", path),
         "%-16s %s" % ("Sessions", "%d%s" % (
             len(records), " (last %d days)" % args.days if args.days else ""))]
    if not records:
        L.append("")
        L.append("  Log is empty. Install the SessionEnd hook, or run --backfill")
        L.append("  to import sessions from the transcripts still on disk.")
        print("\n".join(L))
        return 0

    span = "%s -> %s" % (local(records[0].get("ended_at")),
                         local(records[-1].get("ended_at")))
    L.append("%-16s %s" % ("Span", span))

    tot = bucket_totals(records, lambda r: "all")["all"]
    total_in = (tot["input_tokens"] + tot["cache_creation_input_tokens"]
                + tot["cache_read_input_tokens"])
    hit = 100.0 * tot["cache_read_input_tokens"] / total_in if total_in else 0.0
    secs = sum(r.get("duration_s") or 0 for r in records)
    L += ["",
          "-- TOTALS " + "-" * (W - 10),
          "%-22s %14s" % ("API requests", n(tot["requests"])),
          "%-22s %14s" % ("Input (uncached)", n(tot["input_tokens"])),
          "%-22s %14s" % ("Cache write", n(tot["cache_creation_input_tokens"])),
          "%-22s %14s" % ("Cache read", n(tot["cache_read_input_tokens"])),
          "%-22s %14s" % ("Output", n(tot["output_tokens"])),
          "%-22s %14s" % ("Total input", n(total_in)),
          "%-22s %13.1f%%" % ("Cache hit rate", hit),
          "%-22s %14s" % ("Subagents spawned", n(tot["agents"])),
          "%-22s %14s" % ("Elapsed (incl. idle)", fmt_secs(secs))]

    by_model = defaultdict(lambda: defaultdict(int))
    for rec in records:
        for model, m in (rec.get("grand_total", {}).get("by_model") or {}).items():
            for k, v in m.items():
                by_model[model][k] += v
    L.append("")
    L += model_table(by_model, "  ")

    days = bucket_totals(records, lambda r: (r.get("ended_at") or "")[:10])
    L += ["", "-- BY DAY " + "-" * (W - 10), rollup_header("day", 12)]
    for day in sorted(days, reverse=True)[:args.top]:
        L.append(rollup_row(day or "?", days[day], 12))

    projects = bucket_totals(records, lambda r: r.get("cwd") or "?")
    L += ["", "-- BY PROJECT " + "-" * (W - 14), rollup_header("project", 40)]
    for cwd in sorted(projects, key=lambda k: -projects[k]["output_tokens"])[:args.top]:
        L.append(rollup_row(os.path.basename(cwd.rstrip("/")) or cwd,
                            projects[cwd], 40))

    L += ["", "-- BIGGEST SESSIONS " + "-" * (W - 20),
          "  %-38s %10s %11s %11s %6s" % ("session", "output", "total in",
                                          "requests", "agents")]
    for rec in sorted(records, key=lambda r: -(r.get("grand_total", {})
                                               .get("output_tokens") or 0))[:args.top]:
        g = rec.get("grand_total") or {}
        label = "%s %s" % ((rec.get("ended_at") or "")[:10],
                           os.path.basename((rec.get("cwd") or "?").rstrip("/")))
        L.append("  %-38s %10s %11s %11s %6s" % (
            label[:38], n(g.get("output_tokens")),
            n(g.get("total_input_tokens")), n(g.get("requests")),
            n(rec.get("agent_count"))))

    L.append("=" * W)
    print("\n".join(L))
    return 0


# ------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", help="session id (default: current session)")
    ap.add_argument("--project-dir", help="cwd to resolve the session from")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--log", action="store_true",
                    help="append this session to the history log (SessionEnd hook)")
    ap.add_argument("--rollup", action="store_true",
                    help="aggregate the history log across sessions")
    ap.add_argument("--backfill", action="store_true",
                    help="log every past session not already in the log")
    ap.add_argument("--force", action="store_true",
                    help="--backfill: re-log every session on disk, logged or not")
    ap.add_argument("--log-file", help="history log path (default %s)" % DEFAULT_LOG)
    ap.add_argument("--days", type=int, default=0,
                    help="--rollup: only sessions from the last N days (0 = all)")
    ap.add_argument("--top", type=int, default=10,
                    help="--rollup: rows per table (default 10)")
    ap.add_argument("--full", action="store_true",
                    help="--rollup --json: include every session record")
    ap.add_argument("--reason", help="--log: reason to record (hook supplies one)")
    ap.add_argument("--verbose", action="store_true", help="--log: report what was written")
    args = ap.parse_args()

    if args.rollup:
        return do_rollup(args)
    if args.backfill:
        return do_backfill(args)
    if args.log:
        try:
            return do_log(args)
        except SystemExit:
            raise
        except Exception as exc:  # never break the session on the way out
            print("session-stats: %s" % exc, file=sys.stderr)
            return 0

    cwd = args.project_dir or os.getcwd()
    path, session_id = find_session(args.session, cwd)
    owners, _ = log_owners(args.log_file)
    report = build_report(path, session_id, cwd, owners)
    print(json.dumps(report_json(report), indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
