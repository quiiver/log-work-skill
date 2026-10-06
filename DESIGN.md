# log-work — Work Tracking Skill Design

Date: 2026-08-20 (v1, Jira only) · Revised: 2026-10-06 (v2, Confluence work log, backfill, reports)
Status: Implemented

## Purpose

Keep a complete record of the work done in Claude Code sessions without
having to remember to do it:

- find (or create) the right Jira ticket for whatever you're working on, and
  log a summary of what happened as a comment;
- keep a private monthly Confluence work log, so work with no ticket is
  still recorded, and any "past N" period can be reported from one place;
- rebuild that log from history and turn it into period reports (e.g. a
  quarterly review).

## Scope

- Personal skill named `log-work`, lives in `~/.claude/skills/log-work/`
  (a git checkout of `quiiver/log-work-skill`), invocable as `/log-work`.
  Not tied to any one repo.
- Jira project for new tickets, the Atlassian site and all Confluence/GitHub
  IDs are per-user config, resolved on first use (see Config). Nothing is
  hardcoded.
- Work log lives in the user's Confluence personal space, under a
  view-restricted `Work Log` folder.
- Not in scope: time tracking / formal Jira worklogs, multi-step status
  workflows beyond an initial To Do → In Progress bump, cross-repo session
  merging, automatic publishing of reports to anyone else.

## Architecture

Two integration points, one shared state store, two output stores:

1. **SessionStart hook** — `state.py session-start` runs at every
   startup/resume/clear and injects a context block (state + git facts).
   That drives the catch-up and match-or-continue flow.
2. **Manual `/log-work`** — log now, force a rematch, `backfill`, or `report`.
3. **State file** `~/.claude/log-work-state.json`, keyed by absolute `cwd`:
   ```json
   {
     "/Users/me/dev/some-repo": {
       "ticketKey": "MZCLD-456",
       "branch": "me/MZCLD-456-some-task",
       "lastLoggedSessionId": null,
       "pendingLog": false,
       "tracked": true
     }
   }
   ```
   - `tracked: false` = opted out.
   - `ticketKey: "WORKLOG"` is a sentinel: this directory's work has no
     ticket, so log to Confluence only.
4. **Jira comments** on the resolved ticket (detail).
5. **Confluence work log**: `Work Log/Work Log — YYYY-MM` pages, one per
   month, plus `Work Log/Reports/` for generated reports. All content is
   created with `private: true`, and its restriction state is checked
   afterwards.

### Config

Per-user settings are stored in the same state file under the reserved key
`__config__`. Real entries are keyed by absolute paths, so the two can't
collide. `state.py config` reads them, and `config <key> <value>` sets one.
SessionStart prints them, so the skill knows what's missing without an
extra call.

| Key | Resolved from |
|---|---|
| `jiraProject` | asked once |
| `cloudId` | `getAccessibleAtlassianResources` (asks if there are several sites) |
| `accountId` | `atlassianUserInfo` |
| `confluenceSpaceKey`, `confluenceSpaceId` | `getConfluencePersonalSpace` |
| `workLogFolderId`, `reportsFolderId` | CQL search by title, else created as private |
| `githubLogin` | `gh api user` |

Keys are resolved lazily, on the first procedure that needs them. A
not-found error on a cached ID clears that key and resolves it again once.

> **No Stop/SessionEnd hook.** `Stop` fires after *every* assistant turn,
> and `SessionEnd` gets a 1.5–60s budget with no documented MCP access, so
> neither can reliably summarize and call Jira or Confluence. Instead,
> `pendingLog` is set whenever a session resolves a ticket (or `WORKLOG`).
> It is cleared only by an actual logging action: a SessionStart catch-up
> or `/log-work`.

## Flow

### SessionStart

1. If `tracked` is `false`, stop.
2. If `pendingLog` is `true`, write an honest summary of the previous
   session from what's actually visible (resumed conversation, or
   `git log`/`git status`). Never invent specifics. **Log the summary**
   (below), then clear `pendingLog`.
3. Decide continuation. If `ticketKey` is set and either the directory is
   non-git or the current branch matches the stored `branch`, it's the same
   task: skip matching and re-arm `pendingLog`. Otherwise, run Matching.
4. Persist the resolved `ticketKey` and branch.
5. Status bump (real tickets only, best-effort): if the ticket is in a "To
   Do" status category and exactly one transition leads to "In Progress",
   apply it; otherwise skip silently.
6. Stay quiet — say one line at most, and nothing if nothing happened.

### Logging a summary

- **Real ticket:** post a Jira comment with the full summary, **and** add a
  short pointer entry to this month's work log page (heading with the
  ticket key, category, one-line outcome, links).
- **`WORKLOG`:** add a full entry to this month's work log page.

Every session lands in the work log, ticketed or not. That way a report is
one read of a few monthly pages, rather than crawling every ticket's
comments, which JQL can't filter by comment author or date.

Entry format: `### YYYY-MM-DD — <title>[ — KEY]`, then Category (support,
incident, tooling, docs, review, mentoring, investigation, meeting, other),
What / outcome, Impact / who benefited, Effort (S/M/L), Links, and Status
(done / ongoing / handed off / abandoned). Pages are sorted newest first.

### Matching (only when not a continuation)

1. **Branch key** (git only): if the branch name has exactly one Jira key
   and it resolves, use it. Commit-message keys never override the branch.
2. **JQL search**:
   `project = <jiraProject> AND assignee = currentUser() AND statusCategory != Done`,
   plus keywords from the directory name, README and the task at hand. If
   nothing matches, drop the keywords and rank the results yourself.
3. **Ask the user** (`AskUserQuestion`): up to 2 candidates, plus these two
   options, always present: **Create a new <jiraProject> ticket** and **Log to
   Confluence work log only**. Any extra candidates go in the question text.
4. **Create a ticket** only if the user picks that option. Draft the summary
   and description, wait for explicit confirmation, create the ticket as a
   Task in `<jiraProject>`, and assign it to the user.
5. **Ticket branch** (git only, not for `WORKLOG`): if the branch doesn't
   already contain the key, check out `<user>/<KEY>-<slug>`. Fall back to an
   existing branch of that name, or to staying on the current branch. Never
   force a destructive checkout.

Ticket creation is the only step that always needs confirmation, because it
is visible to the team. Choosing the work log is the user's call in step 3.
Branch creation is automatic and reversible.

### Backfill (`/log-work backfill [since]`, default 90 days)

All sources are read-only; the only writes go to the work log.

| Source | How |
|---|---|
| Jira | Candidates come from `getMyWork` (worked_on) plus JQL on assignee, reporter, watcher or past assignee. `listJiraIssueComments` per ticket, filtered to the user's accountId and the window. Parallel subagents above ~30 tickets. |
| GitHub | `gh search prs --author=@me --created=">=<since>"`. This is the authority on merged/closed status and on which repo a PR is in. |
| claude-mem | `~/.claude-mem/claude-mem.db`, table `session_summaries`, exported per month. Each month goes to one subagent, together with that month's PRs and existing entries. |

Merge rules:
- Group into work items (a theme over a few days), not one entry per
  session or PR.
- Never duplicate an existing entry; add PR links to it instead.
- Date entries by when the work finished.
- Skip trivial sessions.
- Never invent details.
- Hold back anything that looks like a possible personal project until the
  user confirms it.

### Report (`/log-work report <period>`)

1. Read the monthly pages for the period. If the log starts mid-period,
   fill the gap from PRs and session titles.
2. Add GitHub counts:
   - authored PRs, by state and repo;
   - reviews, excluding the user's own login (`githubLogin`) and bots. This is
     approximate: the search matches PRs updated in the window, not
     reviewed in it.
3. Sections: Summary, By the numbers, Themes, Incidents, Reviews and
   collaboration, Key decisions, Carried forward, Coverage notes.
4. Publish privately under `Work Log/Reports`. Sharing is left to the user.

### Manual `/log-work`

- **No arguments:** summarize the conversation so far and log the summary
  (resolving a ticket first if needed), then clear `pendingLog`.
- **"Wrong ticket":** clear the state for `cwd` and re-run Matching.
- **`backfill` / `report`:** run the procedures above.

## Opt-out

"Don't log this" / "don't track this directory" sets `tracked: false`. It
stays off until the user explicitly asks to resume, or runs
`state.py set-tracked <cwd> true`.

## Error handling

- A failure in Jira or Confluence is reported in one line and never blocks
  the user's actual request.
- If only one half of a dual write succeeds (Jira comment + work log
  entry), `pendingLog` is still cleared, and the skill says which half
  failed.
- Status-bump failures or ambiguity are skipped silently.

## Testing

- `scripts/test_state.py` holds 18 stdlib `unittest` tests: state CRUD,
  ticket-key extraction, slugify, git username and session-start output.
  Run with `cd scripts && python3 -m unittest test_state`.
- Manual scenarios (skill + hooks + MCP, not unit-testable):
  1. The branch contains a valid key → matched with no questions.
  2. No key in the branch → JQL search, then the three-way question.
  3. "Create a new ticket" → drafted ticket, confirmed, then created
     and assigned.
  4. Same directory and branch in a new session → continuation, no rematch.
  5. Non-git directory → no key scan; goes straight to search and the
     question.
  6. Session ends without `/log-work` → the next SessionStart logs it first.
  7. Opted-out directory → SessionStart does nothing.
  8. Ticket already In Progress → no transition.
  9. `/log-work` mid-session → logs now and clears `pendingLog`.
  10. Matched while on `main` → checks out the `<user>/<KEY>-<slug>` branch.
  11. Already on the ticket branch → no new branch.
  12. "Log to Confluence work log only" → `WORKLOG` saved; no Jira calls,
      branch or status bump; a full entry lands on this month's page.
  13. Ticketed log → a Jira comment **and** a short pointer entry.
  14. First log of a new month → the month page is created as private, and
      `getConfluenceContentRestrictionState` returns `VIEW_RESTRICTED`.
  15. `backfill 90` → per-month pages with no duplicates, personal projects
      held back, and dates and PR states that match GitHub.
  16. `report Q3 2026` → a private page under Reports, with coverage notes.
