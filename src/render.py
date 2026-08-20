"""Render the digest to a self-contained HTML page.

`select_digest()` is the shared selection logic (also used by notify.py):
trackers with sparklines, top items grouped by tag, a newsletter section with a
floor (always included), and a dead-feeds section. Timestamps are converted to
the configured timezone only here, at render time.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

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

# Trackers to display, in order. Each: name, label, fmt; optional icon/kind.
TRACKER_SPECS = [
    {"name": "usd_vnd", "label": "USD/VND", "fmt": lambda v: f"{v:,.0f}", "group": "Markets"},
    {"name": "sgd_vnd", "label": "SGD/VND", "fmt": lambda v: f"{v:,.0f}", "group": "Markets"},
    {"name": "cny_vnd", "label": "CNY/VND", "fmt": lambda v: f"{v:,.0f}", "group": "Markets"},
    {"name": "sjc_gold_buy", "label": "Gold buy", "fmt": lambda v: f"{v:,.0f}", "group": "Markets"},
    {"name": "sjc_gold_sell", "label": "Gold sell", "fmt": lambda v: f"{v:,.0f}", "group": "Markets"},
    {"name": "sjc_gold_spread", "label": "Gold spread", "fmt": lambda v: f"{v:,.0f}", "group": "Markets"},
    {"name": "hcmc_temp", "label": "Temp", "fmt": lambda v: f"{v:.0f}°C", "icon": "🌡", "group": "Environment"},
    {"name": "hcmc_rain_prob", "label": "Rain", "fmt": lambda v: f"{v:.0f}%", "icon": "🌧", "group": "Environment"},
]
DEAD_FEED_THRESHOLD = 5

# WMO weather codes -> (label, emoji).
_WMO = {
    0: ("Clear", "☀️"), 1: ("Mainly clear", "🌤"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "🌫"), 48: ("Rime fog", "🌫"),
    51: ("Light drizzle", "🌦"), 53: ("Drizzle", "🌦"), 55: ("Heavy drizzle", "🌦"),
    56: ("Freezing drizzle", "🌧"), 57: ("Freezing drizzle", "🌧"),
    61: ("Light rain", "🌦"), 63: ("Rain", "🌧"), 65: ("Heavy rain", "🌧"),
    66: ("Freezing rain", "🌧"), 67: ("Freezing rain", "🌧"),
    71: ("Light snow", "🌨"), 73: ("Snow", "🌨"), 75: ("Heavy snow", "🌨"),
    77: ("Snow grains", "🌨"), 80: ("Showers", "🌦"), 81: ("Showers", "🌧"),
    82: ("Violent showers", "⛈"), 85: ("Snow showers", "🌨"), 86: ("Snow showers", "🌨"),
    95: ("Thunderstorm", "⛈"), 96: ("Thunderstorm + hail", "⛈"), 99: ("Thunderstorm + hail", "⛈"),
}


def _wmo(code: int) -> dict:
    label, icon = _WMO.get(code, ("—", ""))
    return {"label": label, "icon": icon}


def _band_for(kind: str | None, value: float) -> dict | None:
    """Return a band dict for a tracker value, or None if no band applies."""
    if kind == "rain_prob" and value >= 70:
        return {"label": "Likely", "color": "#3b82f6"}
    return None


def humanize_age(published_at: str, now: datetime) -> str:
    secs = max(0, (now.timestamp() - parse_iso(published_at).timestamp()))
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _spark(values: list[float], w: int = 120, h: int = 32, pad: int = 3) -> dict | None:
    """Area + line + endpoint for a sparkline. None if <2 points."""
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = [
        (pad + (w - 2 * pad) * (i / (n - 1)), pad + (h - 2 * pad) * (1 - (v - lo) / span))
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pad},{h - pad} " + line + f" {w - pad},{h - pad}"
    return {"line": line, "area": area, "cx": f"{pts[-1][0]:.1f}",
            "cy": f"{pts[-1][1]:.1f}", "w": w, "h": h}


def _trackers(conn: sqlite3.Connection, links: dict | None = None) -> list[dict]:
    links = links or {}
    cutoff = now_utc().timestamp() - 30 * 86400
    out = []
    for spec in TRACKER_SPECS:
        rows = conn.execute(
            "SELECT ts, value FROM metrics WHERE name=? ORDER BY ts", (spec["name"],)
        ).fetchall()
        rows = [r for r in rows if parse_iso(r["ts"]).timestamp() >= cutoff]
        if not rows:
            continue
        values = [r["value"] for r in rows]
        latest = values[-1]
        delta = None
        if len(values) >= 2 and values[-2]:
            d = latest - values[-2]
            direction = "up" if d > 1e-9 else "down" if d < -1e-9 else "flat"
            arrow = {"up": "▲", "down": "▼", "flat": "·"}[direction]
            delta = {"pct": f"{abs(d) / abs(values[-2]) * 100:.1f}%",
                     "dir": direction, "arrow": arrow}
        name = spec["name"]
        sub = None
        cond = None
        if name == "hcmc_temp":
            hi = conn.execute(
                "SELECT value FROM metrics WHERE name='hcmc_temp_hi' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            lo = conn.execute(
                "SELECT value FROM metrics WHERE name='hcmc_temp_lo' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if hi and lo:
                sub = f"H {hi['value']:.0f}° · L {lo['value']:.0f}°"
            wc = conn.execute(
                "SELECT value FROM metrics WHERE name='hcmc_weathercode' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if wc is not None:
                cond = _wmo(int(wc["value"]))

        kind = spec.get("kind") or ("rain_prob" if name == "hcmc_rain_prob" else None)
        band = _band_for(kind, latest)

        # glanceable alert: emphasize the card only when it's actionable
        alert = None
        if name == "hcmc_rain_prob" and latest >= 70:
            alert = "#3b82f6"

        out.append(
            {
                "label": spec["label"],
                "value": spec["fmt"](latest),
                "icon": spec.get("icon"),
                "delta": delta,
                "sub": sub,
                "cond": cond,
                "band": band,
                "alert": alert,
                "spark": _spark(values),
                "link": links.get(spec["name"]),
                "group": spec.get("group", "Other"),
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


_CITE_RE = re.compile(r"\[(\d+)\]")


def _linkify_summary(text: str | None, refs: list[dict]):
    """Turn inline [n] citation markers into links to the referenced source.

    Returns a safe Markup fragment. Markers with no matching ref stay as plain
    text.
    """
    if not text:
        return None
    ref_by_n = {r["n"]: r for r in refs}
    esc = str(escape(text))  # escape the model text; [ and ] are left intact

    def repl(m):
        r = ref_by_n.get(int(m.group(1)))
        if not r:
            return m.group(0)
        url = escape(r["url"])
        tip = escape(f'{r["source_id"]} — {r["title"]}')
        return (f'<a href="{url}" target="_blank" rel="noopener" '
                f'title="{tip}">[{m.group(1)}]</a>')

    return Markup(_CITE_RE.sub(repl, esc))


def _resolve_refs(conn: sqlite3.Connection, ref_ids: list[str]) -> list[dict]:
    """Resolve a list of item ids into numbered ref dicts for _linkify_summary."""
    refs = []
    for n, item_id in enumerate(ref_ids, start=1):
        row = conn.execute(
            "SELECT source_id, title, url FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if row:
            refs.append({"n": n, "source_id": row["source_id"], "title": row["title"],
                         "url": row["url"]})
    return refs


def _daily_read_articles(conn: sqlite3.Connection) -> list[dict]:
    """Parse the stored daily_read JSON array and linkify each article's body.

    Returns a list of dicts with keys: heading (str), body_html (Markup), refs (list).
    Empty list if daily_read is absent or malformed.
    """
    try:
        raw = json.loads(get_kv(conn, "daily_read") or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    articles = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        heading = (item.get("heading") or "").strip()
        body = (item.get("body") or "").strip()
        ref_ids = item.get("refs") or []
        if not heading and not body:
            continue
        refs = _resolve_refs(conn, ref_ids)
        articles.append({
            "heading": heading,
            "body_html": _linkify_summary(body, refs),
            "refs": refs,
        })
    return articles


def _section_for(tags: list[str], order: list[str]) -> str:
    # release feeds go to their own compact section, regardless of other tags.
    if "releases" in tags:
        return "releases"
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
        "published_iso": r["published_at"],
        "is_new": (now.timestamp() - parse_iso(r["published_at"]).timestamp()) < 86400,
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

    def is_release(r) -> bool:
        return "releases" in srcmap.get(r["source_id"], {}).get("tags", [])

    def is_watching(r) -> bool:
        return "watching" in srcmap.get(r["source_id"], {}).get("tags", [])

    def per_source_cap(rows, cap):
        # rows are already score-sorted; keep at most `cap` per source so one
        # high-volume feed can't crowd out the rest.
        if not cap:
            return rows
        counts: dict[str, int] = {}
        out = []
        for r in rows:
            counts[r["source_id"]] = counts.get(r["source_id"], 0) + 1
            if counts[r["source_id"]] <= cap:
                out.append(r)
        return out

    cap = int(dcfg.get("per_source_cap", 0) or 0)
    newsletters = [r for r in candidates if is_newsletter(r)]
    # Watching is a queue, not a daily-news window. Keep the newest unclicked
    # videos from each channel until the browser records that they were opened.
    watching_cap = int(dcfg.get("watching_per_source_cap", 3))
    watching_max_age = float(dcfg.get("watching_max_age_days", 14)) * 86400
    watching_cutoff = now.timestamp() - watching_max_age
    watching_counts: dict[str, int] = {}
    watching = []
    for r in conn.execute(
        "SELECT * FROM items WHERE state='new' AND is_primary=1 "
        "ORDER BY published_at DESC"
    ).fetchall():
        if not is_watching(r):
            continue
        if parse_iso(r["published_at"]).timestamp() < watching_cutoff:
            continue
        source_id = r["source_id"]
        if watching_counts.get(source_id, 0) >= watching_cap:
            continue
        watching_counts[source_id] = watching_counts.get(source_id, 0) + 1
        watching.append(r)
    # releases get their own compact section + budget (keeps Airflow's provider
    # firehose out of Data Engineering / the max_items pool)
    releases = per_source_cap(
        [r for r in candidates if not is_newsletter(r) and not is_watching(r) and is_release(r)], cap
    )[: int(dcfg.get("release_max", 10))]
    news = per_source_cap(
        [r for r in candidates if not is_newsletter(r) and not is_watching(r) and not is_release(r)], cap
    )

    # hard cut for articles/news; newsletters + releases have their own budgets
    top_news = news[: int(dcfg["max_items"])]

    # floor for "pinned" sources you follow on purpose (YouTube channels, or any
    # source with `pin: true`): always surface their latest in-window items, even
    # if their score fell below the max_items cut.
    def is_pinned(r) -> bool:
        src = srcmap.get(r["source_id"], {})
        return bool(src.get("pin")) or src.get("type") == "youtube"

    # Pinned sources surface their latest item within a wider window (weekly-
    # posting channels shouldn't vanish between the 4-day render window).
    top_ids = {r["id"] for r in top_news}
    pin_cutoff = now.timestamp() - float(dcfg.get("pin_window_days", 21)) * 86400
    pin_count: dict[str, int] = {}
    for r in conn.execute(
        "SELECT * FROM items WHERE state='new' AND is_primary=1 ORDER BY published_at DESC"
    ).fetchall():
        if r["id"] in top_ids or is_watching(r) or not is_pinned(r):
            continue
        if parse_iso(r["published_at"]).timestamp() < pin_cutoff:
            continue
        if pin_count.get(r["source_id"], 0) >= 2:
            continue
        pin_count[r["source_id"]] = pin_count.get(r["source_id"], 0) + 1
        top_news.append(r)
        top_ids.add(r["id"])

    # group into sections (articles + releases; newsletters render separately)
    sections: dict[str, list] = {}
    for r in watching + top_news + releases:
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
            ordered_sections.append({"tag": sec, "entries": sections[sec]})
            seen.add(sec)
    for sec in sorted(k for k in sections if k not in seen):
        ordered_sections.append({"tag": sec, "entries": sections[sec]})

    newsletter_views = [
        _item_view(conn, r, now, srcmap)
        for r in sorted(newsletters, key=lambda x: x["published_at"], reverse=True)
    ]

    # Attach each section's LLM summary + its cited sources (as numbered links).
    id_map = {
        v["id"]: v
        for sec in ordered_sections for v in sec["entries"]
    }
    id_map.update({v["id"]: v for v in newsletter_views})

    def _summary_and_refs(tag):
        raw = sec_summaries.get(tag)
        if isinstance(raw, dict):
            text, ref_ids, tp = raw.get("summary"), raw.get("refs", []), raw.get("top_pick")
        else:  # legacy: plain string, no refs
            text, ref_ids, tp = raw, [], None
        refs, n = [], 1
        for rid in ref_ids:
            v = id_map.get(rid)
            # A cached summary can outlive the current render window. Resolve
            # those citations from the database too, rather than leaving [n]
            # as plain text when the cited entry is not visible today.
            if not v:
                row = conn.execute(
                    "SELECT id, source_id, title, url FROM items WHERE id=?", (rid,)
                ).fetchone()
                if row:
                    v = dict(row)
            if v:
                refs.append({"n": n, "source_id": v["source_id"], "url": v["url"],
                             "title": v["title"]})
                n += 1
        return text, refs, tp

    for sec in ordered_sections:
        text, refs, tp = _summary_and_refs(sec["tag"])
        sec["summary_html"] = _linkify_summary(text, refs)
        sec["top_pick"] = id_map.get(tp) if tp else None
    _nl_text, _nl_refs, _ = _summary_and_refs("newsletter")
    newsletter_summary_html = _linkify_summary(_nl_text, _nl_refs)

    rendered_ids = (
        [r["id"] for r in top_news]
        + [r["id"] for r in releases]
        + [r["id"] for r in newsletters]
    )

    # top 5 across everything shown, by score, for the Telegram message
    shown = top_news + newsletters
    top5 = [
        _item_view(conn, r, now, srcmap)
        for r in sorted(shown, key=lambda x: x["score"] or 0.0, reverse=True)[:5]
    ]

    return {
        "generated_local": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "generated_iso": now.isoformat(),
        "today_label": now_local.strftime("%a %d %b"),
        "overview": get_kv(conn, "overview"),
        "daily_read_articles": _daily_read_articles(conn),
        "daily_read_item_count": int(get_kv(conn, "daily_read_item_count", "0") or 0),
        "daily_read_pages": int(cfg.get("summarize", {}).get("daily_read_pages", 10)),
        "trackers": _trackers(conn, cfg.get("tracker_links", {})),
        "sections": ordered_sections,
        "newsletters": newsletter_views,
        "newsletter_summary_html": newsletter_summary_html,
        "dead_feeds": _dead_feeds(conn),
        "rendered_ids": rendered_ids,
        "top5": top5,
        "counts": {"news": len(top_news), "releases": len(releases),
                   "newsletters": len(newsletter_views)},
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
