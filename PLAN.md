# log-work Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `log-work` personal Claude Code skill that matches (or creates) a Jira ticket for whatever directory you're working in, keeps a small state file tracking that association, and logs a summary comment of session activity — automatically at the start of each new session (catch-up for the previous session) and on demand via `/log-work`.

**Architecture:** A SessionStart hook shells out to a Python CLI (`scripts/state.py`) that reads/writes a JSON state file and inspects git (branch + recent commits) — all deterministic, no LLM involved — and prints a plain-text summary that Claude Code injects into the new session's context. The `log-work` SKILL.md then contains the actual judgment-driven procedure (matching via Jira search, disambiguation, ticket creation with confirmation, status bump, comment posting) that the agent follows, using the same `state.py` CLI to persist results. `/log-work` invokes the same skill in a different mode for on-demand logging/rematching.

**Tech Stack:** Python 3 (stdlib only — `argparse`, `json`, `re`, `subprocess`, `unittest`), Claude Code hooks (`SessionStart`), Claude Code skill (Markdown + frontmatter), Atlassian MCP tools (`mcp__plugin_atlassian_atlassian__*`).

**Spec:** `~/.claude/skills/log-work/DESIGN.md`

## Global Constraints

- Default Jira project for new tickets: **MZCLD**.
- Logging is via Jira **comments** (`addCommentToJiraIssue`), never `addWorklogToJiraIssue` — no time tracking in v1.
- Creating a new Jira ticket always requires explicit user confirmation before calling `createJiraIssue`. Matching/logging against an already-resolved ticket is automatic.
- No `Stop`/`SessionEnd` hook — `Stop` fires after every turn (not at session end) and `SessionEnd` has only a 1.5–60s timeout with no documented MCP access, so neither can reliably do LLM+Jira work. All "log the last session" duty lives in the SessionStart catch-up (driven by the `pendingLog` state flag) plus manual `/log-work`.
- State file: `~/.claude/log-work-state.json`, keyed by absolute `cwd`. Default entry when a directory has no stored entry: `{"ticketKey": null, "branch": null, "lastLoggedSessionId": null, "pendingLog": false, "tracked": true}` (tracking is opt-out, not opt-in).
- Atlassian MCP tools are **deferred tools** — any task that calls one must first call `ToolSearch` with `select:<tool names>` to load their schemas before invoking them.
- This work lives under `~/.claude/skills/log-work/`, which is not a git repository — no `git commit` steps in this plan for files under that path. Do not run `git init` there.

---

## File Structure

- Create: `~/.claude/skills/log-work/scripts/state.py` — state CRUD + git inspection, as both an importable module and a CLI.
- Create: `~/.claude/skills/log-work/scripts/test_state.py` — unit tests (stdlib `unittest`) for the module and CLI.
- Modify: `~/.claude/settings.json` — register the `SessionStart` hook.
- Create: `~/.claude/skills/log-work/SKILL.md` — the skill's agent-facing instructions (frontmatter + procedure).

---

### Task 1: State store module + CLI

**Files:**
- Create: `~/.claude/skills/log-work/scripts/state.py`
- Test: `~/.claude/skills/log-work/scripts/test_state.py`

**Interfaces:**
- Produces (used by Task 2's hook registration and Task 3's SKILL.md):
  - CLI: `python3 state.py [--state-path PATH] get <cwd>` → prints one JSON object: `{"ticketKey": str|null, "branch": str|null, "lastLoggedSessionId": str|null, "pendingLog": bool, "tracked": bool}`
  - CLI: `python3 state.py [--state-path PATH] set-ticket <cwd> <ticket_key> [--branch BRANCH]` → sets `ticketKey`, `branch`, forces `pendingLog=true`; prints resulting entry JSON.
  - CLI: `python3 state.py [--state-path PATH] set-pending <cwd> <true|false>` → prints resulting entry JSON.
  - CLI: `python3 state.py [--state-path PATH] set-tracked <cwd> <true|false>` → prints resulting entry JSON.
  - CLI: `python3 state.py [--state-path PATH] set-last-logged-session <cwd> <session_id>` → prints resulting entry JSON.
  - CLI: `python3 state.py [--state-path PATH] clear <cwd>` → resets `ticketKey`/`branch` to `null` and `pendingLog` to `false`, keeps `tracked`; prints resulting entry JSON.
  - CLI: `python3 state.py [--state-path PATH] extract-keys` → reads text from stdin, prints a JSON array of unique ticket keys found (order preserved).
  - CLI: `python3 state.py [--state-path PATH] session-start` → reads a JSON object from stdin (`{"cwd": ..., "session_id": ..., "transcript_path": ...}`), prints a plain-text summary block (used by Task 2's hook).
  - Default state path (no `--state-path`): `~/.claude/log-work-state.json`.

- [ ] **Step 1: Write failing tests for the core state functions**

Create `~/.claude/skills/log-work/scripts/test_state.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/.claude/skills/log-work/scripts && python3 -m unittest test_state -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'state'` (the module doesn't exist yet).

- [ ] **Step 3: Implement the core module (no CLI/session-start yet)**

Create `~/.claude/skills/log-work/scripts/state.py`:

```python
#!/usr/bin/env python3
"""CLI + library for the log-work skill's state file (~/.claude/log-work-state.json)."""
import argparse
import json
import os
import re
import subprocess
import sys

DEFAULT_STATE_PATH = os.path.expanduser("~/.claude/log-work-state.json")
TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def default_entry():
    return {
        "ticketKey": None,
        "branch": None,
        "lastLoggedSessionId": None,
        "pendingLog": False,
        "tracked": True,
    }


def get_entry(state, cwd):
    entry = default_entry()
    entry.update(state.get(cwd, {}))
    return entry


def set_ticket(state, cwd, ticket_key, branch):
    entry = get_entry(state, cwd)
    entry["ticketKey"] = ticket_key
    entry["branch"] = branch
    entry["pendingLog"] = True
    state[cwd] = entry
    return entry


def set_pending(state, cwd, pending):
    entry = get_entry(state, cwd)
    entry["pendingLog"] = pending
    state[cwd] = entry
    return entry


def set_tracked(state, cwd, tracked):
    entry = get_entry(state, cwd)
    entry["tracked"] = tracked
    state[cwd] = entry
    return entry


def set_last_logged_session(state, cwd, session_id):
    entry = get_entry(state, cwd)
    entry["lastLoggedSessionId"] = session_id
    state[cwd] = entry
    return entry


def clear_ticket(state, cwd):
    entry = get_entry(state, cwd)
    entry["ticketKey"] = None
    entry["branch"] = None
    entry["pendingLog"] = False
    state[cwd] = entry
    return entry


def extract_ticket_keys(text):
    seen = []
    for match in TICKET_KEY_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/.claude/skills/log-work/scripts && python3 -m unittest test_state -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Write failing tests for the CLI and git/session-start behavior**

Append to `~/.claude/skills/log-work/scripts/test_state.py` (inside a new class, above the `if __name__ == "__main__":` line):

```python
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
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `cd ~/.claude/skills/log-work/scripts && python3 -m unittest test_state -v`
Expected: FAIL — `state.py` has no `argparse` CLI or `session-start` command yet (errors like `unrecognized arguments` / non-zero exit).

- [ ] **Step 7: Implement the CLI, git helpers, and session-start command**

Append to `~/.claude/skills/log-work/scripts/state.py` (after `extract_ticket_keys`):

```python
def git_current_branch(cwd):
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_recent_commit_subjects(cwd, count=20):
    result = subprocess.run(
        ["git", "-C", cwd, "log", f"-{count}", "--pretty=%s"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _parse_bool(value):
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def cmd_get(args):
    state = load_state(args.state_path)
    print(json.dumps(get_entry(state, args.cwd)))


def cmd_set_ticket(args):
    state = load_state(args.state_path)
    entry = set_ticket(state, args.cwd, args.ticket_key, args.branch)
    save_state(args.state_path, state)
    print(json.dumps(entry))


def cmd_set_pending(args):
    state = load_state(args.state_path)
    entry = set_pending(state, args.cwd, args.value)
    save_state(args.state_path, state)
    print(json.dumps(entry))


def cmd_set_tracked(args):
    state = load_state(args.state_path)
    entry = set_tracked(state, args.cwd, args.value)
    save_state(args.state_path, state)
    print(json.dumps(entry))


def cmd_set_last_logged_session(args):
    state = load_state(args.state_path)
    entry = set_last_logged_session(state, args.cwd, args.session_id)
    save_state(args.state_path, state)
    print(json.dumps(entry))


def cmd_clear(args):
    state = load_state(args.state_path)
    entry = clear_ticket(state, args.cwd)
    save_state(args.state_path, state)
    print(json.dumps(entry))


def cmd_extract_keys(args):
    text = sys.stdin.read()
    print(json.dumps(extract_ticket_keys(text)))


def cmd_session_start(args):
    payload = json.loads(sys.stdin.read())
    cwd = payload["cwd"]
    state = load_state(args.state_path)
    entry = get_entry(state, cwd)

    branch = git_current_branch(cwd)
    is_repo = branch is not None
    commits = git_recent_commit_subjects(cwd) if is_repo else []
    found_keys = extract_ticket_keys((branch or "") + "\n" + "\n".join(commits))

    lines = [
        f"log-work skill — SessionStart for {cwd}",
        "",
        f'State: tracked={entry["tracked"]}, ticketKey={entry["ticketKey"] or "none"}, '
        f'storedBranch={entry["branch"] or "none"}, pendingLog={entry["pendingLog"]}',
        f"Git: isRepo={is_repo}, currentBranch={branch or 'n/a'}",
        f"Ticket keys found in branch/recent commits: {', '.join(found_keys) or 'none'}",
        "",
        'ACTION REQUIRED: invoke the "log-work" skill now '
        '(Skill tool, skill: "log-work", args: "session-start") to process this '
        "session start before addressing the user's first message.",
    ]
    print("\n".join(lines))


def build_parser():
    parser = argparse.ArgumentParser(description="log-work skill state store")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("get")
    p.add_argument("cwd")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set-ticket")
    p.add_argument("cwd")
    p.add_argument("ticket_key")
    p.add_argument("--branch", default=None)
    p.set_defaults(func=cmd_set_ticket)

    p = sub.add_parser("set-pending")
    p.add_argument("cwd")
    p.add_argument("value", type=_parse_bool)
    p.set_defaults(func=cmd_set_pending)

    p = sub.add_parser("set-tracked")
    p.add_argument("cwd")
    p.add_argument("value", type=_parse_bool)
    p.set_defaults(func=cmd_set_tracked)

    p = sub.add_parser("set-last-logged-session")
    p.add_argument("cwd")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_set_last_logged_session)

    p = sub.add_parser("clear")
    p.add_argument("cwd")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("extract-keys")
    p.set_defaults(func=cmd_extract_keys)

    p = sub.add_parser("session-start")
    p.set_defaults(func=cmd_session_start)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd ~/.claude/skills/log-work/scripts && python3 -m unittest test_state -v`
Expected: all 13 tests PASS.

- [ ] **Step 9: Make the script executable and sanity-check it manually**

Run:
```bash
chmod +x ~/.claude/skills/log-work/scripts/state.py
python3 ~/.claude/skills/log-work/scripts/state.py get "$HOME"
```
Expected: prints `{"ticketKey": null, "branch": null, "lastLoggedSessionId": null, "pendingLog": false, "tracked": true}`.

(No commit — `~/.claude` is not a git repository.)

---

### Task 2: Register the SessionStart hook

**Files:**
- Modify: `~/.claude/settings.json`

**Interfaces:**
- Consumes: `python3 ~/.claude/skills/log-work/scripts/state.py session-start` (Task 1) as the hook command.
- Produces: on every session start (`startup`, `resume`, or `clear`), plain text is injected into the new session's context (Claude Code wraps a `SessionStart` hook's stdout as additional context automatically).

- [ ] **Step 1: Read the current hooks configuration**

Run: `python3 -m json.tool ~/.claude/settings.json`
Confirm the existing `hooks` object (it currently has `PreToolUse`, `PermissionRequest`, `UserPromptSubmit`, `Stop` entries for an unrelated `claude-notify` tool — leave those untouched).

- [ ] **Step 2: Add the SessionStart hook entry**

Using the Edit tool, add a `"SessionStart"` key to the existing `"hooks"` object in `~/.claude/settings.json`, immediately before the `"PreToolUse"` key (or in any position within the same object — order doesn't matter, just don't remove the existing keys):

```json
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/log-work/scripts/state.py session-start",
            "timeout": 15
          }
        ]
      }
    ],
```

The full `hooks` object should read:

```json
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/log-work/scripts/state.py session-start",
            "timeout": 15
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion",
        "hooks": [
          {
            "type": "command",
            "command": "claude-notify question"
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-notify permission"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-notify reset"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-notify reset"
          }
        ]
      }
    ]
  },
```

Deliberately no `matcher` restricting out `compact`/`fork`: leaving those two event sources unmatched means this hook simply won't fire for them (the matcher whitelist only includes `startup|resume|clear`), which is what we want — context compaction and subagent forks aren't new tasks.

- [ ] **Step 3: Validate the JSON is well-formed**

Run: `python3 -m json.tool ~/.claude/settings.json > /dev/null && echo OK`
Expected: prints `OK`.

- [ ] **Step 4: Manually verify the hook command's output**

Run (simulating what Claude Code will send on stdin):
```bash
echo '{"cwd": "'"$HOME"'", "session_id": "manual-test", "transcript_path": "/tmp/t.jsonl"}' | python3 ~/.claude/skills/log-work/scripts/state.py session-start
```
Expected output:
```
log-work skill — SessionStart for /Users/wstuckey

State: tracked=True, ticketKey=none, storedBranch=none, pendingLog=False
Git: isRepo=False, currentBranch=n/a
Ticket keys found in branch name: none
Ticket keys found in recent commits: none

ACTION REQUIRED: invoke the "log-work" skill now (Skill tool, skill: "log-work", args: "session-start") to process this session start before addressing the user's first message.
```
(`isRepo` may read `True` with a real branch/commits if `$HOME` happens to be a git repo — that's fine, it means the git-detection path is exercised too.)

- [ ] **Step 5: Start a fresh Claude Code session and confirm the context actually appears**

Start a new `claude` session in any directory. Confirm a system-reminder resembling `SessionStart hook additional context: log-work skill — SessionStart for <cwd>...` appears (this confirms Claude Code's plain-stdout-injection behavior applies to this hook, not just the ones already configured).

(No commit — `~/.claude` is not a git repository.)

---

### Task 3: Write the `log-work` SKILL.md

> **Post-implementation note:** the embedded content below is the
> *original* draft. Task review and the final whole-plan review found real
> bugs in it (pendingLog ordering, an unfollowable transcript-based
> catch-up instruction, a false opt-out auto-reenable claim, a
> branch/commit key conflation that made the branch fast-path nearly
> unreachable) and fix waves corrected them directly in the shipped file.
> Treat the live `~/.claude/skills/log-work/SKILL.md` as the source of
> truth, not this embedded block.

**Files:**
- Create: `~/.claude/skills/log-work/SKILL.md`

**Interfaces:**
- Consumes: the SessionStart hook's injected text (Task 2) and the `state.py` CLI subcommands (Task 1).
- Produces: the `/log-work` slash command (Claude Code maps a skill directory name directly to a slash command).

- [ ] **Step 1: Write the skill file**

Create `~/.claude/skills/log-work/SKILL.md`:

```markdown
---
name: log-work
description: Match, create, and log work against Jira tickets from Claude Code sessions. Triggered automatically at the start of every session (via a SessionStart hook injecting context) to catch up any unlogged work from the previous session and resolve which ticket the current directory's work belongs to. Also invocable manually as `/log-work` to log the current conversation right now, or to force a rematch if the resolved ticket is wrong. Default Jira project for new tickets is MZCLD.
---

# log-work

## When this runs

1. **Automatically at session start.** A `SessionStart` hook runs
   `state.py session-start` and injects a context block starting with
   `log-work skill — SessionStart for <cwd>`, ending with an
   `ACTION REQUIRED` line. When you see that block, follow the
   **Session-start procedure** below *before* addressing the user's first
   message.
2. **Manually via `/log-work`.** Follow the **Manual procedure** below.
   If invoked with `args: "session-start"` (only the hook does this),
   treat it as case 1.

All `state.py` calls below use:
`python3 ~/.claude/skills/log-work/scripts/state.py <subcommand> ...`

Before calling any Atlassian tool for the first time in a session, load its
schema with `ToolSearch` (`query: "select:mcp__plugin_atlassian_atlassian__<name1>,mcp__plugin_atlassian_atlassian__<name2>,..."`)
— these are deferred tools and calling them without first fetching the
schema will fail.

## Session-start procedure

The injected context block already tells you: `tracked`, `ticketKey`,
`storedBranch`, `pendingLog`, `isRepo`, `currentBranch`, and any ticket keys
found in the branch name / recent commits. Use it instead of re-deriving
this yourself.

1. **Opted out?** If `tracked=False`, do nothing further for this skill —
   don't run any of the steps below, don't mention it unless asked.
2. **Catch up pending work.** If `pendingLog=True`, there's an
   already-resolved `ticketKey` from a previous session that never got
   logged:
   - Read the previous session's transcript (use the `transcript_path`
     from the hook's stdin — if you need it and it's not in the visible
     context, you can re-derive it isn't necessary: the transcript for
     *this* session start is the one to summarize only if this is a
     `resume` continuation; for a fresh `startup`, summarize based on what
     you can infer was done — in practice, prefer summarizing via
     `git log`/`git diff` activity in the directory since `lastLoggedSessionId`
     if it's a git repo, falling back to a short generic note like "Session
     resumed; prior work not fully reconstructable" if there's nothing to
     go on).
   - Post that summary with `addCommentToJiraIssue` on the stored
     `ticketKey`.
   - Run `state.py set-pending <cwd> false`.
3. **Decide continuation.**
   - If `ticketKey` is set, and either `isRepo=False` or
     `currentBranch == storedBranch`, this is a continuation: skip
     matching, keep using `ticketKey`. Go to step 5.
   - Otherwise, run the **Matching procedure** below to resolve a
     `ticketKey`.
4. Once matching resolves a `ticketKey` (found, confirmed, or newly
   created), run `state.py set-ticket <cwd> <ticketKey> --branch <currentBranch-or-empty>`.
5. **Status bump (best-effort).** Call `getJiraIssue` on `ticketKey` and
   check its status's `statusCategory`. If the category is "To Do" (not
   "In Progress" or "Done"), call `getTransitionsForJiraIssue` and, if
   exactly one available transition's target status has category
   "In Progress", call `transitionJiraIssue` to apply it. If there are zero
   or multiple such candidates, skip silently — don't ask the user, don't
   report failure.
6. Don't announce any of this verbosely to the user — a brief one-line
   mention (e.g. "Logged last session to MZCLD-42, continuing on it") is
   enough. If nothing needed doing (already a continuation, nothing
   pending), say nothing at all.

## Matching procedure

Only runs when session-start step 3 or the manual "force rematch" flow
needs a ticket resolved.

1. **Git branch/commit key scan** (git repos only — skip entirely for
   non-git directories): the hook context already lists any keys found in
   the branch name and recent commits. If exactly one distinct key is
   found, call `getJiraIssue` to verify it resolves. If it does, that's
   the ticket — done.
2. **JQL keyword search** (non-git directories, or git repos where step 1
   found nothing or multiple different keys): derive 2-4 keywords from the
   directory's basename and whatever the user has said about the task so
   far this session (check the directory for a `README.md` title line
   too, if present). Call `searchJiraIssuesUsingJql` with:
   `project = MZCLD AND assignee = currentUser() AND statusCategory != Done AND text ~ "<keywords>"`
   If that returns nothing, retry with just
   `project = MZCLD AND assignee = currentUser() AND statusCategory != Done`
   and rank by textual similarity yourself.
3. **Disambiguate with the user.** Present whatever candidates you found
   (ticket key + summary each), or say none were found, and ask the user
   to pick one or say "none of these — create a new one." Use
   `AskUserQuestion` when there are 2-4 concrete candidates; otherwise ask
   in plain chat.
4. **Create new ticket** only if the user explicitly declines all
   candidates or asks for a new one. Draft a summary and description from
   the session's context, propose them to the user, and only after they
   confirm, call `createJiraIssue` with `project: "MZCLD"`, `issueType: "Task"`.
   Then resolve the current user's account id (`atlassianUserInfo`, or
   `lookupJiraAccountId` if you need to look up by email) and assign the
   new ticket to them via `editJiraIssue`.

## Manual procedure (`/log-work`)

Default behavior (no special instruction from the user in the same
message):

1. Summarize what's happened in the current conversation so far (what was
   worked on, key decisions, outcome so far).
2. If `state.py get <cwd>` shows a `ticketKey`, post the summary as a
   comment there via `addCommentToJiraIssue`, then run
   `state.py set-pending <cwd> false`.
3. If there's no `ticketKey` yet, run the **Matching procedure** first,
   then post the summary per step 2 and run
   `state.py set-ticket <cwd> <ticketKey> --branch <currentBranch-or-empty>`.

If the user instead says the resolved ticket is wrong (e.g. "wrong
ticket", "that's not it"):

1. Run `state.py clear <cwd>`.
2. Run the **Matching procedure** to resolve a new `ticketKey`.
3. Run `state.py set-ticket <cwd> <ticketKey> --branch <currentBranch-or-empty>`.

## Opt-out

If the user says something like "don't log this to Jira" / "don't track
this directory": run `state.py set-tracked <cwd> false` and confirm you've
turned off tracking for this directory. Re-enabling happens automatically
the next time `/log-work` runs (it doesn't check `tracked`), or by running
`state.py set-tracked <cwd> true` directly.

## Error handling

If any Atlassian MCP call fails (auth, network, not found), tell the user
briefly what failed and continue the session normally — never block the
user's actual request on a Jira error.
```

- [ ] **Step 2: Verify the frontmatter is well-formed**

Run: `python3 -c "import yaml, sys; d=open('$HOME/.claude/skills/log-work/SKILL.md').read().split('---')[1]; print(yaml.safe_load(d))"`

Expected: prints a dict with `name: log-work` and a non-empty `description` — no exception. (If `pyyaml` isn't installed, instead visually confirm the frontmatter block starts and ends with `---` on its own line and has `name:`/`description:` keys, matching the format used by `~/.claude/skills/okr-update/SKILL.md`.)

- [ ] **Step 3: Confirm the skill is discoverable**

Start a new Claude Code session and check that `log-work` appears in the available-skills listing (the same mechanism that surfaces `okr-update`, `generate-alert-runbooks`, etc.), and that typing `/log-work` invokes it.

(No commit — `~/.claude` is not a git repository.)

---

### Task 5: Branch-per-ticket support

> **Added after initial ship**, per user request: whenever `log-work`
> resolves a ticket and the current branch doesn't already reflect it,
> create/check out `<user>/<ticketKey>-<ctx>` before proceeding. See
> `~/.claude/jobs/82877e45/tmp/sdd-log-work/task-5-brief.md` for the full
> task text (state.py additions, tests, and the SKILL.md edit) — that file
> is this task's source of truth and mirrors the process used for Tasks
> 1-3. DESIGN.md's Matching section and Testing scenarios 10-11 already
> reflect this behavior.

**Files:**
- Modify: `~/.claude/skills/log-work/scripts/state.py`
- Modify: `~/.claude/skills/log-work/scripts/test_state.py`
- Modify: `~/.claude/skills/log-work/SKILL.md`

**Interfaces:**
- Produces: `state.py git-username <cwd>` (prints the local part of `git
  config user.email`, or a slugified `git config user.name`, or empty),
  and `state.py slugify` (reads text from stdin, prints a kebab-case slug,
  first 5 words / 40 chars max) — both consumed by SKILL.md's Matching
  procedure step 5.

(No commit — `~/.claude` is not a git repository.)

---

### Task 6: Manual end-to-end verification

**Files:** none (verification only, against the real Jira instance — use a scratch/low-stakes directory, not a real feature branch, for the creation scenarios).

- [ ] **Step 1: Initial branch-key match**

In a git repo whose current branch name contains a real ticket key you're assigned (e.g. `feature/MZCLD-123-foo`), start a session. Confirm: SessionStart context appears, the skill resolves `MZCLD-123` without asking you anything, and `state.py get <cwd>` shows that `ticketKey` with `pendingLog=true`.

- [ ] **Step 2: Manual log-now**

In that same session, run `/log-work`. Confirm a new comment appears on `MZCLD-123` in Jira summarizing the conversation, and `state.py get <cwd>` now shows `pendingLog=false`.

- [ ] **Step 3: True continuation (no rematch)**

Make a bit more conversation/progress in the same session, then start a **new** session in the same directory without changing branch. Confirm the skill treats this as a continuation — no JQL search, no candidate prompt, `state.py get <cwd>` still shows `ticketKey=MZCLD-123` — and that it still ran the `pendingLog` catch-up step first (posting a comment for the step-2-to-now activity) since you didn't run `/log-work` again before ending that session.

- [ ] **Step 4: Retroactive catch-up after an abrupt end**

Make further conversation/progress, then end the session by closing the terminal (not via `/log-work`). Start a new session in the same directory. Confirm the skill notices `pendingLog=true` and posts a catch-up comment before resolving continuation, exactly as in Step 3 but simulating an abrupt exit rather than a clean stop.

- [ ] **Step 5: No-key JQL matching**

In a git repo/branch with no ticket key in the name or recent commits, start a session, and confirm the skill runs a JQL search and asks you to confirm/decline candidates rather than guessing silently.

- [ ] **Step 6: Ticket creation gate**

Decline all candidates in step 5 (or use a directory with no plausible existing ticket). Confirm the skill drafts a summary/description and **waits for your explicit yes** before calling `createJiraIssue`, and that the created ticket is assigned to you.

- [ ] **Step 7: Non-git directory**

Repeat step 5 in a non-git directory (e.g. this `crossy-kit` directory, which isn't a git repo). Confirm the skill skips the branch/commit scan entirely and goes straight to JQL search/ask.

- [ ] **Step 8: Opt-out**

Say "don't track this directory" in some directory. Confirm `state.py get <cwd>` shows `tracked=false`, and that a new session started in that directory produces no SessionStart action (only the raw hook output, no ticket resolution).

- [ ] **Step 9: Status bump no-op**

Repeat step 1 against a ticket that's already "In Progress." Confirm the skill does not attempt a transition (no visible Jira status change, no error).

- [ ] **Step 10: Branch creation on match/create**

Start a session on an unrelated branch (e.g. `main`) in a repo where step 5's JQL search will match or step 6's creation flow will fire. Confirm the skill checks out a new `<your-git-username>/<ticketKey>-<slug>` branch before proceeding, and that `state.py get <cwd>` shows `branch` set to that new branch name.
