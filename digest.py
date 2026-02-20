import os, re, math, hashlib
from datetime import datetime, timezone, timedelta

import feedparser
from dateutil import parser as dtparser
from dotenv import load_dotenv

load_dotenv()

# ---- config (env-tweakable) ----
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "50"))
MAX_TOTAL_ITEMS = int(os.getenv("MAX_TOTAL_ITEMS", "400"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
INTERESTS_MAX_CHARS = int(os.getenv("INTERESTS_MAX_CHARS", "3000"))
SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "500"))
PREFILTER_KEEP_TOP = int(os.getenv("PREFILTER_KEEP_TOP", "200"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
MIN_SCORE_READ = float(os.getenv("MIN_SCORE_READ", "0.65"))
MAX_RETURNED = int(os.getenv("MAX_RETURNED", "40"))


# ---- tiny helpers ----
def load_feeds(path: str) -> list[dict]:
    """
    Supports:
    - blank lines
    - comments starting with #
    - optional naming via: Name | URL

    Returns list of:
    { "name": "...", "url": "..." }
    """
    feeds = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue

            # Named feed: "Name | URL"
            if "|" in s:
                name, url = [x.strip() for x in s.split("|", 1)]
            else:
                name, url = None, s

            feeds.append({
                "name": name,
                "url": url
            })

    return feeds

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def section(md: str, heading: str) -> str:
    m = re.search(rf"(?im)^\s*#{1,6}\s+{re.escape(heading)}\s*$", md)
    if not m:
        return ""
    rest = md[m.end():]
    m2 = re.search(r"(?im)^\s*#{1,6}\s+\S", rest)
    return (rest[:m2.start()] if m2 else rest).strip()

def parse_interests_md(md: str) -> dict:
    keywords = []
    for line in section(md, "Keywords").splitlines():
        line = re.sub(r"^[\-\*\+]\s+", "", line.strip())
        if line:
            keywords.append(line)
    narrative = section(md, "Narrative").strip()
    if len(narrative) > INTERESTS_MAX_CHARS:
        narrative = narrative[:INTERESTS_MAX_CHARS] + "…"
    return {"keywords": keywords[:200], "narrative": narrative}


# ---- rss ----
def parse_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = dtparser.parse(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def fetch_rss_items(feeds: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    items = []
    for feed in feeds:
        url = feed["url"]
        d = feedparser.parse(url)

        # Priority: manual name > RSS title > URL
        source = (
            feed.get("name")
            or d.feed.get("title")
            or url
        ).strip()
        for e in d.entries[:MAX_ITEMS_PER_FEED]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not (title and link):
                continue
            dt = parse_date(e)
            if dt and dt < cutoff:
                continue
            summary = re.sub(r"\s+", " ", (e.get("summary") or e.get("description") or "").strip())
            if len(summary) > SUMMARY_MAX_CHARS:
                summary = summary[:SUMMARY_MAX_CHARS] + "…"
            items.append({
                "id": sha1(f"{source}|{title}|{link}"),
                "source": source,
                "title": title,
                "link": link,
                "published_utc": dt.isoformat() if dt else None,
                "summary": summary,
            })
    # dedupe + newest first
    items = list({it["id"]: it for it in items}.values())
    items.sort(key=lambda x: x["published_utc"] or "", reverse=True)
    return items[:MAX_TOTAL_ITEMS]


# ---- local prefilter ----
def keyword_prefilter(items: list[dict], keywords: list[str], keep_top: int) -> list[dict]:
    kws = [k.lower() for k in keywords if k.strip()]
    def hits(it):
        text = (it.get("title","") + " " + it.get("summary","")).lower()
        return sum(1 for k in kws if k in text)
    scored = [(hits(it), it) for it in items]
    matched = [it for s, it in scored if s > 0]
    if len(matched) < min(50, keep_top):
        return items[:keep_top]
    matched.sort(key=hits, reverse=True)
    return matched[:keep_top]


# ---- triage (backend-agnostic batch loop) ----
def triage_in_batches(interests: dict, items: list[dict], batch_size: int, triage_fn) -> dict:
    """triage_fn(interests, batch) -> dict with keys notes, ranked (and optionally week_of)."""
    week_of = datetime.now(timezone.utc).date().isoformat()
    total = math.ceil(len(items) / batch_size)
    all_ranked, notes_parts = [], []

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        print(f"Triage batch {i // batch_size + 1}/{total} ({len(batch)} items)")
        res = triage_fn(interests, batch)
        if res.get("notes", "").strip():
            notes_parts.append(res["notes"].strip())
        all_ranked.extend(res.get("ranked", []))

    best = {}
    for r in all_ranked:
        rid = r["id"]
        if rid not in best or r["score"] > best[rid]["score"]:
            best[rid] = r

    ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return {"week_of": week_of, "notes": " ".join(dict.fromkeys(notes_parts))[:1000], "ranked": ranked}


# ---- render ----
def render_digest_md(result: dict, items_by_id: dict[str, dict]) -> str:
    week_of = result["week_of"]
    notes = result.get("notes", "").strip()
    ranked = result.get("ranked", [])
    kept = [r for r in ranked if r["score"] >= MIN_SCORE_READ][:MAX_RETURNED]

    lines = [f"# Weekly ToC Digest (week of {week_of})", ""]
    if notes:
        lines += [notes, ""]
    lines += [
        f"**Included:** {len(kept)} (score ≥ {MIN_SCORE_READ:.2f})  ",
        f"**Scored:** {len(ranked)} total items",
        "",
        "---",
        "",
    ]
    if not kept:
        return "\n".join(lines + ["_No items met the relevance threshold this week._", ""])

    for r in kept:
        it = items_by_id.get(r["id"], {})
        tags = ", ".join(r.get("tags", [])) if r.get("tags") else ""
        pub = r.get("published_utc")
        summary = (it.get("summary") or "").strip()

        lines += [
            f"## [{r['title']}]({r['link']})",
            f"*{r['source']}*  ",
            f"Score: **{r['score']:.2f}**" + (f"  \nPublished: {pub}" if pub else ""),
            (f"Tags: {tags}" if tags else ""),
            "",
            r["why"].strip(),
            "",
        ]
        if summary:
            lines += ["<details>", "<summary>RSS summary</summary>", "", summary, "", "</details>", ""]
        lines += ["---", ""]
    return "\n".join(lines)


def main():
    interests = parse_interests_md(read_text("interests.md"))
    feeds = load_feeds("feeds.txt")
    items = fetch_rss_items(feeds)
    print(f"Fetched {len(items)} RSS items (pre-filter)")

    today = datetime.now(timezone.utc).date().isoformat()
    if not items:
        with open("digest.md", "w", encoding="utf-8") as f:
            f.write(f"# Weekly ToC Digest (week of {today})\n\n_No RSS items found in the last {LOOKBACK_DAYS} days._\n")
        print("No items; wrote digest.md")
        return

    items = keyword_prefilter(items, interests["keywords"], keep_top=PREFILTER_KEEP_TOP)
    print(f"Sending {len(items)} RSS items to model (post-filter)")

    items_by_id = {it["id"]: it for it in items}

    from integrations import get_triage_backend
    triage_fn = get_triage_backend()
    result = triage_in_batches(interests, items, BATCH_SIZE, triage_fn)
    md = render_digest_md(result, items_by_id)

    with open("digest.md", "w", encoding="utf-8") as f:
        f.write(md)
    print("Wrote digest.md")


if __name__ == "__main__":
    main()
