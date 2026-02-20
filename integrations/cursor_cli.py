"""Cursor CLI triage backend. Needs CURSOR_API_KEY and `agent` on PATH."""

import json
import os
import subprocess
import time

from integrations._shared import build_triage_prompt, parse_structured_response

SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "500"))

# Must match SCHEMA in _shared (Cursor has no structured-output API)
CURSOR_PROMPT_SUFFIX = """

Return **only** a single JSON object, no markdown code fences, no commentary. Schema:
{"week_of": "<ISO date>", "notes": "<string>", "ranked": [{"id": "<string>", "title": "<string>", "link": "<string>", "source": "<string>", "published_utc": "<string|null>", "score": <0-1>, "why": "<string>", "tags": ["<string>"]}]}
"""


def is_available() -> bool:
    return bool(os.environ.get("CURSOR_API_KEY", "").strip())


def call_cursor_triage(interests: dict, items: list[dict]) -> dict:
    prompt, _ = build_triage_prompt(
        interests, items, summary_max_chars=SUMMARY_MAX_CHARS
    )
    prompt = prompt + CURSOR_PROMPT_SUFFIX
    args = ["agent", "-p", "--output-format", "text", "--trust", prompt]
    last = None
    for attempt in range(2):
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, env=os.environ
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"cursor CLI exit {result.returncode}: {result.stderr or result.stdout or 'no output'}"
                )
            response_text = (result.stdout or "").strip()
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ValueError("No JSON object found in Cursor output")
            return parse_structured_response(response_text[start:end])
        except (ValueError, json.JSONDecodeError, RuntimeError) as e:
            last = e
            if attempt == 0:
                time.sleep(3)
    raise last
