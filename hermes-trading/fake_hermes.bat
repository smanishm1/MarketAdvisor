@echo off
REM Stub "hermes" CLI for testing the reflection's hermes code path WITHOUT installing
REM the real Nous Hermes agent. reflect.py calls:  hermes -z "<prompt>"  and parses one
REM JSON object from stdout. This ignores its args and prints a valid proposal so a card
REM labeled source=hermes appears. Point the app at it with HERMES_CMD (see below), then
REM remove HERMES_CMD to use the real hermes.
echo {"variable": "catastrophe_stop_pct", "new_value": 30, "rationale": "stub hermes proposal - testing the hermes path end to end"}
