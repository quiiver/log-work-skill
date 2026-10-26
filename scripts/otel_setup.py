#!/usr/bin/env python3
"""Point Claude Code's OpenTelemetry export at the collector named in log-work config.

Writes the `env` block (and `otelHeadersHelper` for Google auth) in ~/.claude/settings.json.
Config keys (set with `state.py config <key> <value>`):
  otelEndpoint    OTLP endpoint of the collector (required)
  otelProtocol    http/protobuf (default), http/json or grpc
  otelGcpProject  GCP project the telemetry belongs to, e.g. cloudeng-agents-nonprod
  otelAuth        gcloud (default when otelGcpProject is set) or none
"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otel_headers.py")
MANAGED_ENV = [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_METRICS_INCLUDE_VERSION",
]


def _set_resource_attr(current, key, value):
    pairs = [p for p in (current or "").split(",") if p and not p.startswith(key + "=")]
    if value:
        pairs.append(f"{key}={value}")
    return ",".join(pairs)


def apply(settings, config, helper=HELPER):
    """Return a copy of settings with telemetry configured from config."""
    endpoint = config.get("otelEndpoint")
    if not endpoint:
        raise ValueError("otelEndpoint is not set: state.py config otelEndpoint <url>")
    protocol = config.get("otelProtocol", "http/protobuf")
    project = config.get("otelGcpProject")
    auth = config.get("otelAuth", "gcloud" if project else "none")
    if auth == "gcloud" and not protocol.startswith("http"):
        raise ValueError("otelAuth=gcloud needs an http/* protocol (headers helper is http-only)")

    out = json.loads(json.dumps(settings))
    env = out.setdefault("env", {})
    env.update({
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        # Cloud Monitoring stores cumulative metrics; Claude Code defaults to delta.
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
        "OTEL_METRICS_INCLUDE_VERSION": "true",
    })
    attrs = _set_resource_attr(env.get("OTEL_RESOURCE_ATTRIBUTES"), "gcp.project_id", project)
    if attrs:
        env["OTEL_RESOURCE_ATTRIBUTES"] = attrs
    else:
        env.pop("OTEL_RESOURCE_ATTRIBUTES", None)
    if auth == "gcloud":
        out["otelHeadersHelper"] = helper
    elif out.get("otelHeadersHelper") == helper:
        del out["otelHeadersHelper"]
    return out


def disable(settings, helper=HELPER):
    out = json.loads(json.dumps(settings))
    env = out.get("env", {})
    for key in MANAGED_ENV:
        env.pop(key, None)
    attrs = _set_resource_attr(env.get("OTEL_RESOURCE_ATTRIBUTES"), "gcp.project_id", None)
    if attrs:
        env["OTEL_RESOURCE_ATTRIBUTES"] = attrs
    else:
        env.pop("OTEL_RESOURCE_ATTRIBUTES", None)
    if out.get("otelHeadersHelper") == helper:
        del out["otelHeadersHelper"]
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    p.add_argument("--state-path", default=state.DEFAULT_STATE_PATH)
    p.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    p.add_argument("--disable", action="store_true", help="remove the telemetry settings")
    args = p.parse_args(argv)

    settings = {}
    if os.path.exists(args.settings):
        with open(args.settings) as f:
            settings = json.load(f)
    config = state.get_config(state.load_state(args.state_path))
    try:
        new = disable(settings) if args.disable else apply(settings, config)
    except ValueError as e:
        sys.exit(f"otel_setup: {e}")

    before, after = settings.get("env", {}), new.get("env", {})
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            print(f"env.{key}: {before.get(key)!r} -> {after.get(key)!r}")
    if settings.get("otelHeadersHelper") != new.get("otelHeadersHelper"):
        print(f"otelHeadersHelper: {settings.get('otelHeadersHelper')!r} -> {new.get('otelHeadersHelper')!r}")
    if args.dry_run or new == settings:
        print("no changes written" if new == settings else "dry run: nothing written")
        return

    if os.path.exists(args.settings):
        backup = f"{args.settings}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(args.settings, backup)
        print(f"backup: {backup}")
    with open(args.settings, "w") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    print(f"wrote {args.settings}; restart Claude Code sessions to pick it up")


if __name__ == "__main__":
    main()
