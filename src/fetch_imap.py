"""Fetch Gmail newsletters via IMAP into `items`.

No OAuth: uses an app password (account must have 2FA on). Gmail labels are IMAP
folders; nested labels use '/'. We search SINCE the last 3 days and rely on the
id primary key for dedup rather than mutating read/unseen state.
"""
from __future__ import annotations

import email
import imaplib
import os
import sqlite3
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from .db import get_conn, init_db, now_iso, now_utc, sha256, source_map, to_iso

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SINCE_DAYS = 3
RAW_TEXT_MAX = 4000  # newsletters are long; keep a bit more than feed items


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - malformed headers happen
        return value


def _body_text(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to text/html stripped. Decode defensively."""
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain" and plain is None:
                plain = _part_text(part)
            elif ctype == "text/html" and html is None:
                html = _part_text(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _part_text(msg)
        else:
            plain = _part_text(msg)

    if plain and plain.strip():
        text = plain
    elif html:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    else:
        text = ""
    return " ".join(text.split())[:RAW_TEXT_MAX]


def _part_text(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)  # handles base64 / quoted-printable
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _gmail_search_url(message_id: str) -> str:
    from urllib.parse import quote

    return "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{message_id}")


def _upsert_message(conn, src, raw_bytes, now) -> bool:
    msg = email.message_from_bytes(raw_bytes)
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        return False
    item_id = sha256(message_id)
    subject = _decode(msg.get("Subject")) or "(no subject)"
    author = _decode(msg.get("From"))

    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        published = to_iso(dt) if dt else to_iso(now)
    except (TypeError, ValueError):
        published = to_iso(now)
    # Clamp future dates, like the feed path.
    if published > to_iso(now):
        published = to_iso(now)

    body = _body_text(msg)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO items
            (id, source_id, source_type, title, url, author, published_at,
             fetched_at, raw_text, is_primary, state)
        VALUES (?, ?, 'imap', ?, ?, ?, ?, ?, ?, 0, 'new')
        """,
        (
            item_id, src["id"], subject, _gmail_search_url(message_id), author,
            published, to_iso(now), body,
        ),
    )
    return cur.rowcount > 0


def _select_label(imap: imaplib.IMAP4_SSL, label: str):
    # Quote the mailbox name so labels with spaces / nesting work.
    typ, _ = imap.select(f'"{label}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"cannot select label {label!r}")


def run(conn: sqlite3.Connection) -> list[dict]:
    sources = [s for s in source_map().values() if s["type"] == "imap"]
    if not sources:
        print("  (no imap sources configured)")
        return []

    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        print("  GMAIL_USER / GMAIL_APP_PASSWORD not set -> skipping imap sources")
        return [{"id": s["id"], "status": "skipped"} for s in sources]

    now = now_utc()
    # IMAP SINCE is date-granular (e.g. 06-Aug-2026) in the server's local time.
    since_date = (now - timedelta(days=SINCE_DAYS)).strftime("%d-%b-%Y")

    results = []
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(user, pw)
        for src in sources:
            try:
                _select_label(imap, src["label"])
                typ, data = imap.search(None, "SINCE", since_date)
                if typ != "OK":
                    raise RuntimeError("search failed")
                ids = data[0].split()
                added = 0
                for num in ids:
                    typ, msgdata = imap.fetch(num, "(RFC822)")
                    if typ != "OK" or not msgdata or not msgdata[0]:
                        continue
                    added += _upsert_message(conn, src, msgdata[0][1], now)
                conn.commit()
                results.append({"id": src["id"], "status": "ok",
                                "seen": len(ids), "new": added})
                print(f"  [   ok] {src['id']}: {added} new / {len(ids)} since {since_date}")
            except Exception as exc:  # noqa: BLE001
                conn.execute(
                    """INSERT INTO feed_state (source_id, last_error, error_count)
                       VALUES (?, ?, 1)
                       ON CONFLICT(source_id) DO UPDATE SET
                         last_error = excluded.last_error,
                         error_count = feed_state.error_count + 1""",
                    (src["id"], f"{type(exc).__name__}: {exc}"[:500]),
                )
                conn.commit()
                results.append({"id": src["id"], "status": "error", "error": str(exc)})
                print(f"  [error] {src['id']}: {exc}")
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return results


if __name__ == "__main__":
    conn = init_db()
    print("fetch_imap: fetching Gmail labels...")
    run(conn)
    n = conn.execute("SELECT COUNT(*) FROM items WHERE source_type='imap'").fetchone()[0]
    print(f"imap items in db: {n}")
