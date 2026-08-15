"""Minimal 'hermes' bridge: send the reflection prompt straight to the Claude API.

This lets you test the real Claude brain as the reflection engine WITHOUT installing
the Nous Hermes agent. reflect.py shells out to a command as `<cmd> <flag> <prompt>`
and parses one JSON object from stdout. Wire it up by pointing those at this script:

    # in .env
    HERMES_CMD=C:\\ClaudeDev\\hermes-trading\\.venv\\Scripts\\python.exe
    HERMES_PROMPT_FLAG=C:\\ClaudeDev\\hermes-trading\\claude_brain.py

That makes the call `python.exe claude_brain.py "<prompt>"`, so the prompt arrives as
argv[1] untouched (no shell quoting issues). Needs ANTHROPIC_API_KEY in .env.

Standalone smoke test:
    .venv/Scripts/python claude_brain.py "Reply with ONLY {\"variable\": \"none\", \"rationale\": \"ping\"}"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from hermes_trading.paths import load_env

MODEL = os.environ.get("CLAUDE_BRAIN_MODEL", "claude-sonnet-4-6")
SYSTEM = "You are the reflection brain of a paper-trading agent. Respond with ONLY a single JSON object, no prose."


def main() -> int:
    load_env()  # pull ANTHROPIC_API_KEY from .env if not already in the environment
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set (.env)", file=sys.stderr)
        return 2

    prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 400,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"ERROR: Claude API {exc.code}: {exc.read().decode('utf-8')[:300]}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Concatenate any text blocks; reflect._extract_json pulls the JSON object out.
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    print(text.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
