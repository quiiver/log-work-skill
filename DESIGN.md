# log-work — Work Tracking Skill Design

Date: 2026-08-20 (v1, Jira only) · Revised: 2026-10-06 (v2, Confluence work log, backfill, reports) · 2026-11-05 (v3, work item per session, asked at start)
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
  workflows, ticket branches, cross-repo session merging, automatic publishing of reports to anyone else.

## Architecture

Two integration points, one shared state store, two output stores:

1. **SessionStart hook** — `state.py session-start` runs at every
   startup/resume/clear. A new session gets an `ACTION REQUIRED` block that
   has the skill ask which work item it is for; a resumed or compacted one
   already has an item and is left alone.
2. **Manual `/log-work`** — log now, change the item, `backfill`, or `report`.
3. **Session ledger** `~/.claude/log-work-sessions.jsonl` holds each
   session's work item (`set-item` rows; newest wins). The work item
   belongs to the session: nothing is inherited from the directory or the
   branch, which is what made v2's per-directory ticket hard to reason
   about (resumed sessions and stale branches picked up the wrong ticket).
   The state file `~/.claude/log-work-state.json` keeps only per-directory
   opt-outs (`{"/path": {"tracked": false}}`) and config. Unticketed work
   gets its own `WORKLOG:<date>-<hex>` id per item.
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
> neither can reliably summarize and call Jira or Confluence. Sessions are
> logged with `/log-work`; unlogged ones are caught by backfill. (v2 caught
> them up at the next session start, which tied sessions to directories.)

## Flow

### SessionStart

1. Tracking off, or the session already has an item: stop, say nothing.
2. Suggest up to 2 items: the one ticket key in the branch name, one JQL
   search on the first message's keywords, and recent items from the ledger.
3. Ask (`AskUserQuestion`): the suggestions, **New ticket**, **Work log only**.
4. `state.py set-item <session> <KEY|WORKLOG>`; a new ticket is drafted,
   confirmed, created and assigned first.

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

### Backfill (`/log-work backfill [since]`, default 90 days)

All sources are read-only; the only writes go to the work log.

| Source | How |
|---|---|
| Jira | Candidates come from `getMyWork` (worked_on) plus JQL on assignee, reporter, watcher or past assignee. `listJiraIssueComments` per ticket, filtered to the user's accountId and the window. Parallel subagents above ~30 tickets. |
| GitHub | `gh search prs --author=@me --created=">=<since>"`. This is the authority on merged/closed status and on which repo a PR is in. |
| claude-mem | `~/.claude-mem/claude-mem.db`, table `session_summaries`, exported per month. Each month goes to one subagent, together with that month's PRs and existing entries. |
| Zoom + Calendar | Zoom search, keeping meetings where the user's role is `attendee`. Get-meeting-assets **by UUID** (a numeric ID returns only the latest instance of a recurring meeting) and keep only `my_notes.content_markdown`; the search's summary flags are unreliable. The calendar adds conferences, in-person events and talks. Only work outcomes and decisions are taken from notes, never feedback about people, personnel or personal matters. |

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
  (asking for the work item first if the session has none).
- **"Wrong ticket":** `set-item` again; the newest item wins.
- **`backfill` / `report`:** run the procedures above.

## Opt-out

"Don't log this" / "don't track this directory" sets `tracked: false`. It
stays off until the user explicitly asks to resume, or runs
`state.py set-tracked <cwd> true`.

## Error handling

- A failure in Jira or Confluence is reported in one line and never blocks
  the user's actual request.
- If only one half of a dual write succeeds (Jira comment + work log
  entry), the skill says which half failed.

## Testing

- `scripts/test_*.py` are stdlib `unittest` tests: run
  `python3 -m unittest discover -s scripts`.
- Manual scenarios (skill + hooks + MCP, not unit-testable):
  1. New session on a branch with one ticket key → that key is the first suggestion.
  2. New session, non-git directory → JQL suggestions from the first message.
  3. "New ticket" → drafted, confirmed, created, assigned, set.
  4. "Work log only" → a new `WORKLOG:` id; no Jira calls; a full work log entry when logged.
  5. Resumed session → keeps its item, no question.
  6. Second new session in the same directory → asked again (recent items offered).
  7. Opted-out directory → SessionStart does nothing.
  8. `/log-work` with no item set → asks, then logs.
  9. Ticketed log → a Jira comment **and** a short pointer entry.
  10. First log of a new month → the page is created private (`VIEW_RESTRICTED`).
  11. `backfill 90` / `report Q3 2026` → as above.
