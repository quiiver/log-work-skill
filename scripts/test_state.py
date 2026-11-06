import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state


class TestStateFunctions(unittest.TestCase):
    def test_title_payload(self):
        p = state.title_payload({}, "ABC-1", "Fix the thing", now_ns=5_000_000_000)
        point = p["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]
        self.assertEqual({a["key"]: a["value"]["stringValue"] for a in point["attributes"]},
                         {"work_item": "ABC-1", "work_title": "Fix the thing"})

    def test_title_payload_size(self):
        p = state.title_payload({}, "ABC-1", "Fix", "XL", "fix", now_ns=5_000_000_000)
        m = p["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        self.assertEqual(m["name"], "log_work.work_item.info")
        self.assertEqual({a["key"]: a["value"]["stringValue"] for a in m["gauge"]["dataPoints"][0]["attributes"]},
                         {"work_item": "ABC-1", "work_title": "Fix", "work_size": "XL", "work_type": "fix"})

    def test_get_entry_default_when_missing(self):
        entry = state.get_entry({}, "/some/dir")
        self.assertEqual(entry, state.default_entry())

    def test_set_tracked_false_persists_through_get_entry(self):
        s = {}
        state.set_tracked(s, "/repo", False)
        entry = state.get_entry(s, "/repo")
        self.assertFalse(entry["tracked"])

    def test_extract_ticket_keys_finds_multiple_unique_in_order(self):
        text = "fix MZCLD-10 and also MZCLD-2, see MZCLD-10 again"
        self.assertEqual(state.extract_ticket_keys(text), ["MZCLD-10", "MZCLD-2"])

    def test_extract_ticket_keys_ignores_lowercase_and_bare_words(self):
        text = "mzcld-10 is not a match, neither is A-1 alone, but B12-5 matches"
        self.assertEqual(state.extract_ticket_keys(text), ["B12-5"])

    def test_load_state_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nonexistent.json")
            self.assertEqual(state.load_state(path), {})

    def test_save_then_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            s = {}
            state.set_tracked(s, "/repo", False)
            state.save_state(path, s)
            self.assertEqual(state.load_state(path), s)


class TestCLI(unittest.TestCase):
    def _run(self, state_path, *args, input_text=None):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.py")
        return subprocess.run(
            [sys.executable, script, "--state-path", state_path, *args],
            capture_output=True,
            text=True,
            input=input_text,
        )

    def test_config_set_get_unset(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            self._run(path, "config", "jiraProject", "MZCLD")
            self._run(path, "config", "cloudId", "abc")
            self._run(path, "config", "cloudId")
            result = self._run(path, "config")
            self.assertEqual(json.loads(result.stdout), {"jiraProject": "MZCLD"})

    def test_session_start_reports_git_branch_and_keys(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            repo = os.path.join(d, "repo")
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "checkout", "-q", "-b", "feature/MZCLD-9-thing"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "-c", "user.email=t@t.com", "-c", "user.name=t",
                 "commit", "-q", "--allow-empty", "-m", "start MZCLD-9"],
                check=True,
            )
            payload = json.dumps(
                {"cwd": repo, "session_id": "s1", "transcript_path": "/tmp/t.jsonl"}
            )
            result = self._run(state_path, "session-start", input_text=payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Branch: feature/MZCLD-9-thing; ticket keys in it: MZCLD-9", result.stdout)
            self.assertIn("ACTION REQUIRED", result.stdout)

    def test_session_start_non_git_dir_reports_isRepo_false(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            plain_dir = os.path.join(d, "plain")
            os.makedirs(plain_dir)
            payload = json.dumps(
                {"cwd": plain_dir, "session_id": "s1", "transcript_path": "/tmp/t.jsonl"}
            )
            result = self._run(state_path, "session-start", input_text=payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("not a git repo", result.stdout)

    def test_session_start_gemini_format_and_cwd_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.py")
            # no cwd on stdin: falls back to the process cwd
            result = subprocess.run(
                [sys.executable, script, "--state-path", state_path,
                 "session-start", "--format", "gemini"],
                capture_output=True, text=True, input="", cwd=d,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            out = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "SessionStart")
            self.assertIn(f"SessionStart for {os.path.realpath(d)}\n", out["additionalContext"])

    def test_work_item_belongs_to_the_session(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            start = lambda sid: self._run(state_path, "session-start",
                                          input_text=json.dumps({"cwd": d, "session_id": sid})).stdout
            out = start("s-1")
            self.assertIn("Work item: none yet", out)
            self.assertIn("ACTION REQUIRED", out)
            item = self._run(state_path, "set-item", "s-1", "WORKLOG", "--cwd", d).stdout.strip()
            self.assertRegex(item, r"^WORKLOG:\d{4}-\d\d-\d\d-[0-9a-f]{4}$")
            self.assertEqual(state.extract_ticket_keys(item), [])  # never a Jira key
            self.assertEqual(self._run(state_path, "item", "s-1").stdout.strip(), item)
            # resumed: keeps its item, no question
            out = start("s-1")
            self.assertIn(f"Work item: {item} (already set", out)
            self.assertNotIn("ACTION REQUIRED", out)
            # a new session in the same directory inherits nothing, but sees recent items as hints
            out = start("s-2")
            self.assertIn("Work item: none yet", out)
            self.assertIn(f"Recent work items: {item}", out)
            self.assertEqual(self._run(state_path, "item", "s-2").stdout.strip(), "")
            self._run(state_path, "set-item", "s-2", "MZCLD-7", "--cwd", d)
            self.assertEqual(state.recent_items(state_path), ["MZCLD-7", item])
            # a second WORKLOG pick is a new item
            self.assertNotEqual(self._run(state_path, "set-item", "s-3", "WORKLOG", "--cwd", d).stdout.strip(), item)

    def test_session_item_ignores_inherited_session_start_rows(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            # old ledger rows: session-start recorded the directory's ticket (inherited, not chosen)
            state.append_ledger(state_path, {"event": "session-start", "session_id": "s-1", "ticketKey": "MZCLD-1"})
            self.assertIsNone(state.session_item(state_path, "s-1"))
            state.append_ledger(state_path, {"event": "set-ticket", "session_id": "s-1", "ticketKey": "MZCLD-2"})
            self.assertEqual(state.session_item(state_path, "s-1"), "MZCLD-2")

    def test_untracked_directory_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            self._run(state_path, "set-tracked", d, "false")
            out = self._run(state_path, "session-start", input_text=json.dumps({"cwd": d, "session_id": "s"})).stdout
            self.assertIn("Tracking is off", out)
            self.assertFalse(os.path.exists(os.path.join(d, "log-work-sessions.jsonl")))

    def test_info_payload_shape(self):
        cfg = {"otelGcpProject": "p", "otelInstance": "h", "otelRegion": "us-east1"}
        p = state.info_payload(cfg, "s-1", "MZCLD-1", "x" * 200, "repo", now_ns=5_000_000_000)
        rm = p["resourceMetrics"][0]
        res = {a["key"]: a["value"]["stringValue"] for a in rm["resource"]["attributes"]}
        self.assertEqual(res, {"service.name": "log-work", "service.instance.id": "h",
                               "cloud.region": "us-east1", "gcp.project_id": "p"})
        metric = rm["scopeMetrics"][0]["metrics"][0]
        self.assertEqual(metric["name"], "log_work.session.info")
        point = metric["gauge"]["dataPoints"][0]
        self.assertEqual(point["asInt"], "5")
        attrs = {a["key"]: a["value"]["stringValue"] for a in point["attributes"]}
        self.assertEqual(attrs["session.id"], "s-1")
        self.assertEqual(attrs["work_item"], "MZCLD-1")
        self.assertEqual(len(attrs["work_title"]), 120)
        self.assertNotIn("user.email", attrs)
        p = state.info_payload({**cfg, "userEmail": "a@b.c"}, "s-1", "W")
        point = p["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]
        self.assertIn({"key": "user.email", "value": {"stringValue": "a@b.c"}}, point["attributes"])

    def test_emit_info_skipped_without_endpoint(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            # no otelEndpoint configured: session-start must not try to send
            payload = json.dumps({"cwd": d, "session_id": "s-9"})
            result = self._run(state_path, "session-start", input_text=payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self._run(state_path, "emit-info", "--session", "s", "--work-item", "W")
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
