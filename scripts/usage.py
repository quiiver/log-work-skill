#!/usr/bin/env python3
"""Claude usage from session transcripts -> OTLP counters (`log_work.usage.*`).

Works where Claude Code's own OTel export doesn't (the desktop app): Claude Code
writes `cost-state` lines into each session transcript with its own cost, token,
duration and line totals. They are cumulative per run (`startTime`) and written
when a session's process stops, so values lag until then but totals are exact.

Hook usage (SessionStart and Stop): `usage.py hook` reads the hook JSON on stdin
and hands off to a detached `usage.py emit`, so the hook returns immediately.
Only the transcript bytes added since the last run are scanned.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state

# Successful tool calls that count as a commit or a PR. Subagents commit too, so
# their transcripts (<session>/subagents/*.jsonl) are scanned as well.
COMMIT_RE = re.compile(r"git(?:\s+-C\s+\S+)?\s+commit\b")
PR_RE = re.compile(r"gh\s+pr\s+create\b")
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?^\s*\1\s*$", re.S | re.M)
SEGMENT_RE = re.compile(r"&&|\|\||[;|\n]")


def _commands(cmd):
    """Shell segments of a command, with heredoc bodies removed, so code or
    text that merely mentions `git commit` doesn't count."""
    return [seg.strip() for seg in SEGMENT_RE.split(HEREDOC_RE.sub("", cmd)) if seg.strip()]

TOKEN_FIELDS = {"inputTokens": "input", "outputTokens": "output", "cacheReadInputTokens": "cacheRead",
                "cacheCreationInputTokens": "cacheCreation", "thinkingTokens": "thinking"}


def usage_path(state_path):
    return os.path.join(os.path.dirname(state_path), "log-work-usage.json")


def _tool_kind(block):
    """'commits' / 'prs' for a tool_use block that creates one, else None."""
    name, inp = block.get("name", ""), block.get("input") or {}
    if name.endswith("create_pull_request"):
        return "prs"
    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
    segments = _commands(cmd) if name == "Bash" else []
    if any(PR_RE.match(seg) for seg in segments):
        return "prs"
    if any(COMMIT_RE.match(seg) for seg in segments):
        return "commits"
    return None


def scan(transcript, offset, pending=None, counts=None):
    """Scan bytes after offset. Returns (latest cost-state per run, new offset).

    If pending/counts dicts are given, also counts successful commit/PR tool
    calls: a tool_use is held in pending until its tool_result arrives.
    """
    runs = {}
    size = os.path.getsize(transcript)
    if offset > size:  # transcript rewritten or truncated: start over
        offset = 0
    with open(transcript, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # partial last line still being written; pick it up next time
            offset += len(raw)
            is_cost = b'"cost-state"' in raw
            is_call = counts is not None and b'"tool_use"' in raw and (
                b"commit" in raw or b"pr create" in raw or b"create_pull_request" in raw)
            is_result = counts is not None and pending and b'"tool_result"' in raw
            if not (is_cost or is_call or is_result):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") == "cost-state" and "startTime" in d:
                runs[str(d["startTime"])] = {k: d.get(k) for k in (
                    "totalCostUSD", "totalAPIDuration", "totalToolDuration", "totalDuration",
                    "totalLinesAdded", "totalLinesRemoved", "modelUsage")}
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and is_call:
                    kind = _tool_kind(block)
                    if kind:
                        pending[block.get("id")] = kind
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    kind = pending.pop(block["tool_use_id"])
                    if not block.get("is_error"):
                        counts[kind] = counts.get(kind, 0) + 1
    return runs, offset


def usage_metrics(session_id, runs, now_ns=None, counts=None, counts_start_ms=None):
    """Cumulative OTLP sums, one series per (run, model/type), keyed on session.id."""
    now_ns = now_ns or time.time_ns()
    series = {}  # metric name -> list of data points

    def add(name, value, start_ms, **attrs):
        point = {"asDouble": float(value or 0), "startTimeUnixNano": str(int(start_ms) * 1_000_000),
                 "timeUnixNano": str(now_ns),
                 "attributes": [state.attr("session.id", session_id), state.attr("run", start_ms)]
                 + [state.attr(k, v) for k, v in attrs.items()]}
        series.setdefault(name, []).append(point)

    for start_ms, run in runs.items():
        for model, mu in (run.get("modelUsage") or {}).items():
            add("log_work.usage.cost_usd", mu.get("costUSD"), start_ms, model=model)
            for field, kind in TOKEN_FIELDS.items():
                add("log_work.usage.tokens", mu.get(field), start_ms, model=model, type=kind)
        add("log_work.usage.api_seconds", (run.get("totalAPIDuration") or 0) / 1000, start_ms)
        add("log_work.usage.tool_seconds", (run.get("totalToolDuration") or 0) / 1000, start_ms)
        add("log_work.usage.lines", run.get("totalLinesAdded"), start_ms, type="added")
        add("log_work.usage.lines", run.get("totalLinesRemoved"), start_ms, type="removed")
    if counts is not None and counts_start_ms:
        add("log_work.usage.commits", counts.get("commits", 0), counts_start_ms)
        add("log_work.usage.prs", counts.get("prs", 0), counts_start_ms)
    return [{"name": name, "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": points}}
            for name, points in series.items()]


def emit(state_path, session_id, transcript):
    config = state.get_config(state.load_state(state_path))
    if not (config.get("otelEndpoint") and os.path.exists(transcript)):
        return "skipped"
    path = usage_path(state_path)
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # Stop hooks can overlap
        store = json.load(open(path)) if os.path.exists(path) else {}
        entry = store.get(session_id) or {}
        if "files" not in entry:  # first run, or pre-commit-counting state: rescan everything
            entry = {"files": {}, "runs": entry.get("runs", {}), "pending": {}, "counts": {}}
        files = [transcript] + sorted(glob.glob(os.path.splitext(transcript)[0] + "/subagents/*.jsonl"))
        for f in files:
            runs, entry["files"][f] = scan(f, entry["files"].get(f, 0), entry["pending"], entry["counts"])
            if f == transcript:  # cost-state is session-wide and lives in the main transcript
                entry["runs"].update(runs)
        if entry["runs"] and "counts_start_ms" not in entry:
            entry["counts_start_ms"] = min(int(r) for r in entry["runs"])
        store[session_id] = entry
        json.dump(store, open(path, "w"))
    if not entry["runs"]:
        return "no cost-state yet"
    # Send every known run each time: points must be < 25h old, and resending
    # cumulative totals is idempotent.
    metrics = usage_metrics(session_id, entry["runs"], counts=entry["counts"],
                            counts_start_ms=entry.get("counts_start_ms"))
    status = state.send_info(config, state.otlp_metrics(config, metrics))
    return f"sent {len(entry['runs'])} run(s), {entry['counts']}: HTTP {status}"


def cmd_hook(args):
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    session_id, transcript = payload.get("session_id"), payload.get("transcript_path")
    cwd = payload.get("cwd") or os.getcwd()
    st = state.load_state(args.state_path)
    if not (session_id and transcript and state.get_entry(st, cwd)["tracked"]
            and state.get_config(st).get("otelEndpoint")):
        return
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--state-path", args.state_path, "emit",
         "--session", session_id, "--transcript", transcript],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state-path", default=state.DEFAULT_STATE_PATH)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="SessionStart/Stop hook entry point (reads hook JSON on stdin)")
    e = sub.add_parser("emit", help="scan a transcript and send its usage now")
    e.add_argument("--session", required=True)
    e.add_argument("--transcript", required=True)
    args = p.parse_args(argv)
    if args.command == "hook":
        cmd_hook(args)
    else:
        print(emit(args.state_path, args.session, args.transcript))


if __name__ == "__main__":
    main()
