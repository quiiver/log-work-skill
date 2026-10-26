import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage


def cost_state(total, start=1000, model_cost=None):
    return {"type": "cost-state", "startTime": start, "totalCostUSD": total, "totalAPIDuration": 2000,
            "totalToolDuration": 500, "totalLinesAdded": 7, "totalLinesRemoved": 1,
            "modelUsage": {"claude-x": {"costUSD": model_cost or total, "inputTokens": 10, "outputTokens": 5,
                                        "cacheReadInputTokens": 3, "cacheCreationInputTokens": 2,
                                        "thinkingTokens": 0}}}


class TestUsage(unittest.TestCase):
    def test_scan_is_incremental_and_keeps_latest_per_run(self):
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            with open(t, "w") as f:
                f.write(json.dumps({"type": "assistant"}) + "\n")
                f.write(json.dumps(cost_state(1.0)) + "\n")
                f.write(json.dumps(cost_state(2.5)) + "\n")
                f.write(json.dumps(cost_state(0.4, start=2000)) + "\n")
                f.write('{"type": "cost-state", "partial')  # line still being written
            runs, off = usage.scan(t, 0)
            self.assertEqual(runs["1000"]["totalCostUSD"], 2.5)
            self.assertEqual(runs["2000"]["totalCostUSD"], 0.4)
            self.assertLess(off, os.path.getsize(t))
            runs2, off2 = usage.scan(t, off)
            self.assertEqual((runs2, off2), ({}, off))
            runs3, _ = usage.scan(t, os.path.getsize(t) + 100)  # truncated file: rescan
            self.assertIn("1000", runs3)

    def test_usage_metrics_shape(self):
        runs = {"1000": cost_state(2.5)}
        metrics = {m["name"]: m for m in usage.usage_metrics("s-1", runs, now_ns=5_000_000_000)}
        self.assertEqual(set(metrics), {"log_work.usage.cost_usd", "log_work.usage.tokens",
                                        "log_work.usage.api_seconds", "log_work.usage.tool_seconds",
                                        "log_work.usage.lines"})
        cost = metrics["log_work.usage.cost_usd"]["sum"]
        self.assertTrue(cost["isMonotonic"])
        self.assertEqual(cost["aggregationTemporality"], 2)
        p = cost["dataPoints"][0]
        self.assertEqual(p["asDouble"], 2.5)
        self.assertEqual(p["startTimeUnixNano"], "1000000000")
        attrs = {a["key"]: a["value"]["stringValue"] for a in p["attributes"]}
        self.assertEqual(attrs, {"session.id": "s-1", "run": "1000", "model": "claude-x"})
        self.assertEqual(len(metrics["log_work.usage.tokens"]["sum"]["dataPoints"]), 5)
        self.assertEqual(metrics["log_work.usage.api_seconds"]["sum"]["dataPoints"][0]["asDouble"], 2.0)

    def test_counts_successful_commits_and_prs_only(self):
        def call(i, cmd, name="Bash"):
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": i, "name": name, "input": {"command": cmd}}]}}
        def result(i, err=False):
            return {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": i, "is_error": err, "content": "x"}]}}
        lines = [call("a", "git add . && git commit -m 'x'"), result("a"),
                 call("b", "git -C /repo commit -q -m y"), result("b", err=True),   # failed
                 call("c", "gh pr create --fill"), result("c"),
                 call("d", "", name="mcp__github__create_pull_request"), result("d"),
                 call("e", "git log --oneline"), result("e"),                     # not a commit
                 call("g", "python3 - <<'EOF'\nx = \"git commit -m 'x'\" && 'gh pr create'\nEOF"), result("g"),  # heredoc text
                 call("h", "echo 'run git commit later'"), result("h"),             # mentioned, not run
                 call("f", "git commit -m pending")]                              # no result yet
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            open(t, "w").write("".join(json.dumps(x) + "\n" for x in lines))
            pending, counts = {}, {}
            _, off = usage.scan(t, 0, pending, counts)
            self.assertEqual(counts, {"commits": 1, "prs": 2})
            self.assertEqual(set(pending), {"f"})
            open(t, "a").write(json.dumps(result("f")) + "\n")  # result lands in a later scan
            usage.scan(t, off, pending, counts)
            self.assertEqual(counts["commits"], 2)
            self.assertEqual(pending, {})

    def test_emit_skips_without_endpoint(self):
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            open(t, "w").write(json.dumps(cost_state(1.0)) + "\n")
            self.assertEqual(usage.emit(os.path.join(d, "state.json"), "s", t), "skipped")


if __name__ == "__main__":
    unittest.main()
