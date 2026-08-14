"""LLM summaries via Gemini.

One call per run produces, from the items that will ship in today's digest:
  - a short day `overview`,
  - a `summary` per section/type (dataeng, ai, newsletter, …),
  - a one-line `gist` per item (used by Telegram + the dashboard).

Section summaries land in `kv['section_summaries']` (JSON), the overview in
`kv['overview']`, and gists in `items.summary`. Ranking + dedup stay
deterministic — only this descriptive text is generated.

Best-effort: if GEMINI_API_KEY is unset (or the call fails), it skips and the
digest falls back to the compact item list. Uses httpx, no extra dependency.
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

_SCHEMA = {
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


def _call_gemini(model: str, key: str, prompt: str) -> dict:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            # Newer flash models spend "thinking" tokens from this same budget,
            # so keep it well above the JSON we expect or the output truncates
            # (summary + refs + top_pick + a gist per item, across all sections).
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }
    url = GEMINI_URL.format(model=model)
    last_exc = None
    for attempt in range(4):
        try:
            r = httpx.post(url, params={"key": key}, json=body, timeout=90.0)
            if r.status_code in (429, 500, 503):  # transient — back off and retry
                raise httpx.HTTPStatusError(f"transient {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in (429, 500, 503) and not isinstance(exc, httpx.TransportError):
                raise
            if attempt < 3:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
    raise last_exc


def _build_prompt(grouped: list[dict], max_chars: int) -> str:
    lines = [
        "You are a terse tech-news editor for a personal daily digest covering "
        "data engineering and AI. Write plainly, no marketing, no fluff.",
        "",
        "Return JSON matching the schema:",
        "- overview: 2 sentences on the day's themes across ALL sections.",
        "- sections: for EACH section tag below, an object with:",
        "    * summary: 2-4 sentences synthesizing what happened in that category "
        "(what the items mean, don't just relist titles). Cite sources inline with "
        "bracketed numbers [1], [2], … where [1] is the first id in 'refs', [2] the "
        "second, and so on.",
        "    * refs: the item ids the summary actually draws from, in the SAME order "
        "as the [n] markers used in the summary text.",
        "    * top_pick: the id of the SINGLE most significant / must-read item in "
        "that section.",
        f"- gists: for EACH item id, a one-line gist (max {max_chars} chars).",
        "",
    ]
    for g in grouped:
        lines.append(f"## SECTION {g['tag']} ({len(g['items'])} items)")
        for it in g["items"]:
            snip = (it.get("snip") or "").replace("\n", " ").strip()[:300]
            lines.append(f"- id={it['id']} | {it['title']} | {snip}")
        lines.append("")
    return "\n".join(lines)


def run(conn: sqlite3.Connection, force: bool = False) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("  GEMINI_API_KEY not set -> skipping summaries (digest falls back to item list)")
        return {"status": "skipped"}

    cfg = load_config()
    scfg = cfg.get("summarize", {})
    model = os.environ.get("GEMINI_MODEL") or scfg.get("model", "gemini-flash-latest")
    max_chars = int(scfg.get("max_gist_chars", 140))

    payload = select_digest(conn)
    # Watching is a persistent, user-managed queue. Do not re-summarize it on
    # every daily run; its entries remain unchanged until opened in the page.
    summarizable_sections = [sec for sec in payload["sections"] if sec["tag"] != "watching"]
    grouped = [
        {"tag": sec["tag"], "items": [{"id": e["id"], "title": e["title"]} for e in sec["entries"]]}
        for sec in summarizable_sections
    ]
    if payload["newsletters"]:
        grouped.append(
            {"tag": "newsletter",
             "items": [{"id": e["id"], "title": e["title"]} for e in payload["newsletters"]]}
        )

    all_views = [e for sec in summarizable_sections for e in sec["entries"]] + payload["newsletters"]
    seen, items = set(), []
    for v in all_views:
        if v["id"] not in seen:
            seen.add(v["id"])
            items.append(v)

    if not items:
        print("  nothing in the digest window to summarize")
        return {"status": "empty"}

    have_all = all(v.get("summary") for v in items)
    if have_all and get_kv(conn, "overview") and get_kv(conn, "section_summaries") and not force:
        print(f"  all {len(items)} items already summarized (cached)")
        return {"status": "cached", "items": len(items)}

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

    prompt = _build_prompt(grouped, max_chars)
    try:
        result = _call_gemini(model, key, prompt)
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"  summarize failed ({type(exc).__name__}: {exc}) -> falling back to item list")
        return {"status": "error", "error": str(exc)}

    # per-item gists
    gists = {g["id"]: g["gist"].strip() for g in result.get("gists", []) if g.get("id")}
    updated = 0
    for iid, gist in gists.items():
        if gist:
            conn.execute("UPDATE items SET summary = ? WHERE id = ?", (gist[: max_chars * 2], iid))
            updated += 1

    # per-section summaries + the sources (item ids) each one cites
    sec_summaries = {
        s["tag"]: {"summary": s["summary"].strip(), "refs": s.get("refs", []),
                   "top_pick": s.get("top_pick")}
        for s in result.get("sections", [])
        if s.get("tag") and s.get("summary")
    }
    set_kv(conn, "section_summaries", json.dumps(sec_summaries))

    overview = (result.get("overview") or "").strip()
    if overview:
        set_kv(conn, "overview", overview)
    conn.commit()
    print(
        f"  {model}: overview {'set' if overview else 'empty'}, "
        f"{len(sec_summaries)} section summaries, {updated}/{len(items)} gists"
    )
    return {"status": "ok", "model": model, "sections": len(sec_summaries), "gists": updated}


if __name__ == "__main__":
    conn = init_db()
    print("summarize: generating overview + section summaries + gists...")
    run(conn, force="--force" in sys.argv)
