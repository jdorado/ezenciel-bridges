#!/usr/bin/env python3
"""Run a real chat completion request against the local Codex bridge."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    api_key = os.environ.get("BRIDGE_API_KEY")
    if not api_key:
        print("BRIDGE_API_KEY is required", file=sys.stderr)
        return 2

    base_url = os.environ.get("BRIDGE_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
    model = os.environ.get("BRIDGE_MODEL", "gpt-5-codex")
    timeout = int(os.environ.get("BRIDGE_TIMEOUT_SECONDS", "1200"))
    prompt = " ".join(sys.argv[1:]).strip() or "Reply with exactly: bridge smoke OK"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print(json.dumps(body, indent=2), file=sys.stderr)
        print(f"Unexpected response shape: {exc}", file=sys.stderr)
        return 1

    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
