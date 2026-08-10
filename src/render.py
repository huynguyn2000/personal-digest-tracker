"""Render the digest to a self-contained HTML page.

`select_digest()` is the shared selection logic (also used by notify.py):
trackers with sparklines, top items grouped by tag, a newsletter section with a
floor (always included), and a dead-feeds section. Timestamps are converted to
the configured timezone only here, at render time.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import (
    OUT_DIR,
    ROOT,
    get_conn,
    get_kv,
    init_db,
    load_config,
    now_utc,
    parse_iso,
    source_map,
)

# Trackers to display, in order: (metric_name, label, formatter)
TRACKER_SPECS = [
    ("usd_vnd", "USD/VND", lambda v: f"{v:,.0f}"),
    ("cny_vnd", "CNY/VND", lambda v: f"{v:,.0f}"),
    ("hcmc_temp", "Temp", lambda v: f"{v:.0f}°C"),
    ("hcmc_rain_prob", "Rain", lambda v: f"{v:.0f}%"),
    ("hcmc_aqi", "AQI", lambda v: f"{v:.0f}"),
    ("sjc_gold_buy", "Gold buy", lambda v: f"{v:,.0f}"),
    ("sjc_gold_sell", "Gold sell", lambda v: f"{v:,.0f}"),
    ("sjc_gold_spread", "Gold spread", lambda v: f"{v:,.0f}"),
]
DEAD_FEED_THRESHOLD = 5


def humanize_age(published_at: str, now: datetime) -> str:
    secs = max(0, (now.timestamp() - parse_iso(published_at).timestamp()))
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def sparkline_points(values: list[float], w: int = 110, h: int = 26, pad: int = 3) -> str:
    """Hand-rolled <polyline> points from a value series. Empty if <2 points."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _trackers(conn: sqlite3.Connection, links: dict | None = None) -> list[dict]:
    links = links or {}
    cutoff = now_utc().timestamp() - 30 * 86400
    out = []
    for name, label, fmt in TRACKER_SPECS:
        rows = conn.execute(
            "SELECT ts, value FROM metrics WHERE name=? ORDER BY ts", (name,)
        ).fetchall()
        rows = [r for r in rows if parse_iso(r["ts"]).timestamp() >= cutoff]
        if not rows:
            continue
        values = [r["value"] for r in rows]
        out.append(
            {
                "label": label,
                "value": fmt(values[-1]),
                "spark": sparkline_points(values),
                "link": links.get(name),
            }
        )
    return out


def _dead_feeds(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT source_id, last_error, error_count FROM feed_state "
        "WHERE error_count >= ? ORDER BY error_count DESC",
        (DEAD_FEED_THRESHOLD,),
    ).fetchall()
    return [dict(r) for r in rows]


def _section_for(tags: list[str], order: list[str]) -> str:
    for t in order:
        if t in tags:
            return t
    non_meta = [t for t in tags if t not in ("newsletter", "releases")]
    return non_meta[0] if non_meta else (tags[0] if tags else "other")


def _siblings(conn: sqlite3.Connection, item) -> list[str]:
    if not item["cluster_id"] or item["cluster_id"] == item["id"]:
        return []
    rows = conn.execute(
        "SELECT DISTINCT source_id FROM items WHERE cluster_id=? AND id!=?",
        (item["cluster_id"], item["id"]),
    ).fetchall()
    return [r["source_id"] for r in rows]


def _item_view(conn, r, now, srcmap) -> dict:
    src = srcmap.get(r["source_id"], {})
    snippet = (r["raw_text"] or "").strip()
    keys = r.keys()
    return {
        "id": r["id"],
        "title": r["title"],
        "url": r["url"],
        "source_id": r["source_id"],
        "source_type": r["source_type"],
        "age": humanize_age(r["published_at"], now),
        "summary": (r["summary"] if "summary" in keys else None),
        "snippet": snippet[:200] + ("…" if len(snippet) > 200 else ""),
        "siblings": _siblings(conn, r),
        "score": r["score"] or 0.0,
        "tags": src.get("tags", []),
    }


def select_digest(conn: sqlite3.Connection) -> dict:
    cfg = load_config()
    dcfg = cfg["digest"]
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = now_utc()
    now_local = now.astimezone(tz)
    srcmap = source_map()
    order = dcfg.get("section_order", [])
    window_cutoff = now.timestamp() - float(dcfg.get("render_window_days", 4)) * 86400

    candidates = [
        r
        for r in conn.execute(
            "SELECT * FROM items WHERE state='new' AND is_primary=1 ORDER BY score DESC"
        ).fetchall()
        if parse_iso(r["published_at"]).timestamp() >= window_cutoff
    ]

    def is_newsletter(r) -> bool:
        return r["source_type"] == "imap" or "newsletter" in srcmap.get(
            r["source_id"], {}
        ).get("tags", [])

    newsletters = [r for r in candidates if is_newsletter(r)]
    news = [r for r in candidates if not is_newsletter(r)]

    # hard cut for scored news; newsletters get a floor (always included)
    top_news = news[: int(dcfg["max_items"])]

    # group into sections
    sections: dict[str, list] = {}
    for r in top_news:
        sec = _section_for(srcmap.get(r["source_id"], {}).get("tags", []), order)
        sections.setdefault(sec, []).append(_item_view(conn, r, now, srcmap))

    try:
        sec_summaries = json.loads(get_kv(conn, "section_summaries") or "{}")
    except (ValueError, TypeError):
        sec_summaries = {}

    ordered_sections = []
    seen = set()
    for sec in order:
        if sec in sections:
            ordered_sections.append(
                {"tag": sec, "entries": sections[sec], "summary": sec_summaries.get(sec)}
            )
            seen.add(sec)
    for sec in sorted(k for k in sections if k not in seen):
        ordered_sections.append(
            {"tag": sec, "entries": sections[sec], "summary": sec_summaries.get(sec)}
        )

    newsletter_views = [
        _item_view(conn, r, now, srcmap)
        for r in sorted(newsletters, key=lambda x: x["published_at"], reverse=True)
    ]

    rendered_ids = [r["id"] for r in top_news] + [r["id"] for r in newsletters]

    # top 5 across everything shown, by score, for the Telegram message
    shown = top_news + newsletters
    top5 = [
        _item_view(conn, r, now, srcmap)
        for r in sorted(shown, key=lambda x: x["score"] or 0.0, reverse=True)[:5]
    ]

    return {
        "generated_local": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "overview": get_kv(conn, "overview"),
        "trackers": _trackers(conn, cfg.get("tracker_links", {})),
        "sections": ordered_sections,
        "newsletters": newsletter_views,
        "newsletter_summary": sec_summaries.get("newsletter"),
        "dead_feeds": _dead_feeds(conn),
        "rendered_ids": rendered_ids,
        "top5": top5,
        "counts": {"news": len(top_news), "newsletters": len(newsletter_views)},
    }


def render_html(payload: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    return env.get_template("digest.html.j2").render(**payload)


def run(conn: sqlite3.Connection) -> dict:
    payload = select_digest(conn)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    html = render_html(payload)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")
    # A machine-readable copy for debugging (git-ignored).
    (OUT_DIR / "digest.json").write_text(
        json.dumps({k: v for k, v in payload.items() if k != "rendered_ids"},
                   indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"render: wrote out/index.html "
        f"({payload['counts']['news']} news, {payload['counts']['newsletters']} newsletters, "
        f"{len(payload['trackers'])} trackers, {len(payload['dead_feeds'])} dead feeds)"
    )
    return payload


if __name__ == "__main__":
    conn = init_db()
    run(conn)
