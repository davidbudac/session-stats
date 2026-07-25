#!/usr/bin/env python3
"""Token / cache / subagent statistics for a Claude Code session.

Reads the session transcript(s) under ~/.claude/projects/ and reports
input, output and cache token usage for the main thread plus every
subagent (recursively), including the model each one actually ran on.

Usage:
    session_stats.py [--session <id>] [--json] [--project-dir <dir>]

With no --session it uses $CLAUDE_CODE_SESSION_ID, falling back to the
most recently modified transcript for the current working directory.
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

PROJECTS = os.path.expanduser("~/.claude/projects")

USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")


# ---------------------------------------------------------------- parsing


def read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # last line may be half-written
    except OSError:
        pass
    return rows


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


def summarize(rows):
    """Aggregate one transcript. Usage is repeated on every content block of a
    message, so requests are deduped on (requestId, message.id)."""
    s = blank()
    seen = set()

    for row in rows:
        ts = row.get("timestamp")
        if ts:
            if s["first_ts"] is None or ts < s["first_ts"]:
                s["first_ts"] = ts
            if s["last_ts"] is None or ts > s["last_ts"]:
                s["last_ts"] = ts

        msg = row.get("message") or {}
        content = msg.get("content")

        if row.get("type") == "user" and isinstance(content, (str, list)):
            # only count real prompts, not tool_result carrier messages
            if isinstance(content, str):
                s["user_turns"] += 1
            elif not any(isinstance(b, dict) and b.get("type") == "tool_result"
                         for b in content):
                s["user_turns"] += 1

        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    s["tools"][b.get("name", "?")] += 1

        if row.get("type") != "assistant":
            continue

        model = msg.get("model") or "unknown"
        if row.get("isApiErrorMessage"):
            s["api_errors"] += 1
        if model == "<synthetic>":
            continue

        key = (row.get("requestId"), msg.get("id"))
        if key in seen:
            continue
        seen.add(key)

        usage = msg.get("usage") or {}
        s["requests"] += 1
        s["by_model"][model]["requests"] += 1
        for f in USAGE_FIELDS:
            v = usage.get(f) or 0
            s[f] += v
            s["by_model"][model][f] += v
        cc = usage.get("cache_creation") or {}
        s["ephemeral_1h"] += cc.get("ephemeral_1h_input_tokens") or 0
        s["ephemeral_5m"] += cc.get("ephemeral_5m_input_tokens") or 0
        stu = usage.get("server_tool_use") or {}
        s["web_search"] += stu.get("web_search_requests") or 0
        s["web_fetch"] += stu.get("web_fetch_requests") or 0

    return s


def add_into(total, s):
    for f in USAGE_FIELDS + ("requests", "ephemeral_1h", "ephemeral_5m",
                             "web_search", "web_fetch", "api_errors"):
        total[f] += s[f]
    total["tools"].update(s["tools"])
    for model, m in s["by_model"].items():
        for k, v in m.items():
            total["by_model"][model][k] += v
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
                meta = json.load(open(meta_path))
            except (OSError, json.JSONDecodeError):
                meta = {}
        agents.append({"id": agent_id, "path": path, "meta": meta})

    # legacy layout: <project>/agent-<id>.jsonl carrying sessionId inside
    for path in sorted(glob.glob(os.path.join(project_dir, "agent-*.jsonl"))):
        rows = read_jsonl(path)
        if not rows or rows[0].get("sessionId") != session_id:
            continue
        agent_id = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        agents.append({"id": agent_id, "path": path, "meta": {}})

    for a in agents:
        a["rows"] = read_jsonl(a["path"])
        a["stats"] = summarize(a["rows"])
    return agents


def link_parents(agents, main_rows):
    """Attach parent_id using meta.parentAgentId, or by finding which
    transcript issued the spawning tool_use id."""
    owner = {}  # tool_use id -> agent id that issued it ('' = main thread)
    for row in main_rows:
        for b in (row.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                owner[b.get("id")] = ""
    for a in agents:
        for row in a["rows"]:
            for b in (row.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    owner[b.get("id")] = a["id"]

    known = {a["id"] for a in agents}
    for a in agents:
        parent = a["meta"].get("parentAgentId")
        if parent not in known:
            parent = owner.get(a["meta"].get("toolUseId"), "")
            if parent not in known:
                parent = ""
        a["parent_id"] = parent
    return agents


def read_workflows(project_dir, session_id):
    out = []
    for path in sorted(glob.glob(os.path.join(
            project_dir, session_id, "workflows", "wf_*.json"))):
        try:
            d = json.load(open(path))
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


# ------------------------------------------------------------- rendering


def n(v):
    return "{:,}".format(int(v or 0))


def dur(first, last):
    if not first or not last:
        return "-"
    try:
        a = datetime.fromisoformat(first.replace("Z", "+00:00"))
        b = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    secs = int((b - a).total_seconds())
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return ("%dh%02dm" % (h, m)) if h else ("%dm%02ds" % (m, s)) if m else ("%ds" % s)


def local(ts):
    if not ts:
        return "-"
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return d.astimezone().strftime("%Y-%m-%d %H:%M")


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


def model_table(s, indent=""):
    rows = sorted(s["by_model"].items(), key=lambda kv: -kv[1]["output_tokens"])
    out = ["%s%-26s %5s %11s %11s %11s %11s" % (
        indent, "model", "reqs", "input", "cache wr", "cache rd", "output")]
    for model, m in rows:
        out.append("%s%-26s %5s %11s %11s %11s %11s" % (
            indent, model[:26], n(m["requests"]), n(m["input_tokens"]),
            n(m["cache_creation_input_tokens"]), n(m["cache_read_input_tokens"]),
            n(m["output_tokens"])))
    return out


def agent_label(a):
    meta = a["meta"]
    bits = [meta.get("agentType") or "agent"]
    if meta.get("name"):
        bits.append("(%s)" % meta["name"])
    label = " ".join(bits)
    desc = meta.get("description")
    return label, desc


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
    L.append("")
    L.append("-- MAIN THREAD " + "-" * (W - 15))
    L += token_block(report["main"])
    L.append("")
    L += model_table(report["main"], "  ")

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

        def walk(parent, depth):
            for a in by_parent.get(parent, []):
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
                walk(a["id"], depth + 1)

        walk("", 0)
        orphan_parents = set(by_parent) - {a["id"] for a in agents} - {""}
        for p in sorted(orphan_parents):
            walk(p, 0)

        L.append("")
        L.append("  subagent totals:")
        L += token_block(report["agent_total"], "    ")
        L.append("")
        L += model_table(report["agent_total"], "    ")

    if report["workflows"]:
        L.append("")
        L.append("-- WORKFLOWS " + "-" * (W - 13))
        for w in report["workflows"]:
            L.append("  %-28s %-10s agents=%s model=%s" % (
                (w["name"] or w["runId"])[:28], w["status"] or "?",
                w["agentCount"], w["defaultModel"]))

    L.append("")
    L.append("-- GRAND TOTAL (main + subagents) " + "-" * (W - 34))
    L += token_block(report["grand_total"])
    L.append("")
    L += model_table(report["grand_total"], "  ")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="session id (default: current session)")
    ap.add_argument("--project-dir", help="cwd to resolve the session from")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    cwd = args.project_dir or os.getcwd()
    path, session_id = find_session(args.session, cwd)
    project_dir = os.path.dirname(path)

    main_rows = [r for r in read_jsonl(path) if not r.get("isSidechain")]
    main_stats = summarize(main_rows)

    agents = link_parents(collect_agents(project_dir, session_id), main_rows)

    agent_total = blank()
    for a in agents:
        add_into(agent_total, a["stats"])
    grand = add_into(add_into(blank(), main_stats), agent_total)

    report = {
        "session_id": session_id,
        "transcript": path,
        "cwd": next((r["cwd"] for r in main_rows if r.get("cwd")), cwd),
        "main": main_stats,
        "agents": agents,
        "agent_total": agent_total,
        "grand_total": grand,
        "workflows": read_workflows(project_dir, session_id),
    }

    if args.json:
        print(json.dumps({
            "session_id": session_id,
            "transcript": path,
            "cwd": report["cwd"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "main": jsonable(main_stats),
            "agents": [{
                "id": a["id"],
                "parent_id": a["parent_id"] or None,
                "agent_type": a["meta"].get("agentType"),
                "name": a["meta"].get("name"),
                "description": a["meta"].get("description"),
                "requested_model": a["meta"].get("model"),
                "spawn_depth": a["meta"].get("spawnDepth"),
                "stats": jsonable(a["stats"]),
            } for a in agents],
            "agent_total": jsonable(agent_total),
            "grand_total": jsonable(grand),
            "workflows": report["workflows"],
        }, indent=2))
    else:
        print(render(report))


if __name__ == "__main__":
    main()
