#!/usr/bin/env python3
"""CLI + library for the log-work skill's state file (~/.claude/log-work-state.json)."""
import argparse
import json
import os
import re
import subprocess
import socket
import sys
import time

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


def ledger_path(state_path):
    # Session -> work item history, joined to Claude Code OTel data on session.id.
    return os.path.join(os.path.dirname(state_path), "log-work-sessions.jsonl")


def append_ledger(state_path, record):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with open(ledger_path(state_path), "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def info_payload(config, session_id, work_item, title="", repo="", now_ns=None):
    """OTLP/JSON gauge `log_work.session.info` mapping a session to its work item.

    The value is the assignment time (unix seconds) so queries can keep the
    latest assignment per session. Resource attributes match otel_setup.py's,
    which Google's Telemetry API needs (instance + non-global region).
    """
    now_ns = now_ns or time.time_ns()
    attr = lambda k, v: {"key": k, "value": {"stringValue": str(v)[:120]}}
    resource = [
        attr("service.name", "log-work"),
        attr("service.instance.id", config.get("otelInstance", socket.gethostname().split(".")[0])),
        attr("cloud.region", config.get("otelRegion", "us-central1")),
    ]
    if config.get("otelGcpProject"):
        resource.append(attr("gcp.project_id", config["otelGcpProject"]))
    point = {
        "asInt": str(now_ns // 1_000_000_000),
        "timeUnixNano": str(now_ns),
        "attributes": [attr("session.id", session_id), attr("work_item", work_item),
                       attr("work_title", title or work_item), attr("repo", repo or "-")],
    }
    return {"resourceMetrics": [{"resource": {"attributes": resource}, "scopeMetrics": [
        {"scope": {"name": "log-work"},
         "metrics": [{"name": "log_work.session.info", "gauge": {"dataPoints": [point]}}]}]}]}


def send_info(config, payload):
    import urllib.request
    import otel_headers
    hdrs = {"Content-Type": "application/json"}
    if config.get("otelAuth", "gcloud" if config.get("otelGcpProject") else "none") == "gcloud":
        hdrs.update(otel_headers.headers(config))
    req = urllib.request.Request(config["otelEndpoint"].rstrip("/") + "/v1/metrics",
                                 data=json.dumps(payload).encode(), headers=hdrs)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def emit_info_async(state_path, session_id, work_item, title="", repo=""):
    """Fire-and-forget so the SessionStart hook never waits on the network."""
    config = get_config(load_state(state_path))
    if not (config.get("otelEndpoint") and session_id and work_item):
        return
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--state-path", state_path, "emit-info",
         "--session", session_id, "--work-item", work_item, "--title", title, "--repo", repo],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def clear_ticket(state, cwd):
    entry = get_entry(state, cwd)
    entry["ticketKey"] = None
    entry["branch"] = None
    entry["pendingLog"] = False
    state[cwd] = entry
    return entry


# Per-user settings (Jira project, Atlassian/Confluence/GitHub IDs) live in the
# state file under a reserved key; real entries are keyed by absolute paths.
CONFIG_KEY = "__config__"


def get_config(state):
    return dict(state.get(CONFIG_KEY, {}))


def set_config(state, key, value):
    config = get_config(state)
    if value:
        config[key] = value
    else:
        config.pop(key, None)
    state[CONFIG_KEY] = config
    return config


def extract_ticket_keys(text):
    seen = []
    for match in TICKET_KEY_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen


def _slugify(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def slugify_summary(text, max_words=5, max_len=40):
    words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    slug = "-".join(words[:max_words])
    return slug[:max_len].rstrip("-")


def git_username(cwd):
    result = subprocess.run(
        ["git", "-C", cwd, "config", "user.email"],
        capture_output=True,
        text=True,
    )
    email = result.stdout.strip() if result.returncode == 0 else ""
    if email and "@" in email:
        return _slugify(email.split("@", 1)[0])

    result = subprocess.run(
        ["git", "-C", cwd, "config", "user.name"],
        capture_output=True,
        text=True,
    )
    name = result.stdout.strip() if result.returncode == 0 else ""
    if name:
        return _slugify(name)

    return ""


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
    if args.session:
        append_ledger(args.state_path, {"event": "set-ticket", "session_id": args.session,
                                        "cwd": args.cwd, "ticketKey": args.ticket_key,
                                        "branch": args.branch})
        emit_info_async(args.state_path, args.session, args.ticket_key,
                        repo=os.path.basename(args.cwd.rstrip("/")))
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


def cmd_git_username(args):
    print(git_username(args.cwd))


def cmd_slugify(args):
    text = sys.stdin.read()
    print(slugify_summary(text))


def cmd_config(args):
    state = load_state(args.state_path)
    if args.key is None:
        print(json.dumps(get_config(state), sort_keys=True))
        return
    config = set_config(state, args.key, args.value)
    save_state(args.state_path, state)
    print(json.dumps(config, sort_keys=True))


def cmd_emit_info(args):
    config = get_config(load_state(args.state_path))
    if not config.get("otelEndpoint"):
        sys.exit("emit-info: otelEndpoint not configured (see /log-work setup-otel)")
    payload = info_payload(config, args.session, args.work_item, args.title, args.repo)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return
    print(send_info(config, payload))


def wrap_context(text, fmt):
    """Shape hook output for the harness: plain stdout (Claude Code, Codex) or JSON."""
    if fmt == "gemini":
        return json.dumps(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        )
    if fmt == "cursor":
        return json.dumps({"additional_context": text})
    if fmt == "copilot":
        return json.dumps({"additionalContext": text})
    return text


def cmd_session_start(args):
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    # Harnesses that don't send cwd on stdin run the hook in the session's directory.
    cwd = payload.get("cwd") or os.getcwd()
    state = load_state(args.state_path)
    entry = get_entry(state, cwd)
    config = get_config(state)

    branch = git_current_branch(cwd)
    is_repo = branch is not None
    commits = git_recent_commit_subjects(cwd) if is_repo else []
    branch_keys = extract_ticket_keys(branch or "")
    commit_keys = [
        k for k in extract_ticket_keys("\n".join(commits)) if k not in branch_keys
    ]
    session_id = payload.get("session_id")
    if session_id and entry["tracked"]:
        append_ledger(args.state_path, {"event": "session-start", "session_id": session_id,
                                        "cwd": cwd, "source": payload.get("source"),
                                        "ticketKey": entry["ticketKey"], "branch": branch})
        emit_info_async(args.state_path, session_id, entry["ticketKey"] or "unassigned",
                        repo=os.path.basename(cwd.rstrip("/")))

    lines = [
        f"log-work skill — SessionStart for {cwd}",
        "",
        f'State: tracked={entry["tracked"]}, ticketKey={entry["ticketKey"] or "none"}, '
        f'storedBranch={entry["branch"] or "none"}, pendingLog={entry["pendingLog"]}',
        f"Session: {session_id or 'unknown'}",
        f"Git: isRepo={is_repo}, currentBranch={branch or 'n/a'}",
        f"Ticket keys found in branch name: {', '.join(branch_keys) or 'none'}",
        f"Ticket keys found in recent commits: {', '.join(commit_keys) or 'none'}",
        f"Config: {json.dumps(config, sort_keys=True) if config else 'none'}",
        "",
        'ACTION REQUIRED: invoke the "log-work" skill now '
        '(Skill tool, skill: "log-work", args: "session-start") to process this '
        "session start before addressing the user's first message.",
    ]
    print(wrap_context("\n".join(lines), args.format))


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
    p.add_argument("--session", default=None, help="session id, recorded in the ledger")
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

    p = sub.add_parser("git-username")
    p.add_argument("cwd")
    p.set_defaults(func=cmd_git_username)

    p = sub.add_parser("slugify")
    p.set_defaults(func=cmd_slugify)

    p = sub.add_parser("config", help="print config, set KEY VALUE, or unset KEY")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?", default="")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("emit-info", help="publish log_work.session.info (session -> work item)")
    p.add_argument("--session", required=True)
    p.add_argument("--work-item", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_emit_info)

    p = sub.add_parser("session-start")
    p.add_argument(
        "--format", choices=["plain", "gemini", "cursor", "copilot"], default="plain"
    )
    p.set_defaults(func=cmd_session_start)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
