"""LLM summaries via Gemini.

One call per run: given the items that will ship in today's digest, produce a
short day-overview + a one-line gist per item. Gists are cached in
`items.summary`; the overview lands in `kv`. Ranking and dedup remain
deterministic — only this descriptive text is generated.

Best-effort: if GEMINI_API_KEY is unset (or the call fails), it skips and the
digest falls back to raw snippets. Needs no extra dependency (uses httpx).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import httpx

from .db import get_conn, get_kv, init_db, load_config, set_kv
from .render import select_digest

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "gists": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "gist": {"type": "string"}},
                "required": ["id", "gist"],
            },
        },
    },
    "required": ["overview", "gists"],
}


def _call_gemini(model: str, key: str, prompt: str) -> dict:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            # Newer flash models spend "thinking" tokens from this same budget,
            # so keep it well above the JSON we expect or the output truncates.
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }
    r = httpx.post(
        GEMINI_URL.format(model=model), params={"key": key}, json=body, timeout=90.0
    )
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _build_prompt(items: list[dict], max_chars: int) -> str:
    lines = [
        "You are a terse tech-news editor for a personal daily digest covering "
        "data engineering and AI.",
        "",
        f"For EACH item below, write a plain one-line gist (max {max_chars} chars): "
        "what it is and why it matters, no fluff, no marketing, no trailing period "
        "needed. Then write a 2-sentence 'overview' of the day's themes across all "
        "items. Return JSON matching the schema (fields: overview, gists[{id,gist}]).",
        "",
        "ITEMS:",
    ]
    for it in items:
        snip = (it.get("summary_source") or "").replace("\n", " ").strip()[:400]
        lines.append(f"- id={it['id']} | {it['title']} | {snip}")
    return "\n".join(lines)


def run(conn: sqlite3.Connection, force: bool = False) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("  GEMINI_API_KEY not set -> skipping summaries (digest falls back to snippets)")
        return {"status": "skipped"}

    cfg = load_config()
    scfg = cfg.get("summarize", {})
    model = os.environ.get("GEMINI_MODEL") or scfg.get("model", "gemini-2.0-flash")
    max_chars = int(scfg.get("max_gist_chars", 140))

    payload = select_digest(conn)
    views = [e for sec in payload["sections"] for e in sec["entries"]] + payload["newsletters"]
    # de-dup by id, keep order
    seen, items = set(), []
    for v in views:
        if v["id"] in seen:
            continue
        seen.add(v["id"])
        items.append(v)

    if not items:
        print("  nothing in the digest window to summarize")
        return {"status": "empty"}

    # cache: skip the call if every selected item already has a gist and we have
    # a stored overview (unless forced).
    have_all = all(v.get("summary") for v in items)
    if have_all and get_kv(conn, "overview") and not force:
        print(f"  all {len(items)} items already summarized (cached); overview kept")
        return {"status": "cached", "items": len(items)}

    # attach raw text for the prompt
    rows = {
        r["id"]: r["raw_text"]
        for r in conn.execute(
            f"SELECT id, raw_text FROM items WHERE id IN ({','.join('?' * len(items))})",
            [v["id"] for v in items],
        )
    }
    for v in items:
        v["summary_source"] = rows.get(v["id"], "")

    prompt = _build_prompt(items, max_chars)
    try:
        result = _call_gemini(model, key, prompt)
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"  summarize failed ({type(exc).__name__}: {exc}) -> falling back to snippets")
        return {"status": "error", "error": str(exc)}

    gists = {g["id"]: g["gist"].strip() for g in result.get("gists", []) if g.get("id")}
    updated = 0
    for iid, gist in gists.items():
        if gist:
            conn.execute("UPDATE items SET summary = ? WHERE id = ?", (gist[:max_chars * 2], iid))
            updated += 1
    overview = (result.get("overview") or "").strip()
    if overview:
        set_kv(conn, "overview", overview)
    conn.commit()
    print(f"  summarized {updated}/{len(items)} items via {model}; overview {'set' if overview else 'empty'}")
    return {"status": "ok", "model": model, "items": len(items), "gists": updated}


if __name__ == "__main__":
    conn = init_db()
    print("summarize: generating gists + overview...")
    run(conn, force="--force" in sys.argv)
