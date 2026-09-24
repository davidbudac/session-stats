"""Synthetic transcripts for the tests: row builders plus a throwaway
~/.claude/projects tree and history log in a temp dir."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "session-stats")
sys.path.insert(0, SKILL)

import session_stats as ss  # noqa: E402

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ts(sec):
    return (EPOCH + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def message(req, msg_id, outs, t, tools=(), model="claude-test", inp=10, cw=100, cr=1000):
    """One API message the way the transcript stores it: a row per content
    block, same ids and usage on each, output_tokens growing. The tool_use
    blocks ([(id, name)]) come last."""
    blocks = [{"type": "text", "text": "..."}] * (len(outs) - len(tools))
    blocks += [{"type": "tool_use", "id": i, "name": name, "input": {}} for i, name in tools]
    return [{
        "type": "assistant", "requestId": req, "timestamp": ts(t + k),
        "uuid": "%s-%d" % (msg_id, k),
        "message": {"id": msg_id, "model": model, "role": "assistant", "content": [b],
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": cr}},
    } for k, (out, b) in enumerate(zip(outs, blocks))]


def user(content, t, **extra):
    row = {"type": "user", "timestamp": ts(t), "uuid": "u-%s" % t,
           "message": {"role": "user", "content": content}}
    row.update(extra)
    return row


def tool_result(tool_id, t):
    return user([{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}], t)


class Case(unittest.TestCase):
    """Points session_stats at a temp projects dir and a temp log."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.projects = os.path.join(self.tmp, "projects")
        self.project = os.path.join(self.projects, "-tmp-proj")
        self.log = os.path.join(self.tmp, "sessions.jsonl")
        os.makedirs(self.project)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["CLAUDE_SESSION_STATS_LOG"] = self.log
        for p in (mock.patch.object(ss, "PROJECTS", self.projects),
                  mock.patch.object(ss, "DEFAULT_LOG", self.log),
                  mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(sys, "stdin", io.StringIO(""))):
            p.start()
            self.addCleanup(p.stop)

    def write(self, path, rows, tail=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.write(tail)
        return path

    def main(self, sid, rows, tail=""):
        rows = [dict(r, sessionId=sid, cwd="/tmp/proj") for r in rows]
        return self.write(os.path.join(self.project, sid + ".jsonl"), rows, tail)

    def agent(self, sid, aid, rows, meta=None, legacy=False):
        rows = [dict(r, sessionId=sid, agentId=aid, isSidechain=True) for r in rows]
        if legacy:
            return self.write(os.path.join(self.project, "agent-%s.jsonl" % aid), rows)
        path = os.path.join(self.project, sid, "subagents", "agent-%s.jsonl" % aid)
        self.write(path, rows)
        if meta is not None:
            with open(path[:-len(".jsonl")] + ".meta.json", "w") as fh:
                json.dump(meta, fh)
        return path

    def report(self, sid, owners=None):
        path = os.path.join(self.project, sid + ".jsonl")
        return ss.build_report(path, sid, "/tmp/proj", owners)

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["session_stats.py"] + list(argv)), \
                redirect_stdout(out):
            ss.main()
        return out.getvalue()

    def records(self):
        with open(self.log) as fh:
            return [json.loads(line) for line in fh if line.strip()]
