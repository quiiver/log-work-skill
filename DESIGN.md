# log-work — Jira Work Tracking Skill Design

Date: 2026-08-20
Status: Approved, pending implementation plan

## Purpose

Automatically keep Jira in sync with the work done in Claude Code sessions:
find (or create) the right ticket for whatever you're working on, and log a
summary of what happened as a comment — without you having to remember to do
it.

## Scope

- Personal skill named `log-work`, lives in `~/.claude/skills/log-work/`
  (invocable manually as `/log-work`). Not tied to any one repo.
- Single default Jira project for new tickets: **MZCLD**.
- Not in scope for v1: time tracking / formal worklogs, multi-step status
  workflows beyond an initial Backlog/To Do → In Progress bump, cross-repo
  session merging.

## Architecture

Two integration points, one shared state store, one skill:

1. **SessionStart hook** — runs at the start of every new session/context
   window. Drives the retroactive-log-catch-up + match-or-continue flow.
2. **Manual `/log-work` command** — force a log-now of the conversation so
   far, or force a rematch if the resolved ticket is wrong.
3. **Global state file**, `~/.claude/log-work-state.json`, keyed by
   absolute working directory:
   ```json
   {
     "/Users/wstuckey/dev/some-repo": {
       "ticketKey": "MZCLD-456",
       "branch": "feature/foo",
       "lastLoggedSessionId": "...",
       "pendingLog": false,
       "tracked": true
     }
   }
   ```
   `tracked: false` marks a directory as opted out (see Opt-out below) —
   the skill short-circuits immediately without matching or logging.

> **Note on scope (revised after implementation research):** an earlier
> version of this design included a Stop/SessionEnd hook to log immediately
> when a session ends. Dropped: `Stop` fires after *every* assistant turn
> (not at session end), and `SessionEnd` hooks get only a 1.5–60s timeout
> budget with no documented MCP tool access — neither can reliably do
> LLM-driven summarization + Jira API calls. SessionStart catch-up (below)
> now does all of the "log the last session's work" job, not just the
> failure case: `pendingLog` is set `true` whenever a session resolves an
> active ticket, and only cleared by an actual logging action (catch-up or
> `/log-work`) — never by a session simply ending.

## Flow

### SessionStart

1. Look up state for `cwd`.
2. If `tracked` is explicitly `false`, stop — do nothing.
3. If `pendingLog` is `true` (the previous tracked session ended without
   `/log-work` having been run), read that previous session's transcript,
   summarize it, and post the summary as a comment on the stored
   `ticketKey`. Set `pendingLog: false`.
4. Decide continuation:
   - If state exists for this directory, and (for git repos) the current
     branch matches the stored `branch`, or (for non-git directories) state
     simply exists and hasn't been invalidated — treat this as the same
     task. Skip matching, keep using `ticketKey`.
   - Otherwise, run matching (below).
5. Resolve ticket status: fetch current status via `getJiraIssue`. If it's
   in a "not started" category (e.g. Backlog, To Do — check the status
   `statusCategory`, not a hardcoded name list), look up available
   transitions with `getTransitionsForJiraIssue` and, if one clearly moves
   it to an "in progress" category, apply it via `transitionJiraIssue`.
   Best-effort: if ambiguous (multiple candidate transitions) or none
   found, skip silently — don't block on this.
6. Write/update state for `cwd` with the resolved `ticketKey` and branch.

### Matching (only when not a continuation)

1. **Git branch key check** (git repos only): look for a Jira key pattern
   (`[A-Z][A-Z0-9]+-\d+`) in the current branch name specifically (not
   commit messages — a repo with keyed commit history alongside a keyed
   branch must not make this ambiguous). If the branch name has exactly
   one key, verify with `getJiraIssue` — if it resolves, use it, done.
2. **JQL keyword search** (git repos where step 1 found zero or multiple
   keys, or any non-git directory): search
   `project = MZCLD AND assignee = currentUser() AND statusCategory != Done`,
   further filtered/ranked by keywords pulled from the directory name,
   README, or the task at hand.
3. **Disambiguate with the user**: if step 2 returns zero, one, or several
   candidates, present them (or "no matches") and ask the user to confirm
   one, or decline all.
4. **Create new ticket**: if the user declines all candidates (or there
   were none and they want a new one), draft a ticket — summary and
   description generated from context, project `MZCLD`, type `Task` — and
   **wait for explicit user confirmation** before calling `createJiraIssue`.
   Assign it to the user via `lookupJiraAccountId`/`atlassianUserInfo`
   afterward.
5. **Ensure a ticket branch** (git repos only): if the current branch's
   name doesn't already contain the resolved `ticketKey` (true whenever
   the match came from step 2, 3, or 4 — step 1 by definition already
   satisfies this), create and check out a new branch named
   `<user>/<ticketKey>-<ctx>` before continuing:
   - `<user>`: the local part of `git config user.email` (falling back to
     a slugified `git config user.name` if no email is set).
   - `<ctx>`: a short kebab-case slug (a handful of words) derived from
     the ticket's Jira summary.
   If checkout fails for any reason (name collision, uncommitted changes
   that would be overwritten), fall back to switching to an existing
   branch of that name, or failing that, tell the user and continue on
   the current branch — never force a destructive checkout.

Ticket creation is the one step that always requires confirmation, since it's
visible to the team and hard to undo. Matching against and logging to an
already-identified ticket is automatic. Branch creation (step 5) is
automatic too — it's a routine, reversible git operation, not a
team-visible action like ticket creation.

### Manual `/log-work`

- Default: summarize the conversation so far and log it now (same posting
  logic as the SessionStart catch-up), without waiting for the session to
  end. Sets `pendingLog: false` after a successful post.
- If told the resolved ticket is wrong: drop the stored state for `cwd` and
  re-run the Matching flow.

## Logging content

Comments only — not formal Jira worklogs (`addWorklogToJiraIssue` wants a
duration, and this design deliberately doesn't track time). Each comment is
a short generated summary of what happened in the session: what was
worked on, key decisions, and outcome. Not a raw commit list, though commit
messages can inform the summary.

## Opt-out

Any session can be marked "don't track" — e.g. by telling the skill in
conversation ("don't log this to Jira") or via a manual state edit. This
sets `tracked: false` for that directory, and SessionStart short-circuits
immediately for that directory. This does not undo itself: tracking stays
off until the user explicitly asks to resume it (e.g. "start tracking this
directory again") or runs `state.py set-tracked <cwd> true` directly —
`/log-work` does not implicitly clear the opt-out.

## Non-git directories

Skip the branch/commit key scan entirely; go straight to JQL search and
user disambiguation. Continuation detection still works (state exists for
`cwd`, no branch to compare), it's just less precise about detecting when
the underlying task has actually changed — acceptable for v1.

## Error handling

- Jira API errors (auth, network, not found) during matching/logging:
  surface a brief note to the user in the session rather than failing
  silently or blocking the session from proceeding.
- If `getTransitionsForJiraIssue`/`transitionJiraIssue` fails or is
  ambiguous, skip the status bump silently — it's a nice-to-have, not
  load-bearing.

## Testing

- Manual scenario walkthroughs (this is a skill + hooks, not a unit-testable
  library):
  1. Fresh directory, git repo, branch contains a valid ticket key → skips
     search, logs against that ticket.
  2. Fresh directory, git repo, no key in branch/commits → falls to JQL
     search, finds one candidate, confirms with user.
  3. Fresh directory, no candidates found → drafts new ticket, confirms,
     creates + assigns.
  4. Second session in the same directory/branch → continuation, no
     rematch, just logs.
  5. Non-git directory → skips key scan, goes to search/ask.
  6. Session ends without running `/log-work` → next SessionStart finds
     `pendingLog: true` and logs the missed session first, before doing its
     own continuation/matching step.
  7. Opt-out directory → SessionStart no-ops immediately.
  8. Ticket already In Progress → status-bump step is a no-op.
  9. `/log-work` run mid-session → posts a comment immediately and clears
     `pendingLog` without waiting for the next SessionStart.
  10. Matched/created a ticket while on an unrelated branch (e.g. `main`)
      → checks out a new `<user>/<ticketKey>-<ctx>` branch before
      proceeding, and the stored `branch` reflects the new branch.
  11. Already on a branch containing the ticket key → no branch is
      created (this is a no-op every time step 1 of Matching, or
      continuation, already applies).
