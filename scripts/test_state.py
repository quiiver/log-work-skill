import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state


class TestStateFunctions(unittest.TestCase):
    def test_get_entry_default_when_missing(self):
        entry = state.get_entry({}, "/some/dir")
        self.assertEqual(entry, state.default_entry())

    def test_set_ticket_sets_pending_true(self):
        s = {}
        entry = state.set_ticket(s, "/repo", "MZCLD-1", "main")
        self.assertEqual(entry["ticketKey"], "MZCLD-1")
        self.assertEqual(entry["branch"], "main")
        self.assertTrue(entry["pendingLog"])
        self.assertTrue(s["/repo"]["pendingLog"])

    def test_set_tracked_false_persists_through_get_entry(self):
        s = {}
        state.set_tracked(s, "/repo", False)
        entry = state.get_entry(s, "/repo")
        self.assertFalse(entry["tracked"])

    def test_clear_ticket_resets_fields_but_keeps_tracked(self):
        s = {}
        state.set_ticket(s, "/repo", "MZCLD-1", "main")
        state.set_tracked(s, "/repo", False)
        entry = state.clear_ticket(s, "/repo")
        self.assertIsNone(entry["ticketKey"])
        self.assertIsNone(entry["branch"])
        self.assertFalse(entry["pendingLog"])
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
            state.set_ticket(s, "/repo", "MZCLD-1", "main")
            state.save_state(path, s)
            self.assertEqual(state.load_state(path), s)

    def test_slugify_summary_basic(self):
        self.assertEqual(
            state.slugify_summary("Fix leaderboard duplicate entries for same user!"),
            "fix-leaderboard-duplicate-entries-for",
        )

    def test_slugify_summary_empty(self):
        self.assertEqual(state.slugify_summary(""), "")

    def test_git_username_from_email(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "alice@example.com"],
                check=True,
            )
            self.assertEqual(state.git_username(repo), "alice")


class TestCLI(unittest.TestCase):
    def _run(self, state_path, *args, input_text=None):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.py")
        return subprocess.run(
            [sys.executable, script, "--state-path", state_path, *args],
            capture_output=True,
            text=True,
            input=input_text,
        )

    def test_set_ticket_then_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            result = self._run(path, "set-ticket", "/repo", "MZCLD-1", "--branch", "main")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self._run(path, "get", "/repo")
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = json.loads(result.stdout)
            self.assertEqual(entry["ticketKey"], "MZCLD-1")
            self.assertEqual(entry["branch"], "main")
            self.assertTrue(entry["pendingLog"])

    def test_set_tracked_false_then_get(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            self._run(path, "set-tracked", "/repo", "false")
            result = self._run(path, "get", "/repo")
            entry = json.loads(result.stdout)
            self.assertFalse(entry["tracked"])

    def test_extract_keys_cli_reads_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            result = self._run(path, "extract-keys", input_text="see MZCLD-7 please")
            self.assertEqual(json.loads(result.stdout), ["MZCLD-7"])

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
            self.assertIn("feature/MZCLD-9-thing", result.stdout)
            self.assertIn("MZCLD-9", result.stdout)

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
            self.assertIn("isRepo=False", result.stdout)

    def test_git_username_cli(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            repo = os.path.join(d, "repo")
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "bob@example.com"],
                check=True,
            )
            result = self._run(state_path, "git-username", repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "bob")

    def test_slugify_cli(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            result = self._run(
                state_path, "slugify", input_text="Add real-time presence indicator!!"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "add-real-time-presence-indicator")


if __name__ == "__main__":
    unittest.main()
