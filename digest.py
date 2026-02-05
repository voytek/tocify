import os
import re
import json
import hashlib
from datetime import datetime, timezone, timedelta

import feedparser
import httpx
from dateutil import parser as dtparser
from openai import OpenAI

# clean up any key issues
def make_openai_client() -> OpenAI:
    raw = os.environ.get("OPENAI_API_KEY", "")
    # Strip whitespace/newlines; reject obviously bad keys early
    key = raw.strip()

    if not key or not key.startswith("sk-"):
        raise RuntimeError("OPENAI_API_KEY is missing or does not look like an OpenAI key (expected to start with 'sk-').")

    http_client = httpx.Client(
        timeout=httpx.Timeout(
            connect=30.0,
            read=300.0,   # <-- key: allow slow responses
            write=30.0,
            pool=30.0,
        ),
        http2=False,
        trust_env=False,
        headers={"Connection": "close"},  # reduces some h11 edge cases
    )
    return OpenAI(api_key=key, http_client=http_client)

# ---------- Config ----------
MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "50"))
MAX_TOTAL_ITEMS = int(os.getenv("MAX_TOTAL_ITEMS", "500"))
MIN_SCORE_READ = float(os.getenv("MIN_SCORE_READ", "0.65"))  # threshold for inclusion in digest.md
MAX_RETURNED = int(os.getenv("MAX_RETURNED", "10"))          # cap for digest.md
INTERESTS_MAX_CHARS = int(os.getenv("INTERESTS_MAX_CHARS", "12000"))
SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "2000"))

# Only include items newer than this many days (helps avoid old backlog flooding)
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

# ---------- Helpers ----------
def load_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        lines = []
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            lines.append(s)
        return lines

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def parse_interests_md(md: str) -> dict:
    """
    Convention:
    - Keywords under a heading '## Keywords' (or '# Keywords'), one per line until next heading.
    - Everything else is narrative/context.
    """
    keywords = []
    narrative = md

    # Find keywords block
    m = re.search(r"(?im)^\s*#{1,6}\s+Keywords\s*$", md)
    if m:
        start = m.end()
        rest = md[start:]
        # Stop at next heading
        m2 = re.search(r"(?im)^\s*#{1,6}\s+\S", rest)
        block = rest[: m2.start()] if m2 else rest
        # Keywords: non-empty lines, strip bullets
        for line in block.splitlines():
            line = line.strip()
            line = re.sub(r"^[\-\*\+]\s+", "", line)
            if line:
                keywords.append(line)
        # Remove the keywords block from narrative (optional)
        # We'll keep full md as narrative anyway; keywords are just emphasized.
    else:
        # Fallback: try a 'Keywords:' line
        m = re.search(r"(?im)^\s*Keywords\s*:\s*(.+)$", md)
        if m:
            keywords = [k.strip() for k in re.split(r"[,\n;]+", m.group(1)) if k.strip()]

    narrative = md[:INTERESTS_MAX_CHARS]
    return {"keywords": keywords[:200], "narrative": narrative}

def parse_date(entry) -> datetime | None:
    # feedparser may provide structured time
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    if getattr(entry, "updated_parsed", None):
        return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    # try string fields
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = dtparser.parse(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def fetch_rss_items(feed_urls: list[str]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    items = []
    for url in feed_urls:
        d = feedparser.parse(url)
        source = (d.feed.get("title") or url).strip()
        for e in d.entries[:MAX_ITEMS_PER_FEED]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue

            dt = parse_date(e)
            if dt and dt < cutoff:
                continue

            summary = (e.get("summary") or e.get("description") or "").strip()
            if len(summary) > SUMMARY_MAX_CHARS:
                summary = summary[:SUMMARY_MAX_CHARS] + "…"

            items.append(
                {
                    "id": sha1(f"{source}|{title}|{link}"),
                    "source": source,
                    "title": title,
                    "link": link,
                    "published_utc": dt.isoformat() if dt else None,
                    "summary": summary,
                }
            )

    # De-dupe by id
    dedup = {}
    for it in items:
        dedup[it["id"]] = it
    items = list(dedup.values())

    # Sort newest first (helps prompt)
    items.sort(key=lambda x: x["published_utc"] or "", reverse=True)

    return items[:MAX_TOTAL_ITEMS]

def call_openai_triage(interests: dict, items: list[dict]) -> dict:
    # Force HTTP/1.1 and ignore any proxy env vars that can break TLS in CI
    http_client = httpx.Client(
        timeout=httpx.Timeout(60.0, connect=20.0),
        http2=False,
        trust_env=False,
    )
    client = OpenAI(http_client=http_client)

    schema = {
        "name": "weekly_toc_digest",
        "schema": {
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
        },
    }

    # System-style instruction inside prompt (portable)
    prompt = f"""
You are triaging weekly journal table-of-contents RSS items for a researcher.
Use the user's interests seed below as the primary basis for relevance.

Output rules:
- Return JSON strictly matching the provided schema.
- Provide a relevance score in [0, 1].
- "why" must be 1–2 sentences, concrete (methods/phenomenon/data type).
- "tags" should be short (e.g., EEG, aperiodic, timescales, HMM, ECG, clinical, state dynamics).
- Rank highest score first.
- If only title/short summary is available, be cautious; score lower unless clearly aligned.
- Do NOT hallucinate details that aren't present.

Interests keywords (emphasize strongly):
{json.dumps(interests["keywords"], ensure_ascii=False)}

Interests seed (narrative + user's paper titles/abstracts):
{interests["narrative"]}

RSS items to triage:
{json.dumps(items, ensure_ascii=False)}
"""
    resp = client.responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "weekly_toc_digest",
                "schema": schema["schema"],  # the actual JSON Schema object
                "strict": True,
            }
        }
    )
    return json.loads(resp.output_text)

def render_digest_md(result: dict, items_by_id: dict[str, dict]) -> str:
    week_of = result["week_of"]
    notes = result["notes"].strip()
    ranked = result["ranked"]

    # Filter to "relevant" for digest.md
    kept = [r for r in ranked if r["score"] >= MIN_SCORE_READ]
    kept = kept[:MAX_RETURNED]

    lines = []
    lines.append(f"# Weekly ToC Digest (week of {week_of})")
    lines.append("")
    if notes:
        lines.append(notes)
        lines.append("")

    lines.append(f"**Included:** {len(kept)} (score ≥ {MIN_SCORE_READ:.2f})  \n**Scored:** {len(ranked)} total items")
    lines.append("")
    lines.append("---")
    lines.append("")

    if not kept:
        lines.append("_No items met the relevance threshold this week._")
        lines.append("")
        return "\n".join(lines)

    for r in kept:
        title = r["title"]
        link = r["link"]
        source = r["source"]
        score = r["score"]
        why = r["why"].strip()
        tags = ", ".join(r["tags"]) if r["tags"] else ""

        pub = r.get("published_utc")
        pub_str = f"  \nPublished: {pub}" if pub else ""

        # Include the RSS summary (if available) as a collapsible details block for quick scanning
        summary = items_by_id.get(r["id"], {}).get("summary", "").strip()

        lines.append(f"## [{title}]({link})")
        lines.append(f"*{source}*  \nScore: **{score:.2f}**{pub_str}")
        if tags:
            lines.append(f"Tags: {tags}")
        lines.append("")
        lines.append(why)
        lines.append("")

        if summary:
            lines.append("<details>")
            lines.append("<summary>RSS summary</summary>")
            lines.append("")
            # Avoid giant blobs
            safe = summary.replace("\n", " ").strip()
            lines.append(safe)
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)

def main():
    feed_urls = load_lines("feeds.txt")
    interests_md = read_text("interests.md")
    interests = parse_interests_md(interests_md)

    items = fetch_rss_items(feed_urls)
    items_by_id = {it["id"]: it for it in items}

    # If no items, still write a digest explaining that.
    if not items:
        today = datetime.now(timezone.utc).date().isoformat()
        md = f"# Weekly ToC Digest (week of {today})\n\n_No RSS items found in the last {LOOKBACK_DAYS} days._\n"
        with open("digest.md", "w", encoding="utf-8") as f:
            f.write(md)
        print("No items; wrote digest.md")
        return

    result = call_openai_triage(interests, items)

    # Ensure sorted by score descending (model should do it; we enforce)
    result["ranked"].sort(key=lambda x: x["score"], reverse=True)

    md = render_digest_md(result, items_by_id)
    with open("digest.md", "w", encoding="utf-8") as f:
        f.write(md)
    print("Wrote digest.md")

if __name__ == "__main__":
    main()
