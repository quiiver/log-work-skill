# log-work

A Claude Code skill that keeps a record of my work:

- matches each session to a Jira ticket (default project MZCLD) and logs a summary as a comment;
- when there's no ticket, asks whether to create one or log to a private monthly Confluence work log;
- backfills the work log from Jira comments, GitHub PRs and claude-mem history (`/log-work backfill`);
- writes period reports, e.g. `/log-work report Q3 2026`.

See [SKILL.md](SKILL.md) for the procedures and [DESIGN.md](DESIGN.md) / [PLAN.md](PLAN.md) for the original (Jira-only) design.

## Install

```bash
git clone git@github.com:quiiver/log-work-skill.git ~/.claude/skills/log-work
```

Register the SessionStart hook in `~/.claude/settings.json`:

```json
{
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
    ]
  }
}
```

Requires the Atlassian MCP plugin, `gh`, and (for backfill) claude-mem. The "Known IDs" in SKILL.md are mine; change them, or delete them so the skill looks them up.

State lives in `~/.claude/log-work-state.json`.

## Tests

```bash
cd scripts && python3 -m unittest test_state
```
