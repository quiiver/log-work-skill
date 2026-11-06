#!/usr/bin/env python3
"""CLI + library for the log-work skill's state file (~/.claude/log-work-state.json)."""
import argparse
import json
import os
import re
import secrets
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
    # Per-directory state is only the opt-out; work items belong to sessions (ledger).
    return {"tracked": True}


def get_entry(state, cwd):
    entry = default_entry()
    entry.update(state.get(cwd, {}))
    return entry


def new_worklog_id():
    # One id per unticketed work item, so the dashboard doesn't lump them all under
    # WORKLOG. No "-<digits>" suffix, so it never parses as a Jira key.
    return f"WORKLOG:{time.strftime('%Y-%m-%d')}-{secrets.token_hex(2)}"


def set_tracked(state, cwd, tracked):
    entry = get_entry(state, cwd)
    entry["tracked"] = tracked
    state[cwd] = entry
    return entry


def ledger_path(state_path):
    # Session -> work item history, joined to Claude Code OTel data on session.id.
    return os.path.join(os.path.dirname(state_path), "log-work-sessions.jsonl")


# Ledger events that set a session's work item (backfill-worklog-id: one-off migration).
ITEM_EVENTS = ("set-item", "set-ticket", "backfill-worklog-id")


def _ledger_rows(state_path):
    path = ledger_path(state_path)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _item(row):
    return row.get("workItem") or row.get("ticketKey") if row.get("event") in ITEM_EVENTS else None


def session_item(state_path, session_id):
    """The work item last set for this session, or None. Never inherited from the directory."""
    found = None
    for row in _ledger_rows(state_path):
        if row.get("session_id") == session_id and _item(row):
            found = _item(row)
    return found


def recent_items(state_path, n=4):
    """Distinct work items set most recently, newest first (hints for the session-start question)."""
    out = []
    for row in reversed(_ledger_rows(state_path)):
        item = _item(row)
        if item and item not in ("unassigned", "WORKLOG") and item not in out:
            out.append(item)
            if len(out) == n:
                break
    return out


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
    point = {
        "asInt": str(now_ns // 1_000_000_000),
        "timeUnixNano": str(now_ns),
        "attributes": [attr("session.id", session_id), attr("work_item", work_item),
                       attr("work_title", title or work_item), attr("repo", repo or "-")]
        # same label name as Claude Code's own metrics, for the dashboard's user filter
        + ([attr("user.email", config["userEmail"])] if config.get("userEmail") else []),
    }
    return otlp_metrics(config, [{"name": "log_work.session.info", "gauge": {"dataPoints": [point]}}])


def title_payload(config, work_item, title, size="", work_type="", now_ns=None):
    """OTLP/JSON gauge `log_work.work_item.info`: a work item's title (Jira summary)
    and, when set, its S/M/L/XL size (`work_size`) and type (`work_type`). The value is the set time, so
    queries keep the latest per work item. A new metric name for size never loaded
    in Quick's metrics proxy, so size rides on this one: always send the title with it."""
    now_ns = now_ns or time.time_ns()
    point = {"asInt": str(now_ns // 1_000_000_000), "timeUnixNano": str(now_ns),
             "attributes": [attr("work_item", work_item), attr("work_title", title)]
             + ([attr("work_size", size)] if size else []) + ([attr("work_type", work_type)] if work_type else [])}
    return otlp_metrics(config, [{"name": "log_work.work_item.info", "gauge": {"dataPoints": [point]}}])


SIZES = ("S", "M", "L", "XL")
TYPES = ("build", "fix", "investigate", "other")


def attr(key, value):
    return {"key": key, "value": {"stringValue": str(value)[:120]}}


def otlp_metrics(config, metrics):
    """Wrap OTLP metrics with the resource attributes Google's Telemetry API needs."""
    resource = [
        attr("service.name", "log-work"),
        attr("service.instance.id", config.get("otelInstance", socket.gethostname().split(".")[0])),
        attr("cloud.region", config.get("otelRegion", "us-central1")),
    ]
    if config.get("otelGcpProject"):
        resource.append(attr("gcp.project_id", config["otelGcpProject"]))
    return {"resourceMetrics": [{"resource": {"attributes": resource}, "scopeMetrics": [
        {"scope": {"name": "log-work"}, "metrics": metrics}]}]}


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


def git_current_branch(cwd):
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _parse_bool(value):
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def cmd_set_item(args):
    """Set this session's work item: a ticket key, or WORKLOG (gets a new WORKLOG:<date>-<hex> id)."""
    state = load_state(args.state_path)
    item = new_worklog_id() if args.work_item == "WORKLOG" else args.work_item
    append_ledger(args.state_path, {"event": "set-item", "session_id": args.session, "cwd": args.cwd,
                                    "workItem": item, "branch": git_current_branch(args.cwd)})
    emit_info_async(args.state_path, args.session, item, repo=os.path.basename(args.cwd.rstrip("/")))
    if args.title and get_config(state).get("otelEndpoint"):
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--state-path", args.state_path,
                          "set-title", item, args.title],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    print(item)


def cmd_item(args):
    print(session_item(args.state_path, args.session) or "")


def cmd_set_title(args):
    config = get_config(load_state(args.state_path))
    if not config.get("otelEndpoint"):
        sys.exit("set-title: otelEndpoint not configured (see /log-work setup-otel)")
    print(send_info(config, title_payload(config, args.work_item, args.title, args.size, args.type)))


def cmd_set_tracked(args):
    state = load_state(args.state_path)
    entry = set_tracked(state, args.cwd, args.value)
    save_state(args.state_path, state)
    print(json.dumps(entry))


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
    work_item = args.work_item
    if not work_item or work_item == "WORKLOG":  # default: the item set for this session
        work_item = session_item(args.state_path, args.session) or work_item
    if not work_item:
        sys.exit("emit-info: no work item set for this session (state.py set-item)")
    payload = info_payload(config, args.session, work_item, args.title, args.repo)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return
    print(send_info(config, payload))
    if args.title and work_item.startswith("WORKLOG:"):  # the entry title is the work item's title
        print(send_info(config, title_payload(config, work_item, args.title, args.size, args.type)))
    elif args.size or args.type:
        print("emit-info: --size/--type need a WORKLOG item; for tickets run set-title <ticket> <Jira summary> --size/--type", file=sys.stderr)


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
    config = get_config(state)
    session_id = payload.get("session_id")
    head = f"log-work skill — SessionStart for {cwd}"
    if not get_entry(state, cwd)["tracked"]:
        print(wrap_context(f"{head}\n\nTracking is off for this directory: do nothing.", args.format))
        return

    branch = git_current_branch(cwd)
    item = session_item(args.state_path, session_id) if session_id else None
    if session_id:
        append_ledger(args.state_path, {"event": "session-start", "session_id": session_id, "cwd": cwd,
                                        "source": payload.get("source"), "branch": branch})
    if item:  # resumed or compacted: keep its item, and don't overwrite the tag sent at log time
        lines = [head, "", f"Session: {session_id}", f"Work item: {item} (already set for this session; nothing to do)"]
    else:
        if session_id:
            emit_info_async(args.state_path, session_id, "unassigned", repo=os.path.basename(cwd.rstrip("/")))
        lines = [
            head, "",
            f"Session: {session_id or 'unknown'}",
            "Work item: none yet",
            f"Branch: {branch or 'n/a (not a git repo)'}; ticket keys in it: {', '.join(extract_ticket_keys(branch or '')) or 'none'}",
            f"Recent work items: {', '.join(recent_items(args.state_path)) or 'none'}",
            f"Config: {json.dumps(config, sort_keys=True) if config else 'none'}",
            "",
            'ACTION REQUIRED: invoke the "log-work" skill now '
            '(Skill tool, skill: "log-work", args: "session-start") to ask which work item this '
            "session is for, before addressing the user's first message.",
        ]
    print(wrap_context("\n".join(lines), args.format))


def build_parser():
    parser = argparse.ArgumentParser(description="log-work skill state store")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("set-item", help="set this session's work item (ticket key or WORKLOG)")
    p.add_argument("session")
    p.add_argument("work_item")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--title", default="", help="ticket summary, published as the work item's title")
    p.set_defaults(func=cmd_set_item)

    p = sub.add_parser("item", help="print this session's work item")
    p.add_argument("session")
    p.set_defaults(func=cmd_item)

    p = sub.add_parser("set-title", help="publish log_work.work_item.info (work item -> title)")
    p.add_argument("work_item")
    p.add_argument("title")
    p.add_argument("--size", type=str.upper, choices=SIZES, default="", help="work item size (S/M/L/XL)")
    p.add_argument("--type", type=str.lower, choices=TYPES, default="", help="work type (build/fix/investigate/other)")
    p.set_defaults(func=cmd_set_title)

    p = sub.add_parser("set-tracked")
    p.add_argument("cwd")
    p.add_argument("value", type=_parse_bool)
    p.set_defaults(func=cmd_set_tracked)

    p = sub.add_parser("config", help="print config, set KEY VALUE, or unset KEY")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?", default="")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("emit-info", help="publish log_work.session.info (session -> work item)")
    p.add_argument("--session", required=True)
    p.add_argument("--work-item", default="", help="default: the item set for this session")
    p.add_argument("--title", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--size", type=str.upper, choices=SIZES, default="", help="work item size (WORKLOG items; for tickets use set-title --size)")
    p.add_argument("--type", type=str.lower, choices=TYPES, default="", help="work type (WORKLOG items; for tickets use set-title --type)")
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
