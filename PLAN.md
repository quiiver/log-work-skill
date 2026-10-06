# log-work Implementation Record

What was built, in order, and what is still unverified. The original
task-by-task plan (with embedded draft code) is in git history. The live
files are the source of truth: [SKILL.md](SKILL.md),
[scripts/state.py](scripts/state.py), [scripts/test_state.py](scripts/test_state.py).
Design: [DESIGN.md](DESIGN.md).

## Constraints

- No hardcoded project or IDs: `jiraProject`, `cloudId`, Confluence and
  GitHub IDs are per-user config (`state.py config`). Ticket creation
  always needs explicit user confirmation.
- Logging uses Jira comments plus Confluence work log entries. It never
  uses `addWorklogToJiraIssue`, because there's no time tracking.
- There is no Stop/SessionEnd hook. All "log the last session" work runs at
  SessionStart catch-up (via `pendingLog`) or on `/log-work`.
- `state.py` uses the Python 3 stdlib only. The state file is
  `~/.claude/log-work-state.json`, keyed by absolute `cwd`.
- Atlassian MCP tools are deferred. Load them with
  `ToolSearch select:<names>` before the first call.
- Everything written to Confluence is created `private: true`, and its
  restriction state is checked afterwards.

## Done

| # | Date | Change | Files |
|---|---|---|---|
| 1 | 2026-08-20 | State store module + CLI (`get`, `set-ticket`, `set-pending`, `set-tracked`, `clear`, `extract-keys`, `session-start`) with unit tests | `scripts/state.py`, `scripts/test_state.py` |
| 2 | 2026-08-20 | Registered the SessionStart hook (`startup\|resume\|clear` → `state.py session-start`, 15s timeout) | `~/.claude/settings.json` |
| 3 | 2026-08-20 | Wrote SKILL.md. Review fixed pendingLog ordering, removed the unfollowable transcript-reading catch-up, fixed the opt-out auto-re-enable claim, and stopped commit keys from overriding branch keys | `SKILL.md` |
| 4 | 2026-08-20 | Branch per ticket: `state.py git-username` and `slugify`; checks out `<user>/<KEY>-<slug>` after matching | `scripts/state.py`, `scripts/test_state.py`, `SKILL.md` |
| 5 | 2026-10-05 | Confluence work log: the `WORKLOG` sentinel; a three-way question when no ticket matches (candidate / create ticket / work log only); every logged summary also goes to the monthly page; tolerates one half of the dual write failing | `SKILL.md` |
| 6 | 2026-10-05 | Created the private `Work Log` folder and the 2026-07 to 2026-10 pages, backfilled from Jira comments, GitHub PRs and claude-mem | Confluence |
| 7 | 2026-10-06 | Wrote down the backfill and report procedures (`/log-work backfill`, `/log-work report`), known IDs, the markdown read/write approach and the privacy check | `SKILL.md` |
| 8 | 2026-10-06 | Published as `quiiver/log-work-skill` (private), with a README and install steps | `README.md`, `.gitignore` |
| 9 | 2026-10-06 | Made the skill generic: added a `state.py config` store (the `__config__` key) and lazy resolution of every ID; SessionStart prints the config; removed the hardcoded IDs, MZCLD and the GitHub login from SKILL.md | `scripts/state.py`, `scripts/test_state.py`, `SKILL.md` |
| 10 | 2026-10-06 | Install for other harnesses: `session-start --format` for Gemini/Cursor/Copilot JSON output; falls back to the process cwd when stdin has no `cwd`; README covers Claude Code surfaces, Codex, Gemini, Cursor, Copilot and opencode | `scripts/state.py`, `README.md` |

`WORKLOG` is stored as an ordinary `ticketKey` string, and `extract-keys`
never matches it.

## Verification

Unit tests: `cd scripts && python3 -m unittest test_state`. 20 tests, all
passing as of 2026-10-06.

Manual scenarios (numbers match DESIGN.md → Testing). Only results seen
in a real session are checked off:

- [ ] 1 Branch key match with no questions
- [x] 2 No key → JQL search → ask (non-git, 2026-10-05; nothing matched)
- [ ] 3 Create-ticket path: drafted, confirmed, then created and assigned
- [x] 4 Continuation in the same directory (non-git `WORKLOG`, 2026-10-06 resume)
- [x] 5 Non-git directory skips the key scan
- [x] 6 Missed log caught up at the next SessionStart (2026-10-06, `WORKLOG` → October page)
- [ ] 7 Opted-out directory does nothing
- [ ] 8 Ticket already In Progress → no transition
- [ ] 9 `/log-work` mid-session clears `pendingLog`
- [ ] 10 Matched on `main` → ticket branch created
- [ ] 11 Already on the ticket branch → no new branch
- [x] 12 `WORKLOG` path: no Jira calls, full entry on the month page
- [ ] 13 Ticketed log writes both the Jira comment and the pointer entry
- [x] 14 New month page is private (`VIEW_RESTRICTED` confirmed)
- [x] 15 Backfill: no duplicates, personal projects held back, PR states fixed against GitHub
- [x] 16 Report: private page under Reports, with coverage notes (Q3 2026)

## Open

- Category labels on monthly pages: specified in SKILL.md, but not yet
  applied to the backfilled pages.
- Backfill misses SREIN tickets you commented on but don't watch.
  Comment search by author would need a different API.
- Reviews in reports are approximate (they match on `updated`, not on
  review date).
- Install for other harnesses (plugin packaging, cloud sessions, other
  agent CLIs): see the Install section of README.md.
