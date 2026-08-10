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
    {"name": "usd_vnd", "label": "USD/VND", "fmt": lambda v: f"{v:,.0f}"},
    {"name": "cny_vnd", "label": "CNY/VND", "fmt": lambda v: f"{v:,.0f}"},
    {"name": "hcmc_temp", "label": "Temp", "fmt": lambda v: f"{v:.0f}°C", "icon": "🌡"},
    {"name": "hcmc_rain_prob", "label": "Rain", "fmt": lambda v: f"{v:.0f}%", "icon": "🌧"},
    {"name": "hcmc_aqi", "label": "AQI", "fmt": lambda v: f"{v:.0f}", "kind": "aqi"},
    {"name": "hcmc_uv", "label": "UV", "fmt": lambda v: f"{v:.0f}", "kind": "uv", "icon": "☀️"},
    {"name": "sjc_gold_buy", "label": "Gold buy", "fmt": lambda v: f"{v:,.0f}"},
    {"name": "sjc_gold_sell", "label": "Gold sell", "fmt": lambda v: f"{v:,.0f}"},
    {"name": "sjc_gold_spread", "label": "Gold spread", "fmt": lambda v: f"{v:,.0f}"},
]
DEAD_FEED_THRESHOLD = 5

# US AQI categories with their standard (theme-independent) colors.
_AQI_BANDS = [
    (50, "Good", "#2fa36b"),
    (100, "Moderate", "#c9a227"),
    (150, "Unhealthy for sensitive", "#e8730c"),
    (200, "Unhealthy", "#d63b5b"),
    (300, "Very unhealthy", "#8b5cf6"),
    (10 ** 9, "Hazardous", "#7f1d1d"),
]


def _aqi_band(v: float) -> dict | None:
    for hi, label, color in _AQI_BANDS:
        if v <= hi:
            return {"label": label, "color": color}
    return None


# WHO UV index categories.
_UV_BANDS = [
    (2, "Low", "#2fa36b"),
    (5, "Moderate", "#c9a227"),
    (7, "High", "#e8730c"),
    (10, "Very high", "#d63b5b"),
    (10 ** 9, "Extreme", "#8b5cf6"),
]


def _uv_band(v: float) -> dict | None:
    for hi, label, color in _UV_BANDS:
        if v <= hi:
            return {"label": label, "color": color}
    return None


def _band_for(kind: str | None, value: float) -> dict | None:
    if kind == "aqi":
        return _aqi_band(value)
    if kind == "uv":
        return _uv_band(value)
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
        sub = None
        if spec["name"] == "hcmc_temp":
            hi = conn.execute(
                "SELECT value FROM metrics WHERE name='hcmc_temp_hi' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            lo = conn.execute(
                "SELECT value FROM metrics WHERE name='hcmc_temp_lo' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if hi and lo:
                sub = f"H {hi['value']:.0f}° · L {lo['value']:.0f}°"

        out.append(
            {
                "label": spec["label"],
                "value": spec["fmt"](latest),
                "icon": spec.get("icon"),
                "delta": delta,
                "sub": sub,
                "band": _band_for(spec.get("kind"), latest),
                "spark": _spark(values),
                "link": links.get(spec["name"]),
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

    newsletters = [r for r in candidates if is_newsletter(r)]
    news = [r for r in candidates if not is_newsletter(r)]

    # per-source cap (candidates are already score-sorted) so one high-volume
    # source can't crowd out low-volume ones you follow
    cap = int(dcfg.get("per_source_cap", 0) or 0)
    if cap:
        counts: dict[str, int] = {}
        capped = []
        for r in news:
            counts[r["source_id"]] = counts.get(r["source_id"], 0) + 1
            if counts[r["source_id"]] <= cap:
                capped.append(r)
        news = capped

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
            text, ref_ids = raw.get("summary"), raw.get("refs", [])
        else:  # legacy: plain string, no refs
            text, ref_ids = raw, []
        refs, n = [], 1
        for rid in ref_ids:
            v = id_map.get(rid)
            if v:
                refs.append({"n": n, "source_id": v["source_id"], "url": v["url"],
                             "title": v["title"]})
                n += 1
        return text, refs

    for sec in ordered_sections:
        text, refs = _summary_and_refs(sec["tag"])
        sec["summary_html"] = _linkify_summary(text, refs)
    _nl_text, _nl_refs = _summary_and_refs("newsletter")
    newsletter_summary_html = _linkify_summary(_nl_text, _nl_refs)

    rendered_ids = [r["id"] for r in top_news] + [r["id"] for r in newsletters]

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
        "trackers": _trackers(conn, cfg.get("tracker_links", {})),
        "sections": ordered_sections,
        "newsletters": newsletter_views,
        "newsletter_summary_html": newsletter_summary_html,
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
