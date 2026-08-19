"""LLM summaries via Gemini.

Two focused calls per run (split to stay under free-tier TPM limits):

  Call 1 — digest pass (fast, small):
    overview, per-section summaries, per-item gists
    Input: digest items only (~12 items × 300-char snips)

  Call 2 — daily_read pass (heavier, best-effort):
    one article object per feed item in the daily window
    Input: daily window items (title + 300-char snip, no long body)

Results land in:
  kv['overview'], kv['section_summaries'], kv['daily_read']
  items.summary (gists)

Best-effort: if GEMINI_API_KEY is unset or any call fails the digest falls
back to the compact item list. Uses httpx, no extra dependency.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import httpx

from .db import get_conn, get_kv, init_db, load_config, set_kv
from .render import select_digest

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# ── JSON schemas ──────────────────────────────────────────────────────────────

_SCHEMA_DIGEST = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "summary": {"type": "string"},
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "top_pick": {"type": "string"},
                },
                "required": ["tag", "summary", "refs", "top_pick"],
            },
        },
        "gists": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "gist": {"type": "string"}},
                "required": ["id", "gist"],
            },
        },
    },
    "required": ["overview", "sections", "gists"],
}

_SCHEMA_DAILY_READ = {
    "type": "object",
    "properties": {
        "daily_read": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "body", "refs"],
            },
        },
    },
    "required": ["daily_read"],
}


# ── Gemini HTTP call ──────────────────────────────────────────────────────────

def _call_gemini(model: str, key: str, prompt: str, schema: dict,
                 max_output_tokens: int = 8192) -> dict:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    url = GEMINI_URL.format(model=model)
    last_exc = None
    max_attempts = 6
    for attempt in range(max_attempts):
        try:
            r = httpx.post(url, params={"key": key}, json=body, timeout=90.0)
            if r.status_code in (429, 500, 503):
                raise httpx.HTTPStatusError(
                    f"transient {r.status_code}", request=r.request, response=r
                )
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in (429, 500, 503) and not isinstance(exc, httpx.TransportError):
                raise
            if attempt < max_attempts - 1:
                wait = min(2 ** attempt, 60)
                print(
                    f"  Gemini {status or 'transport error'} on attempt "
                    f"{attempt + 1}/{max_attempts}, retrying in {wait}s…"
                )
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_digest_prompt(grouped: list[dict], max_chars: int) -> str:
    """Small prompt: overview + section summaries + one-line gists."""
    lines = [
        "You are a terse tech-news editor for a personal daily digest covering "
        "data engineering and AI. Write plainly, no marketing, no fluff.",
        "IMPORTANT: Write ALL human-readable text in Vietnamese (Tiếng Việt).",
        "",
        "Return JSON with:",
        "- overview: 2 Vietnamese sentences on the day's themes across all sections.",
        "- sections: for EACH section tag, an object with:",
        "    * summary: 2-4 Vietnamese sentences synthesising what happened.",
        "      Cite sources inline as [1], [2], … where [n] is the nth id in 'refs'.",
        "    * refs: item ids cited, in marker order.",
        "    * top_pick: id of the single most significant item.",
        f"- gists: for EACH item id below, a one-line Vietnamese gist (max {max_chars} chars).",
        "",
    ]
    for g in grouped:
        lines.append(f"## SECTION {g['tag']} ({len(g['items'])} items)")
        for it in g["items"]:
            snip = (it.get("snip") or "").replace("\n", " ").strip()[:300]
            lines.append(f"- id={it['id']} | {it['title']} | {snip}")
        lines.append("")
    return "\n".join(lines)


def _build_daily_read_prompt(daily_items: list[dict], words_per_article: int) -> str:
    """Heavier prompt: one article object per feed item."""
    lines = [
        "You are a terse tech-news editor. Write plainly in Vietnamese (Tiếng Việt).",
        "",
        "For EACH source article listed below produce one object in daily_read with:",
        "  * heading: short Vietnamese title (max 12 words).",
        f"  * body: self-contained Vietnamese summary (max {words_per_article} words). "
        "Cite with [n] markers referencing this article's own refs list.",
        "  * refs: item ids cited, in marker order.",
        "",
        "## SOURCE ARTICLES",
    ]
    for it in daily_items:
        snip = (it.get("snip") or "").replace("\n", " ").strip()[:300]
        lines.append(f"- id={it['id']} | {it['title']} | {snip}")
    return "\n".join(lines)


# ── Daily-read item loader ────────────────────────────────────────────────────

def _daily_read_items(conn: sqlite3.Connection, window_days: float) -> list[dict]:
    """Primary feed items within the daily window, ordered newest-first."""
    cutoff = time.time() - window_days * 86400
    rows = conn.execute(
        "SELECT id, source_id, title, raw_text, published_at FROM items "
        "WHERE is_primary=1 ORDER BY published_at DESC"
    ).fetchall()
    from .db import parse_iso
    return [
        {
            "id": r["id"],
            "source_id": r["source_id"],
            "title": r["title"],
            "snip": r["raw_text"] or "",
        }
        for r in rows
        if parse_iso(r["published_at"]).timestamp() >= cutoff
    ]


# ── Main entry point ──────────────────────────────────────────────────────────

def run(conn: sqlite3.Connection, force: bool = False) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("  GEMINI_API_KEY not set -> skipping summaries (digest falls back to item list)")
        return {"status": "skipped"}

    cfg = load_config()
    scfg = cfg.get("summarize", {})
    model = os.environ.get("GEMINI_MODEL") or scfg.get("model", "gemini-flash-latest")
    max_chars = int(scfg.get("max_gist_chars", 140))
    daily_read_pages = int(scfg.get("daily_read_pages", 5))
    words_per_page = int(scfg.get("daily_read_words_per_page", 320))
    # words budget spread evenly across however many items are in the window
    # (resolved per-call once we know the item count)
    words_per_page_total = daily_read_pages * words_per_page

    payload = select_digest(conn)
    summarizable_sections = [s for s in payload["sections"] if s["tag"] != "watching"]
    grouped = [
        {
            "tag": sec["tag"],
            "items": [{"id": e["id"], "title": e["title"]} for e in sec["entries"]],
        }
        for sec in summarizable_sections
    ]
    if payload["newsletters"]:
        grouped.append(
            {
                "tag": "newsletter",
                "items": [{"id": e["id"], "title": e["title"]} for e in payload["newsletters"]],
            }
        )

    all_views = (
        [e for sec in summarizable_sections for e in sec["entries"]] + payload["newsletters"]
    )
    seen: set[str] = set()
    items: list[dict] = []
    for v in all_views:
        if v["id"] not in seen:
            seen.add(v["id"])
            items.append(v)

    if not items:
        print("  nothing in the digest window to summarize")
        return {"status": "empty"}

    # Fill per-item snips for the digest prompt
    raw_map = {
        r["id"]: r["raw_text"]
        for r in conn.execute(
            f"SELECT id, raw_text FROM items WHERE id IN ({','.join('?' * len(items))})",
            [v["id"] for v in items],
        )
    }
    for g in grouped:
        for it in g["items"]:
            it["snip"] = raw_map.get(it["id"], "")

    have_all_gists = all(v.get("summary") for v in items)
    have_cache = (
        have_all_gists
        and get_kv(conn, "overview")
        and get_kv(conn, "section_summaries")
        and get_kv(conn, "daily_read")
    )
    if have_cache and not force:
        print(f"  all {len(items)} items already summarized (cached)")
        return {"status": "cached", "items": len(items)}

    # ── Call 1: digest pass (overview + sections + gists) ────────────────────
    digest_prompt = _build_digest_prompt(grouped, max_chars)
    digest_result: dict = {}
    try:
        digest_result = _call_gemini(model, key, digest_prompt, _SCHEMA_DIGEST,
                                     max_output_tokens=8192)
    except Exception as exc:  # noqa: BLE001
        print(f"  digest pass failed ({type(exc).__name__}: {exc}) -> skipping summaries")
        return {"status": "error", "error": str(exc)}

    # Persist gists
    gists = {g["id"]: g["gist"].strip() for g in digest_result.get("gists", []) if g.get("id")}
    updated = 0
    for iid, gist in gists.items():
        if gist:
            conn.execute(
                "UPDATE items SET summary = ? WHERE id = ?", (gist[: max_chars * 2], iid)
            )
            updated += 1

    sec_summaries = {
        s["tag"]: {
            "summary": s["summary"].strip(),
            "refs": s.get("refs", []),
            "top_pick": s.get("top_pick"),
        }
        for s in digest_result.get("sections", [])
        if s.get("tag") and s.get("summary")
    }
    set_kv(conn, "section_summaries", json.dumps(sec_summaries))

    overview = (digest_result.get("overview") or "").strip()
    if overview:
        set_kv(conn, "overview", overview)

    conn.commit()
    print(
        f"  [{model}] digest pass: overview={'set' if overview else 'empty'}, "
        f"{len(sec_summaries)} sections, {updated}/{len(items)} gists"
    )

    # ── Call 2: daily_read pass (best-effort, separate quota hit) ────────────
    daily_items = _daily_read_items(
        conn, float(cfg["digest"].get("render_window_days", 4))
    )
    if not daily_items:
        print("  daily_read pass: no items in window, skipping")
        return {"status": "ok", "model": model, "sections": len(sec_summaries), "gists": updated}

    words_per_article = max(30, words_per_page_total // len(daily_items))
    daily_prompt = _build_daily_read_prompt(daily_items, words_per_article)
    try:
        daily_result = _call_gemini(model, key, daily_prompt, _SCHEMA_DAILY_READ,
                                    max_output_tokens=8192)
        daily_read_articles = [
            a
            for a in daily_result.get("daily_read", [])
            if isinstance(a, dict) and a.get("heading") and a.get("body")
        ]
        if daily_read_articles:
            set_kv(conn, "daily_read", json.dumps(daily_read_articles))
            set_kv(conn, "daily_read_item_count", str(len(daily_items)))
            conn.commit()
        print(
            f"  [{model}] daily_read pass: "
            f"{len(daily_items)} items → {len(daily_read_articles)} articles"
        )
    except Exception as exc:  # noqa: BLE001
        # daily_read is display-only; gists + section summaries are already saved
        print(
            f"  daily_read pass failed ({type(exc).__name__}: {exc}) "
            f"-> digest gists/sections still saved"
        )

    return {"status": "ok", "model": model, "sections": len(sec_summaries), "gists": updated}


if __name__ == "__main__":
    conn = init_db()
    print("summarize: generating overview + section summaries + gists...")
    run(conn, force="--force" in sys.argv)
