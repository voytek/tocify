"""OpenAI triage backend. Needs OPENAI_API_KEY. Model via OPENAI_MODEL env."""

import os
import time

import httpx
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError

from integrations._shared import SCHEMA, build_triage_prompt, parse_structured_response

SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "500"))


def make_openai_client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key.startswith("sk-"):
        raise RuntimeError("OPENAI_API_KEY missing/invalid (expected to start with 'sk-').")
    http_client = httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        http2=False,
        trust_env=False,
        headers={"Connection": "close", "Accept-Encoding": "gzip"},
    )
    return OpenAI(api_key=key, http_client=http_client)


def call_openai_triage(client: OpenAI, interests: dict, items: list[dict]) -> dict:
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o"
    prompt, _ = build_triage_prompt(interests, items, summary_max_chars=SUMMARY_MAX_CHARS)

    last = None
    for attempt in range(6):
        try:
            resp = client.responses.create(
                model=model,
                input=prompt,
                text={"format": {"type": "json_schema", "name": "weekly_toc_digest", "schema": SCHEMA, "strict": True}},
            )
            return parse_structured_response(resp.output_text)
        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            last = e
            time.sleep(min(60, 2 ** attempt))
    raise last
