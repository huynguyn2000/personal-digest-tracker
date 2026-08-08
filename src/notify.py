"""Send the short Telegram digest.

Top 5 items + a tracker one-liner + a link to the full HTML (GitHub Pages).
Designed to fit on one phone screen. Uses parse_mode=HTML with link previews
off. Skips cleanly if the bot token / chat id aren't configured.
"""
from __future__ import annotations

import html
import os
import sqlite3

import httpx

from .db import get_conn, init_db, load_config
from .render import TRACKER_SPECS, select_digest

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3900  # Telegram hard limit is 4096; leave headroom


def _tracker_line(conn: sqlite3.Connection) -> str:
    parts = []
    for name, label, fmt in TRACKER_SPECS:
        row = conn.execute(
            "SELECT value FROM metrics WHERE name=? ORDER BY ts DESC LIMIT 1", (name,)
        ).fetchone()
        if row is not None:
            parts.append(f"{label} {fmt(row['value'])}")
    return " · ".join(parts)


def build_message(conn: sqlite3.Connection) -> str:
    cfg = load_config()
    payload = select_digest(conn)
    pages_url = os.environ.get("PAGES_URL") or cfg.get("pages_url") or ""

    lines = [f"<b>Morning Digest</b> · {html.escape(payload['generated_local'])}"]
    tracker = _tracker_line(conn)
    if tracker:
        lines.append(html.escape(tracker))
    lines.append("")

    if not payload["top5"]:
        lines.append("<i>Nothing in the digest window today.</i>")
    for i, it in enumerate(payload["top5"], 1):
        title = html.escape(it["title"])
        if it["url"]:
            lines.append(f'{i}. <a href="{html.escape(it["url"])}">{title}</a>'
                         f' <i>({html.escape(it["source_id"])}, {it["age"]})</i>')
        else:
            lines.append(f'{i}. {title} <i>({html.escape(it["source_id"])}, {it["age"]})</i>')

    if pages_url:
        lines.append("")
        lines.append(f'<a href="{html.escape(pages_url)}">→ full digest</a>')

    msg = "\n".join(lines)
    return msg[:MAX_LEN]


def send(text: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -> skipping telegram")
        return {"status": "skipped"}
    resp = httpx.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    return {"status": "sent", "response": resp.json().get("ok")}


def run(conn: sqlite3.Connection) -> dict:
    text = build_message(conn)
    result = send(text)
    print(f"notify: {result['status']}")
    return result


if __name__ == "__main__":
    conn = init_db()
    print("--- message preview ---")
    print(build_message(conn))
    print("--- sending ---")
    run(conn)
