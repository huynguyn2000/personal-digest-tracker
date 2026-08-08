"""Fetch RSS / Atom / YouTube / GitHub-releases feeds into `items`.

All three "feed-shaped" source types share this code path; they differ only in
how the URL is built and how the renderer badges them.

Guarantees:
  - conditional GET via stored etag / last-modified (skip work on 304)
  - INSERT OR IGNORE on id -> idempotent, never resurrects a digested item
  - one bad feed is recorded and skipped, never fatal
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup

from .db import (
    canonicalize_url,
    get_conn,
    init_db,
    now_iso,
    now_utc,
    sha256,
    source_map,
    to_iso,
)

FEED_TYPES = {"rss", "youtube", "github"}
USER_AGENT = "personal-digest-tracker/1.0 (+https://github.com/)"
RAW_TEXT_MAX = 2000


def feed_url(src: dict) -> str:
    if src["type"] == "youtube":
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={src['channel_id']}"
    return src["url"]


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return text[:RAW_TEXT_MAX]


def _entry_published(entry, now: datetime) -> str:
    """published_parsed -> updated_parsed -> now; clamp future dates to now."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st is None:
        return to_iso(now)
    # feedparser normalizes *_parsed to UTC struct_time.
    dt = datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    if dt > now:
        dt = now
    return to_iso(dt)


def _feed_state(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM feed_state WHERE source_id = ?", (source_id,)
    ).fetchone()


def _record_ok(conn, source_id, etag, last_modified):
    conn.execute(
        """
        INSERT INTO feed_state (source_id, etag, last_modified, last_ok_at,
                                last_error, error_count)
        VALUES (?, ?, ?, ?, NULL, 0)
        ON CONFLICT(source_id) DO UPDATE SET
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            last_ok_at = excluded.last_ok_at,
            last_error = NULL,
            error_count = 0
        """,
        (source_id, etag, last_modified, now_iso()),
    )


def _record_error(conn, source_id, err: str):
    conn.execute(
        """
        INSERT INTO feed_state (source_id, last_error, error_count)
        VALUES (?, ?, 1)
        ON CONFLICT(source_id) DO UPDATE SET
            last_error = excluded.last_error,
            error_count = feed_state.error_count + 1
        """,
        (source_id, err[:500]),
    )


def _upsert_entry(conn, src, entry, now) -> bool:
    link = entry.get("link") or ""
    canon = canonicalize_url(link)
    title = (entry.get("title") or "").strip()
    if not canon or not title:
        return False
    item_id = sha256(canon)
    raw = _strip_html(
        entry.get("summary")
        or (entry.get("content", [{}])[0].get("value") if entry.get("content") else "")
    )
    source_type = src["type"]
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO items
            (id, source_id, source_type, title, url, author, published_at,
             fetched_at, raw_text, is_primary, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'new')
        """,
        (
            item_id,
            src["id"],
            source_type,
            title,
            canon,
            entry.get("author"),
            _entry_published(entry, now),
            to_iso(now),
            raw,
        ),
    )
    return cur.rowcount > 0


def fetch_source(conn: sqlite3.Connection, src: dict) -> dict:
    """Fetch one source. Returns a small status dict; never raises."""
    source_id = src["id"]
    now = now_utc()
    try:
        state = _feed_state(conn, source_id)
        headers = {"User-Agent": USER_AGENT}
        if state:
            if state["etag"]:
                headers["If-None-Match"] = state["etag"]
            if state["last_modified"]:
                headers["If-Modified-Since"] = state["last_modified"]

        resp = httpx.get(
            feed_url(src), headers=headers, timeout=30.0, follow_redirects=True
        )
        if resp.status_code == 304:
            _record_ok(conn, source_id, headers.get("If-None-Match"),
                       headers.get("If-Modified-Since"))
            conn.commit()
            return {"id": source_id, "status": "304", "new": 0}

        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        # feedparser sets bozo on malformed feeds but often still parses entries;
        # only treat it as an error if we got nothing usable.
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"unparseable feed: {parsed.bozo_exception!r}")

        added = sum(_upsert_entry(conn, src, e, now) for e in parsed.entries)
        _record_ok(
            conn, source_id,
            resp.headers.get("ETag"),
            resp.headers.get("Last-Modified"),
        )
        conn.commit()
        return {"id": source_id, "status": str(resp.status_code),
                "entries": len(parsed.entries), "new": added}
    except Exception as exc:  # noqa: BLE001 - one feed must not kill the run
        _record_error(conn, source_id, f"{type(exc).__name__}: {exc}")
        conn.commit()
        return {"id": source_id, "status": "error", "error": str(exc)}


def run(conn: sqlite3.Connection) -> list[dict]:
    sources = [s for s in source_map().values() if s["type"] in FEED_TYPES]
    results = []
    for src in sources:
        res = fetch_source(conn, src)
        results.append(res)
        detail = res.get("error") or f"{res.get('new', 0)} new / {res.get('entries', '-')} entries"
        print(f"  [{res['status']:>5}] {res['id']}: {detail}")
    return results


if __name__ == "__main__":
    conn = init_db()
    print("fetch_rss: fetching feed sources...")
    run(conn)
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"items in db: {total}")
