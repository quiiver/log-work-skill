#!/usr/bin/env python3
"""otelHeadersHelper for Google Cloud: prints OTLP auth headers as JSON.

Claude Code runs this at startup and every ~29 minutes. The token comes from the
caller's gcloud login and is never written to disk.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state


def headers(config=None):
    if config is None:
        config = state.get_config(state.load_state(state.DEFAULT_STATE_PATH))
    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    out = {"Authorization": f"Bearer {token}"}
    if config.get("otelGcpProject"):
        out["x-goog-user-project"] = config["otelGcpProject"]
    return out


if __name__ == "__main__":
    print(json.dumps(headers()))
