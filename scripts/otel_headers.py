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

token = subprocess.run(
    ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
).stdout.strip()
headers = {"Authorization": f"Bearer {token}"}
project = state.get_config(state.load_state(state.DEFAULT_STATE_PATH)).get("otelGcpProject")
if project:
    headers["x-goog-user-project"] = project
print(json.dumps(headers))
