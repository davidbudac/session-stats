import json
import os
import subprocess
import sys
import tempfile
import unittest

from support import SKILL, ss

VISUALIZE = os.path.join(SKILL, "visualize.py")


class Visualize(unittest.TestCase):

    def test_shares_read_log(self):
        import visualize
        self.assertIs(visualize.read_log, ss.read_log)

    def test_honors_log_env(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = os.path.join(tmp.name, "custom.jsonl")
        out = os.path.join(tmp.name, "dash.html")
        with open(log, "w") as fh:
            for schema, output in ((1, 1000), (2, 400)):
                fh.write(json.dumps({
                    "schema": schema, "session_id": "A", "cwd": "/x/proj",
                    "ended_at": "2026-01-02T00:00:00Z",
                    "grand_total": {"output_tokens": output}}) + "\n")
        env = dict(os.environ, CLAUDE_SESSION_STATS_LOG=log)
        res = subprocess.run([sys.executable, VISUALIZE, "--out", out], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(out) as fh:
            html = fh.read()
        self.assertIn(log, html)
        self.assertIn('"o":400', html)  # schema 2 wins, via session_stats.read_log


if __name__ == "__main__":
    unittest.main()
