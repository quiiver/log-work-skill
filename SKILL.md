---
name: log-work
description: Match, create, and log work against Jira tickets from Claude Code sessions, plus a private monthly Confluence work log for unticketed work. Also backfills the log from Jira comments, GitHub PRs and claude-mem history (`/log-work backfill`) and writes period reports such as quarterly reviews (`/log-work report Q3 2026`). Triggered automatically at the start of every session (via a SessionStart hook injecting context) to catch up any unlogged work from the previous session and resolve which ticket the current directory's work belongs to. Also invocable manually as `/log-work` to log the current conversation right now, or to force a rematch if the resolved ticket is wrong. The Jira project for new tickets is set on first run.
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

## Config

Per-user settings live in the state file under `__config__`. The
SessionStart context prints them (`Config: {...}` or `Config: none`);
otherwise read them with `state.py config`, set one with
`state.py config <key> <value>`, and unset one with `state.py config <key>`.
Below, `<jiraProject>`, `<cloudId>` etc. mean the configured value.

Resolve a missing key the first time a procedure needs it, save it, and
never ask again:

| Key | How to resolve |
|---|---|
| `jiraProject` | Ask the user once: "Which Jira project should new tickets go in?" |
| `cloudId` | `getAccessibleAtlassianResources`. If there are several sites, ask which one. |
| `accountId` | `atlassianUserInfo` |
| `confluenceSpaceKey`, `confluenceSpaceId` | `executeRead` `getConfluencePersonalSpace` (`personalSpace.key` / `.id`) |
| `workLogFolderId` | CQL `space = "<confluenceSpaceKey>" AND type = folder AND title = "Work Log"`, else create it (private, space root) |
| `reportsFolderId` | Same as above, but titled `Reports` and under `workLogFolderId` |
| `githubLogin` | `gh api user --jq .login` |

If a call using a cached ID fails with not-found, unset that key, resolve
it again, and retry once.

## Work log (Confluence)

A private monthly log in the user's personal Confluence space. Every logged
session lands here — ticketed or not — so a "past N" report is one read of a
few monthly pages instead of crawling Jira comments.

- **Location:** personal space (find via `executeRead` `getConfluencePersonalSpace`),
  folder titled `Work Log`, one page per month titled `Work Log — YYYY-MM`,
  and a `Reports` subfolder for generated reports.
- **IDs come from config** (see **Config** below): `workLogFolderId`,
  `reportsFolderId`, `confluenceSpaceKey` / `confluenceSpaceId`.
- **Find or create:** CQL `space = "<personalSpaceKey>" AND title = "<title>"`.
  If the folder is missing, `createConfluenceContent` with
  `contentType: "folder"`, `private: true` at the space root. If the month
  page is missing, create it under the folder (`parentContentId`) with
  `private: true` too (belt and braces; the folder restriction inherits).
- **Sentinel:** `ticketKey = WORKLOG` in state means "this directory's work
  has no ticket; log to Confluence only". Skip Jira calls, status bumps and
  branch creation for it.
- **Appending:** `getConfluenceContent` with `detail: "full"`,
  `content_format: "markdown"` (the pages are plain markdown), then
  `updateConfluenceContent` with the full body, the new entry inserted
  directly under the intro paragraph (newest first, sorted by date), and
  the returned `snapshotToken`. Unescape `\_` / `\~` from the read before
  writing back. Add the entry's category as a page label if it isn't
  already there.
- **Privacy check:** after creating any folder or page, confirm with
  `executeRead` `getConfluenceContentRestrictionState` that it is
  `VIEW_RESTRICTED`. Never publish to a non-private location without the
  user asking.
- **Entry format** (one `<h3>` per entry):
  - Heading: `YYYY-MM-DD — <short title>` (append ` — <TICKET-KEY>` for ticketed work)
  - **Category:** one of support, incident, tooling, docs, review, mentoring, investigation, meeting, other
  - **What / outcome:** 1–3 lines; lead with the outcome
  - **Impact / who benefited:** team, tenant or `app_code`, if known
  - **Effort:** S / M / L
  - **Links:** PRs, Slack threads, Confluence pages, ticket URL
  - **Status:** done / ongoing / handed off / abandoned
  - For ticketed work, keep it to heading + Category + one-line outcome +
    ticket link; the detail lives in the Jira comment.

### Logging a summary

Used wherever a procedure below says "log the summary":

- `ticketKey` is a real Jira key → `addCommentToJiraIssue` with the summary,
  **and** append a short ticketed entry to this month's work log page.
- `ticketKey = WORKLOG` → append a full entry to this month's work log page.

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
   - Write a short, honest summary of what happened in that previous
     session using only what you actually have visibility into — recent
     conversation history if this session was resumed/continued, or
     `git log`/`git status` activity in the directory if it's a git repo.
     Don't invent specifics you can't support; if there's nothing concrete
     to go on, use a brief generic note like "Session resumed; prior work
     not fully reconstructable."
   - Log that summary (see **Logging a summary**) against the stored
     `ticketKey`.
   - Run `state.py set-pending <cwd> false`.
3. **Decide continuation.**
   - If `ticketKey` is set, and either `isRepo=False` or
     `currentBranch == storedBranch`, this is a continuation: skip
     matching, keep using `ticketKey`, and run
     `state.py set-pending <cwd> true` (this session's work needs to be
     caught up next time, same as freshly-matched work would). Go to
     step 5.
   - Otherwise, run the **Matching procedure** below to resolve a
     `ticketKey`.
4. Once matching resolves a `ticketKey` (found, confirmed, or newly
   created), run `state.py set-ticket <cwd> <ticketKey> --branch <currentBranch-or-empty>`
   (pass `--branch` with the current git branch name for git repos; omit
   `--branch` entirely for non-git directories — same rule as the manual
   procedure uses).
5. **Status bump (best-effort; skip for `WORKLOG`).** Call `getJiraIssue` on `ticketKey` and
   check its status's `statusCategory`. If the category is "To Do" (not
   "In Progress" or "Done"), call `getTransitionsForJiraIssue` and, if
   exactly one available transition's target status has category
   "In Progress", call `transitionJiraIssue` to apply it. If there are zero
   or multiple such candidates, skip silently — don't ask the user, don't
   report failure.
6. Don't announce any of this verbosely to the user — a brief one-line
   mention (e.g. "Logged last session to PROJ-42, continuing on it") is
   enough. If nothing needed doing (already a continuation, nothing
   pending), say nothing at all.

## Matching procedure

Only runs when session-start step 3, the manual procedure's default
flow (when there's no `ticketKey` yet), or the manual "force rematch"
flow needs a ticket resolved.

1. **Git branch key check** (git repos only — skip entirely for non-git
   directories): the hook context lists ticket keys found in the branch
   name separately from keys found in recent commits. If the branch name
   contains exactly one ticket key, call `getJiraIssue` to verify it
   resolves — that's the strongest signal, and it doesn't need to agree
   with commit messages. If it does, that's the ticket — done. If the
   branch has zero or more than one key, fall through to step 2.
2. **JQL keyword search** (non-git directories, or git repos where step 1
   found nothing or multiple different keys): derive 2-4 keywords from the
   directory's basename and whatever the user has said about the task so
   far this session (check the directory for a `README.md` title line
   too, if present). Call `searchJiraIssuesUsingJql` with:
   `project = <jiraProject> AND assignee = currentUser() AND statusCategory != Done AND text ~ "<keywords>"`
   If that returns nothing, retry with just
   `project = <jiraProject> AND assignee = currentUser() AND statusCategory != Done`
   and rank by textual similarity yourself.
3. **Disambiguate with the user.** Use `AskUserQuestion` with the
   candidates you found (ticket key + summary each, up to 2) plus these two
   options, always present:
   - **Create a new <jiraProject> ticket** → step 4.
   - **Log to Confluence work log only** → `ticketKey = WORKLOG`; return to
     the calling step (skip step 5).
   If more than 2 plausible candidates exist, list the extras in the
   question text so the user can pick one via "Other".
4. **Create new ticket** only if the user picks that option. Draft a summary and description from
   the session's context, propose them to the user, and only after they
   confirm, call `createJiraIssue` with `project: "<jiraProject>"`, `issueType: "Task"`.
   Then resolve the current user's account id (`atlassianUserInfo`, or
   `lookupJiraAccountId` if you need to look up by email) and assign the
   new ticket to them via `editJiraIssue`.
5. **Ensure a ticket branch** (git repos only — skip entirely for
   non-git directories and for `WORKLOG`). Determine whether the current branch already
   represents `ticketKey`: run `git -C <cwd> rev-parse --abbrev-ref HEAD`
   to get the current branch name, pipe it into `state.py extract-keys`
   to get the exact ticket keys it contains, and check whether
   `ticketKey` is exactly one of them — don't use a plain substring check
   (`PROJ-9` is a substring of `PROJ-90`, which would wrongly treat two
   different tickets' branches as the same). If `ticketKey` isn't among
   the extracted keys (true whenever the match came from step 2, 3, or 4
   — step 1 by definition already satisfies this), create and switch to
   a new branch before returning to whichever step called this
   procedure. From this point forward, "the current branch" means the
   new branch:
   - Get `<user>`: run `state.py git-username <cwd>`. If it prints nothing
     (no git identity configured), ask the user what to use instead of
     guessing.
   - Get `<ctx>`: if you don't already have the ticket's summary, fetch it
     via `getJiraIssue`, then pipe it into `state.py slugify` (reads text
     from stdin, prints a short kebab-case slug) to get `<ctx>`.
   - Run `git -C <cwd> checkout -b <user>/<ticketKey>-<ctx>`.
   - If that fails (name collision, uncommitted changes that would be
     overwritten), try `git -C <cwd> checkout <user>/<ticketKey>-<ctx>` to
     switch to an existing branch of that name instead. If that also
     fails, tell the user briefly and continue the rest of the procedure
     on the current branch — never force a destructive checkout.

## Manual procedure (`/log-work`)

`/log-work backfill [since]` → **Backfill procedure**.
`/log-work report <period>` → **Report procedure**.

Default behavior (no arguments and no special instruction from the user in
the same message):

1. Summarize what's happened in the current conversation so far (what was
   worked on, key decisions, outcome so far).
2. Check `state.py get <cwd>`. If there's no `ticketKey` yet, run the
   **Matching procedure** to resolve one, then run
   `state.py set-ticket <cwd> <ticketKey> --branch <currentBranch-or-empty>`
   (pass `--branch` with the current git branch name for git repos; omit
   `--branch` entirely for non-git directories).
3. Log the summary (see **Logging a summary**) against the resolved
   `ticketKey`.
4. Run `state.py set-pending <cwd> false`.

If the user instead says the resolved ticket is wrong (e.g. "wrong
ticket", "that's not it"):

1. Run `state.py clear <cwd>`.
2. Run the **Matching procedure** to resolve a new `ticketKey`.
3. Run `state.py set-ticket <cwd> <ticketKey> --branch <currentBranch-or-empty>`
   (pass `--branch` with the current git branch name for git repos; omit
   `--branch` entirely for non-git directories).

## Backfill procedure (`/log-work backfill <since>`)

Rebuilds work log entries for a past window (default: 90 days) from three
sources. Read-only on every source; the only writes are to the work log.

1. **Jira comments.** Candidate tickets: `executeRead` `getMyWork`
   (`type: "worked_on"`, `sinceDays`) plus JQL
   `updated >= -<N>d AND (assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser() OR assignee was currentUser())`.
   For each ticket, `executeRead` `listJiraIssueComments`
   (`orderBy: "-created"`) and keep only comments by the user's accountId
   inside the window. For more than ~30 tickets, split across parallel
   subagents that return JSON (`key, date, category, outcome, links`).
2. **GitHub PRs.**
   `gh search prs --author=@me --created=">=<since>" --limit 200 --json repository,number,title,state,createdAt,url`.
   PR state and repo are the source of truth for "merged" / which repo; fix
   any entry that contradicts them.
3. **claude-mem sessions.** Read-only SQLite at `~/.claude-mem/claude-mem.db`,
   table `session_summaries` (`created_at`, `project`, `request`,
   `completed`). Export the window to JSON, split by month, and give each
   month to a subagent along with that month's PR lines and existing
   entries. Files are large (~250KB/month), so subagents should process
   them with python in chunks.
4. **Merge rules** (give these to the subagents):
   - Group into work items (a theme over one or a few days), not one entry
     per session or PR; roughly 5–15 per month.
   - Never duplicate an existing entry; propose PR-link additions to it
     instead.
   - Date = when the work finished or last activity, not when it was
     logged.
   - Skip trivial sessions (one-off lookups, tab cleanups, statusline
     tweaks).
   - Tag anything that looks like a personal/side project
     `(possibly personal — review)` and **leave it out** of the pages until
     the user confirms.
   - Never invent details; mark uncertain status (e.g. "uncommitted at last
     record") as ongoing.
5. **Write** each month's page (create if missing, private), entries sorted
   newest first; existing entries keep their place ahead of new ones on the
   same date. Run the privacy check. Report per-month counts, what was
   excluded, and coverage gaps (SREIN tickets commented on without
   watching, reviews, Slack-only work).

## Report procedure (`/log-work report <period>`)

Builds a report for a period (e.g. `Q3 2026`, `last 30 days`) from the
work log.

1. Read every monthly page in the period. If the start of the period has
   no entries (the log may start mid-period), fill the gap from
   `gh search prs --created=<range>` and claude-mem titles, and say so in
   coverage notes.
2. Numbers from GitHub:
   - authored: `gh search prs --author=@me --created=<range>`, counted by
     state and repo;
   - reviews:
     `gh search prs --reviewed-by=@me --updated=<range> --json author,repository`,
     excluding author `<githubLogin>` and bots. This is approximate, since it
     matches on updated, not reviewed, date; say so.
   - Explain large "closed unmerged" counts if they come from known test
     runs.
3. Structure:
   - Summary (3–5 outcome-first bullets)
   - By the numbers
   - One section per theme (outcome line, then dated bullets, then who
     benefited and key PRs)
   - Incidents
   - Reviews and collaboration
   - Key decisions table
   - Carried into the next period (ongoing items)
   - Coverage notes
4. Publish privately under the `Reports` folder, titled e.g.
   `Q3 2026 Work Report — <name>`, and run the privacy check. Give the user
   the link and the caveats they should check before sharing.

## Opt-out

If the user says something like "don't log this to Jira" / "don't track
this directory": run `state.py set-tracked <cwd> false` and confirm you've
turned off tracking for this directory. This does not undo itself —
tracking stays off until the user explicitly asks to resume it (e.g.
"start tracking this directory again") or you run
`state.py set-tracked <cwd> true` directly.

## Error handling

If any Atlassian MCP call fails (auth, network, not found), tell the user
briefly what failed and continue the session normally — never block the
user's actual request on a Jira or Confluence error. If the Jira comment
succeeds but the work log append fails (or vice versa), still clear
`pendingLog`; mention the half that failed.
