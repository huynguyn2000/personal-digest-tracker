"""Deterministic, explainable scoring.

    score = source_weight * (1 + keyword_score) * recency_factor

No LLM. Every item records a human-readable `score_why` so you can see exactly
why it surfaced and tune weights in config.yaml accordingly.
"""
from __future__ import annotations

import sqlite3

from .db import get_conn, init_db, load_config, now_utc, parse_iso, source_map

RAW_MATCH_CHARS = 500


def keyword_score(text: str, keywords: dict, negatives: dict, cap: float):
    """Return (score, matched_terms). Positive matches capped; negatives applied after."""
    lower = text.lower()
    matched = []
    pos = 0.0
    for kw, w in keywords.items():
        if kw.lower() in lower:
            pos += float(w)
            matched.append(f"{kw}({w:+g})")
    pos = min(pos, cap)
    neg = 0.0
    for kw, w in (negatives or {}).items():
        if kw.lower() in lower:
            neg += float(w)
            matched.append(f"{kw}({w:+g})")
    return pos + neg, matched


def recency_factor(published_at: str, now_ts: float, half_life_hours: float) -> float:
    age_hours = max(0.0, (now_ts - parse_iso(published_at).timestamp()) / 3600.0)
    return 0.5 ** (age_hours / half_life_hours)


def run(conn: sqlite3.Connection) -> dict:
    cfg = load_config()
    dcfg = cfg["digest"]
    keywords = cfg.get("keywords", {})
    negatives = cfg.get("negative_keywords", {})
    cap = float(dcfg.get("keyword_score_cap", 3.0))
    half_life = float(dcfg["half_life_hours"])
    srcmap = source_map()
    now_ts = now_utc().timestamp()

    rows = conn.execute(
        "SELECT id, source_id, title, raw_text, published_at FROM items WHERE state='new'"
    ).fetchall()

    scored = 0
    for r in rows:
        src_weight = float(srcmap.get(r["source_id"], {}).get("weight", 1.0))
        match_text = f"{r['title']} {(r['raw_text'] or '')[:RAW_MATCH_CHARS]}"
        kw, matched = keyword_score(match_text, keywords, negatives, cap)
        rec = recency_factor(r["published_at"], now_ts, half_life)
        score = src_weight * (1 + kw) * rec

        why = f"src:{src_weight:g} kw:{kw:+g} rec:{rec:.2f}"
        if matched:
            why += " [" + ",".join(matched) + "]"
        conn.execute(
            "UPDATE items SET score = ?, score_why = ? WHERE id = ?",
            (score, why, r["id"]),
        )
        scored += 1
    conn.commit()
    return {"scored": scored}


def print_top(conn: sqlite3.Connection, limit: int = 12):
    rows = conn.execute(
        """
        SELECT title, source_id, score, score_why
        FROM items
        WHERE state='new' AND is_primary=1
        ORDER BY score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    print(f"\ntop {len(rows)} primary items by score:")
    for r in rows:
        print(f"  {r['score']:.3f}  [{r['source_id']}] {r['title'][:64]}")
        print(f"          {r['score_why']}")


if __name__ == "__main__":
    conn = init_db()
    stats = run(conn)
    print(f"score: {stats}")
    limit = load_config()["digest"]["max_items"]
    print_top(conn, limit)
