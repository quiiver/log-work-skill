#!/usr/bin/env python3
"""Point Claude Code's OpenTelemetry export at the collector named in log-work config.

Writes the `env` block (and `otelHeadersHelper` for Google auth) in ~/.claude/settings.json.
Config keys (set with `state.py config <key> <value>`):
  otelEndpoint    OTLP endpoint of the collector (required)
  otelProtocol    http/protobuf (default), http/json or grpc
  otelGcpProject  GCP project the telemetry belongs to, e.g. cloudeng-agents-nonprod
  otelAuth        gcloud (default when otelGcpProject is set) or none
  otelRegion      GCP region for metric series (default us-central1; GCP only)
  otelInstance    instance label for metric series (default: short hostname; GCP only)
  otelTraces      true to also export traces (beta: CLAUDE_CODE_ENHANCED_TELEMETRY_BETA)

Google's Telemetry API stores metrics as Prometheus series, which need an instance
and a real region (not "global"); Claude Code sends neither, so they are added as
resource attributes.
"""
import argparse
import json
import os
import re
import shutil
import socket
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
    "OTEL_TRACES_EXPORTER",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
]
GCP_ATTRS = ["gcp.project_id", "cloud.region", "service.instance.id"]


def _set_resource_attr(current, key, value):
    pairs = [p for p in (current or "").split(",") if p and not p.startswith(key + "=")]
    if value:
        pairs.append(f"{key}={value}")
    return ",".join(pairs)


def _attr_value(value):
    # OTEL_RESOURCE_ATTRIBUTES values can't hold spaces, commas, quotes etc.
    return re.sub(r"[^A-Za-z0-9._:/-]", "-", value)


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
    if str(config.get("otelTraces", "")).lower() in ("1", "true", "yes"):
        # Tracing is beta; spans redact prompt and tool content by default.
        env.update({"OTEL_TRACES_EXPORTER": "otlp", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1"})
    else:
        env.pop("OTEL_TRACES_EXPORTER", None)
        env.pop("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", None)
    gcp = {
        "gcp.project_id": project,
        "cloud.region": config.get("otelRegion", "us-central1") if project else None,
        "service.instance.id": config.get("otelInstance", socket.gethostname().split(".")[0]) if project else None,
    }
    attrs = env.get("OTEL_RESOURCE_ATTRIBUTES")
    for key in GCP_ATTRS:
        attrs = _set_resource_attr(attrs, key, _attr_value(gcp[key]) if gcp[key] else None)
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
    attrs = env.get("OTEL_RESOURCE_ATTRIBUTES")
    for key in GCP_ATTRS:
        attrs = _set_resource_attr(attrs, key, None)
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
