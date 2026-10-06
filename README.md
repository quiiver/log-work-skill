# log-work

An agent skill that keeps a record of your work:

- matches each session to a Jira ticket and logs a summary as a comment;
- when there's no ticket, asks whether to create one or log to a private
  monthly Confluence work log (every session also gets a work log entry);
- backfills the work log from Jira comments, GitHub PRs and claude-mem
  history (`/log-work backfill`);
- writes period reports, e.g. `/log-work report Q3 2026`.

See [SKILL.md](SKILL.md) for the procedures, [DESIGN.md](DESIGN.md) for the
design, and [PLAN.md](PLAN.md) for the implementation record.

## Requirements

- Python 3 (stdlib only), `git`, and `gh` (authenticated) for backfill and
  reports.
- An Atlassian MCP server with Jira and Confluence access, available in the
  harness you use.
- Optional: [claude-mem](https://github.com/thedotmack/claude-mem), for
  backfilling from session history.

## Configuration

Nothing is hardcoded. On first use the skill asks which Jira project new
tickets should go in, then looks up and caches your Atlassian site, account,
personal space and work log folders (creating the folders, private, if
needed), plus your GitHub login. Everything is stored under `__config__` in
`~/.claude/log-work-state.json`:

```bash
python3 ~/.claude/skills/log-work/scripts/state.py config                       # show
python3 ~/.claude/skills/log-work/scripts/state.py config jiraProject MZCLD     # set
python3 ~/.claude/skills/log-work/scripts/state.py config cloudId               # unset (re-resolve)
```

## Install

Clone once:

```bash
git clone git@github.com:quiiver/log-work-skill.git ~/.claude/skills/log-work
```

Then register the SessionStart hook for each harness you use. The hook runs
`state.py session-start`, which prints a context block telling the agent to
run the skill. `--format` shapes the output for harnesses that want JSON.

### Claude Code: CLI, desktop app, VS Code, JetBrains

All of these read `~/.claude/skills/` and `~/.claude/settings.json`. Add:

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

**Not covered by this:**

- **Claude Code on the web / cloud sessions** don't read your local
  `~/.claude`, so the hook and skill are missing there. The workaround is
  to commit a project-level `.claude/settings.json` and the skill into the
  repo, or to install the skill as a plugin (below).
- **Claude Agent SDK** doesn't load `~/.claude/settings.json` hooks or
  `~/.claude/skills` automatically. Pass the skill as a local plugin
  (`plugins: [{ type: "local", path: "..." }]`) or register the hook in
  code.
- **Plugin packaging (not done yet):** a `.claude-plugin/plugin.json`
  manifest, the skill under `skills/log-work/`, and a `hooks/hooks.json`
  with the same hook using
  `python3 "${CLAUDE_PLUGIN_ROOT}"/scripts/state.py session-start`, plus a
  `marketplace.json` so you can run
  `/plugin marketplace add quiiver/log-work-skill`. This would let one
  install bring both the skill and the hook. It needs the repo layout to
  change, so it's left for later.

### Other harnesses

Most other agent CLIs discover skills in `~/.agents/skills`, so link it once:

```bash
mkdir -p ~/.agents/skills && ln -s ~/.claude/skills/log-work ~/.agents/skills/log-work
```

| Harness | Hook config | Output | Skills also found in |
|---|---|---|---|
| Codex CLI | `~/.codex/hooks.json` (approve once via `/hooks`) | plain (default) | `~/.agents/skills` |
| Gemini CLI | `~/.gemini/settings.json` | `--format gemini` | `~/.gemini/skills`, `~/.agents/skills` |
| Cursor | `~/.cursor/hooks.json` | `--format cursor` | `~/.cursor/skills`, `~/.claude/skills` |
| Copilot CLI | `~/.copilot/hooks/log-work.json` | `--format copilot` | `~/.copilot/skills`, `~/.claude/skills` |
| opencode | JS plugin (no shell hooks) | plain | `~/.config/opencode/skills`, `~/.claude/skills` |

**Codex CLI** — `~/.codex/hooks.json`:

```json
{"hooks": {"SessionStart": [{"matcher": "startup|resume", "hooks": [
  {"type": "command", "command": "python3 ~/.claude/skills/log-work/scripts/state.py session-start"}]}]}}
```

**Gemini CLI** — `~/.gemini/settings.json`:

```json
{"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
  {"name": "log-work", "type": "command", "timeout": 15000,
   "command": "python3 ~/.claude/skills/log-work/scripts/state.py session-start --format gemini"}]}]}}
```

**Cursor** — `~/.cursor/hooks.json`. Forum reports say `sessionStart`
context is sometimes dropped. If the hook doesn't take effect, use the
`AGENTS.md` fallback below as an always-applied rule.

```json
{"version": 1, "hooks": {"sessionStart": [
  {"command": "python3 ~/.claude/skills/log-work/scripts/state.py session-start --format cursor"}]}}
```

**Copilot CLI** — `~/.copilot/hooks/log-work.json`. The cloud coding agent
only reads `.github/hooks` from the repo, and the script won't exist in
its sandbox.

```json
{"version": 1, "hooks": {"sessionStart": [
  {"type": "command", "bash": "python3 ~/.claude/skills/log-work/scripts/state.py session-start --format copilot", "timeoutSec": 15}]}}
```

**opencode** — `~/.config/opencode/plugins/log-work.js`. This uses an
experimental hook API, so treat it as unverified.

```js
let ctx;
export const LogWork = async ({ $ }) => ({
  "experimental.chat.system.transform": async (_in, out) => {
    ctx ??= await $`python3 ${process.env.HOME}/.claude/skills/log-work/scripts/state.py session-start`.text();
    out.system.push(ctx);
  },
});
```

**Fallback for any harness:** put this line in `AGENTS.md` (or
`GEMINI.md`). It's the weakest option, because it relies on the agent
choosing to run the command.

> At session start, run `python3 ~/.claude/skills/log-work/scripts/state.py session-start` and follow its output.

### Caveats outside Claude Code

- SKILL.md names Claude Code tools (`Skill`, `ToolSearch`,
  `AskUserQuestion`) and the Atlassian MCP tool names. Other harnesses map
  these differently, so expect some rough edges.
- The hook configs for other harnesses come from their docs as of late
  2026. They haven't been tested here.
- State is keyed by directory and shared across harnesses, so a session in
  Codex and one in Claude Code in the same repo use the same ticket.

## Tests

```bash
cd ~/.claude/skills/log-work/scripts && python3 -m unittest test_state
```
