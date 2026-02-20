"""Shared prompt template and JSON schema for all triage backends.

OpenAI, Claude, and Gemini all use JSON Schema for structured output; SCHEMA is the
single source of truth. Cursor has no schema API and uses prompt-only + parse.
"""

import json
import os

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "week_of": {"type": "string"},
        "notes": {"type": "string"},
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "link": {"type": "string"},
                    "source": {"type": "string"},
                    "published_utc": {"type": ["string", "null"]},
                    "score": {"type": "number"},
                    "why": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "link", "source", "published_utc", "score", "why", "tags"],
            },
        },
    },
    "required": ["week_of", "notes", "ranked"],
}


def load_prompt_template(path: str = "prompt.txt") -> str:
    if not os.path.exists(path):
        raise RuntimeError("prompt.txt not found in repo root")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_triage_prompt(
    interests: dict, items: list[dict], *, summary_max_chars: int = 500
) -> tuple[str, list[dict]]:
    """Build the triage prompt and lean items. Returns (prompt_string, lean_items)."""
    lean_items = [
        {
            "id": it["id"],
            "source": it["source"],
            "title": it["title"],
            "link": it["link"],
            "published_utc": it.get("published_utc"),
            "summary": (it.get("summary") or "")[:summary_max_chars],
        }
        for it in items
    ]
    template = load_prompt_template()
    prompt = (
        template.replace("{{KEYWORDS}}", json.dumps(interests["keywords"], ensure_ascii=False))
        .replace("{{NARRATIVE}}", interests["narrative"])
        .replace("{{ITEMS}}", json.dumps(lean_items, ensure_ascii=False))
    )
    return (prompt, lean_items)


def parse_structured_response(response_text: str) -> dict:
    """Parse JSON from a structured-output response; validate 'ranked' exists."""
    data = json.loads(response_text)
    if not isinstance(data, dict) or "ranked" not in data:
        raise ValueError("Response missing required 'ranked' field")
    return data
