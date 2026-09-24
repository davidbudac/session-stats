import json
import os
import unittest

from support import Case, message, ss, tool_result, ts, user


class Parsing(Case):

    def test_last_row_usage_wins(self):
        self.main("s1", [user("hi", 0)] + message("r1", "m1", [3, 7, 12], 1))
        g = self.report("s1")["grand_total"]
        self.assertEqual((g["requests"], g["output_tokens"], g["input_tokens"]), (1, 12, 10))

    def test_synthetic_rows_skipped(self):
        err = {"type": "assistant", "isApiErrorMessage": True, "timestamp": ts(5),
               "message": {"id": "x", "model": "<synthetic>",
                           "content": [{"type": "text", "text": "API Error"}],
                           "usage": {"input_tokens": 999, "output_tokens": 999}}}
        self.main("s1", message("r1", "m1", [5], 0) + [err])
        m = self.report("s1")["main"]
        self.assertEqual((m["requests"], m["output_tokens"], m["api_errors"]), (1, 5, 1))
        self.assertNotIn("<synthetic>", m["by_model"])

    def test_half_written_trailing_line(self):
        self.main("s1", message("r1", "m1", [5], 0),
                  tail='{"type": "assistant", "requestId": "r2", "mess')
        self.assertEqual(self.report("s1")["grand_total"]["output_tokens"], 5)

    def test_user_prompt_classification(self):
        rows = [
            user("typed prompt", 0),
            user("<command-name>/model</command-name>", 1),
            user([{"type": "text", "text": "prompt as blocks"}], 2),
            user("skill body", 3, isMeta=True),
            user("This session is being continued", 4, isCompactSummary=True),
            user("<local-command-stdout>ok</local-command-stdout>", 5),
            user("<local-command-stderr>no</local-command-stderr>", 6),
            user("<local-command-caveat>Caveat: ...</local-command-caveat>", 7),
            user([{"type": "text", "text": "[Request interrupted by user]"}], 8),
            user("[Request interrupted by user for tool use]", 9),
            tool_result("toolu_1", 10),
            user("<task-notification>done</task-notification>", 11,
                 origin={"kind": "task-notification"}),
        ]
        self.main("s1", rows + message("r1", "m1", [5], 20))
        self.assertEqual(self.report("s1")["main"]["user_turns"], 3)


class Agents(Case):

    def fork_of_main(self, full_copy):
        spawn = message("r1", "m1", [4, 9], 1, tools=[("toolu_F", "Agent")])
        history = [user("go", 0)] + spawn
        self.main("s1", history + [tool_result("toolu_F", 90)] + message("r2", "m2", [6], 91))
        if full_copy:
            copied = history
        else:  # newer builds reference the history and copy only the spawning row
            copied = [{"type": "fork-context-ref", "parentSessionId": "s1"}, spawn[-1]]
        own = [user("fork directive", 10)] + message(
            "r3", "m3", [20, 50], 11, tools=[("toolu_x", "Bash")])
        self.agent("s1", "F", copied + own, meta={
            "agentType": "fork", "isFork": True, "toolUseId": "toolu_F", "spawnDepth": 1})
        return self.report("s1")

    def check_fork_of_main(self, r):
        (f,) = r["agents"]
        self.assertEqual(f["parent_id"], "")
        self.assertEqual((r["main"]["requests"], r["main"]["output_tokens"]), (2, 15))
        self.assertEqual((f["stats"]["requests"], f["stats"]["output_tokens"]), (1, 50))
        self.assertEqual(dict(f["stats"]["tools"]), {"Bash": 1})
        self.assertEqual((r["grand_total"]["requests"], r["grand_total"]["output_tokens"]),
                         (3, 65))
        # its window starts with its own rows, not the copied ones
        self.assertEqual(f["stats"]["first_ts"], ts(10))
        self.assertIn("id=F", ss.render(r))

    def test_fork_of_main_full_copy(self):
        self.check_fork_of_main(self.fork_of_main(full_copy=True))

    def test_fork_of_main_context_ref(self):
        self.check_fork_of_main(self.fork_of_main(full_copy=False))

    def test_sibling_forks_of_main(self):
        # F2's copy of the main thread also holds the call that spawned F1
        first = [user("go", 0)] + message("r1", "m1", [5], 1, tools=[("toolu_F1", "Agent")])
        second = first + message("r2", "m2", [6], 2, tools=[("toolu_F2", "Agent")])
        self.main("s1", second)
        meta = {"agentType": "fork", "isFork": True, "spawnDepth": 1}
        self.agent("s1", "F1", first + message("rf1", "mf1", [30], 10),
                   meta=dict(meta, toolUseId="toolu_F1"))
        self.agent("s1", "F2", second + message("rf2", "mf2", [40], 11),
                   meta=dict(meta, toolUseId="toolu_F2"))
        r = self.report("s1")
        self.assertEqual({a["id"]: a["parent_id"] for a in r["agents"]}, {"F1": "", "F2": ""})
        self.assertEqual(r["grand_total"]["output_tokens"], 5 + 6 + 30 + 40)

    def test_never_its_own_parent(self):
        # no spawnDepth or isFork to rank by, and the fork's id sorts first
        spawn = message("rz", "mz", [5], 1, tools=[("toolu_A", "Agent")])
        self.main("s1", message("r1", "m1", [5], 0, tools=[("toolu_Z", "Agent")]))
        self.agent("s1", "Z9", spawn, meta={"toolUseId": "toolu_Z"})
        self.agent("s1", "A1", spawn + message("ra", "ma", [7], 2), meta={"toolUseId": "toolu_A"})
        r = self.report("s1")
        self.assertEqual({a["id"]: a["parent_id"] for a in r["agents"]}, {"Z9": "", "A1": "Z9"})
        self.assertEqual(r["grand_total"]["output_tokens"], 17)

    def test_fork_of_subagent(self):
        self.main("s1", [user("go", 0)] + message(
            "r1", "m1", [5], 1, tools=[("toolu_X", "Agent")]) + [tool_result("toolu_X", 200)])
        x_start = [user("task", 2)] + message("rx1", "mx1", [3, 8], 3, tools=[("toolu_F", "Agent")])
        x_more = message("rx2", "mx2", [4], 20, tools=[("toolu_G", "Agent")])
        self.agent("s1", "X", x_start + x_more + message("rx3", "mx3", [6], 60), meta={
            "agentType": "fork", "isFork": True, "toolUseId": "toolu_X", "spawnDepth": 1})
        # F: no parentAgentId, found by toolUseId; G (a fork too, and holding
        # F's spawning call in its copy) names its parent -- only spawnDepth
        # tells X and G apart as F's originator
        self.agent("s1", "F", x_start + [user("directive", 10)] + message("rf", "mf", [30], 11),
                   meta={"agentType": "fork", "isFork": True, "toolUseId": "toolu_F",
                         "spawnDepth": 2})
        self.agent("s1", "G", x_start + x_more + [user("directive", 30)] + message(
            "rg", "mg", [40], 31), meta={"agentType": "fork", "isFork": True,
                                        "toolUseId": "toolu_G", "parentAgentId": "X",
                                        "spawnDepth": 2})
        r = self.report("s1")
        agents = {a["id"]: a for a in r["agents"]}
        self.assertEqual({k: a["parent_id"] for k, a in agents.items()},
                         {"X": "", "F": "X", "G": "X"})
        self.assertEqual(agents["X"]["stats"]["output_tokens"], 8 + 4 + 6)
        self.assertEqual(agents["F"]["stats"]["output_tokens"], 30)
        self.assertEqual(agents["G"]["stats"]["output_tokens"], 40)
        self.assertEqual(r["grand_total"]["output_tokens"], 5 + 18 + 30 + 40)
        self.assertEqual(r["grand_total"]["requests"], 1 + 3 + 1 + 1)
        self.assertEqual(dict(agents["F"]["stats"]["tools"]), {})
        text = ss.render(r)
        for aid in "XFG":
            self.assertIn("id=%s " % aid, text)

    def test_nesting(self):
        self.main("s1", message("r1", "m1", [5], 0, tools=[("toolu_A", "Agent")]))
        self.agent("s1", "A", message("ra", "ma", [5], 1, tools=[("toolu_B", "Agent")]),
                   meta={"agentType": "general-purpose", "toolUseId": "toolu_A"})
        self.agent("s1", "B", message("rb", "mb", [5], 2),
                   meta={"agentType": "general-purpose", "toolUseId": "toolu_B"})
        self.agent("s1", "C", message("rc", "mc", [5], 3),
                   meta={"agentType": "general-purpose", "parentAgentId": "B"})
        r = self.report("s1")
        self.assertEqual({a["id"]: a["parent_id"] for a in r["agents"]},
                         {"A": "", "B": "A", "C": "B"})
        text = ss.render(r)
        self.assertIn("\n          |- general-purpose", text)  # C, two levels down

    def test_cycles_and_unknown_parents_stay_visible(self):
        self.main("s1", message("r1", "m1", [5], 0))
        self.agent("s1", "P", message("rp", "mp", [5], 1), meta={"parentAgentId": "Q"})
        self.agent("s1", "Q", message("rq", "mq", [5], 2), meta={"parentAgentId": "P"})
        self.agent("s1", "S", message("rs", "ms", [5], 3), meta={"parentAgentId": "S"})
        self.agent("s1", "U", message("ru", "mu", [5], 4), meta={"parentAgentId": "gone"})
        r = self.report("s1")
        parents = {a["id"]: a["parent_id"] for a in r["agents"]}
        self.assertEqual((parents["S"], parents["U"]), ("", ""))
        self.assertIn("", (parents["P"], parents["Q"]))
        text = ss.render(r)
        for aid in "PQSU":
            self.assertIn("id=%s " % aid, text)

    def test_legacy_layout(self):
        self.main("s1", message("r1", "m1", [5], 0))
        self.agent("s1", "L1", message("rl", "ml", [7], 1), legacy=True)
        self.agent("other", "L2", message("ro", "mo", [100], 1), legacy=True)
        r = self.report("s1")
        self.assertEqual([a["id"] for a in r["agents"]], ["L1"])
        self.assertEqual(r["grand_total"]["output_tokens"], 12)


class History(Case):

    def source(self):
        return ([user("first", 0)] + message("r1", "m1", [5, 10], 1) +
                [user("second", 5)] + message("r2", "m2", [20], 6))

    def resumed(self, extra=True):
        """A resume: its own first rows, then a verbatim copy of the source
        (same ids, uuids and timestamps), then new work."""
        rows = [{"type": "queue-operation", "operation": "enqueue", "timestamp": ts(1000)}]
        rows += self.source()
        if extra:
            rows += [user("third", 1001)] + message("r3", "m3", [7], 1002)
        return rows

    def test_resumed_session_excludes_inherited(self):
        self.main("A", self.source())
        self.cli("--log", "--session", "A")
        self.main("B", self.resumed())
        self.cli("--log", "--session", "B")
        recs = {r["session_id"]: r for r in self.records()}
        a, b = recs["A"], recs["B"]
        self.assertEqual((a["schema"], len(a["request_keys"])), (2, 2))
        self.assertEqual(b["inherited_requests"], 2)
        self.assertEqual(b["grand_total"]["output_tokens"], 7)
        self.assertEqual(b["user_prompts"], 1)
        self.assertEqual(b["started_at"], ts(1001))
        self.assertEqual(len(b["request_keys"]), 1)
        self.assertFalse(set(a["request_keys"]) & set(b["request_keys"]))

        rollup = json.loads(self.cli("--rollup", "--json"))
        self.assertEqual(rollup["totals"]["output_tokens"], 10 + 20 + 7)

        rep = json.loads(self.cli("--session", "B", "--json"))
        self.assertEqual((rep["inherited_requests"], rep["grand_total"]["output_tokens"]), (2, 7))
        self.assertIn("Inherited requests", self.cli("--session", "B"))
        self.assertNotIn("Inherited requests", self.cli("--session", "A"))

    def test_same_session_logged_twice(self):
        self.main("A", self.source())
        self.cli("--log", "--session", "A")
        self.main("A", self.source() + [user("more", 50)] + message("r9", "m9", [3], 51))
        self.cli("--log", "--session", "A")
        first, second = self.records()
        self.assertEqual(second["inherited_requests"], 0)
        self.assertEqual(second["grand_total"]["output_tokens"], 33)
        self.assertEqual(len(second["request_keys"]), 3)
        (best,) = ss.read_log(self.log)
        self.assertEqual(best["grand_total"]["output_tokens"], 33)

    def test_read_log_prefers_newest_schema(self):
        def rec(schema, out):
            return {"schema": schema, "session_id": "A", "grand_total": {"output_tokens": out}}
        self.write(self.log, [rec(1, 1000), rec(2, 500), rec(2, 600), rec(2, 550), rec(1, 2000)])
        (best,) = ss.read_log(self.log)
        self.assertEqual((best["schema"], best["grand_total"]["output_tokens"]), (2, 600))

    def test_backfill_force(self):
        # names and mtimes both put the resumes first; only the first row's
        # timestamp says which session came first
        src = self.main("zz-source", self.source())
        self.main("aa-resume", self.resumed())
        self.main("ab-noop", self.resumed(extra=False))  # resumed, did nothing new
        os.utime(src, (2e9, 2e9))
        old = [{"schema": 1, "session_id": sid, "grand_total": {"output_tokens": out}}
               for sid, out in (("zz-source", 30), ("aa-resume", 37), ("ab-noop", 30))]
        self.write(self.log, old)
        with open(self.log) as fh:
            before = fh.read()

        self.assertIn("backfilled 0 session(s)", self.cli("--backfill"))
        self.assertIn("backfilled 3 session(s)", self.cli("--backfill", "--force"))

        with open(self.log) as fh:
            self.assertTrue(fh.read().startswith(before))  # append-only
        new = self.records()[len(old):]
        self.assertEqual(new[0]["session_id"], "zz-source")
        by_sid = {r["session_id"]: r for r in new}
        self.assertEqual(len(by_sid["zz-source"]["request_keys"]), 2)
        self.assertEqual(len(by_sid["aa-resume"]["request_keys"]), 1)
        self.assertEqual(by_sid["ab-noop"]["grand_total"]["output_tokens"], 0)
        totals = json.loads(self.cli("--rollup", "--json"))["totals"]
        self.assertEqual((totals["output_tokens"], totals["sessions"]), (37, 3))

    def test_backfill_logs_source_before_resume(self):
        self.main("zz-source", self.source())
        self.main("aa-resume", self.resumed())
        self.cli("--backfill")
        recs = self.records()
        self.assertEqual([r["session_id"] for r in recs], ["zz-source", "aa-resume"])
        self.assertEqual(recs[1]["inherited_requests"], 2)


if __name__ == "__main__":
    unittest.main()
