"""Enrich content-less items by fetching their official docs.

Some feeds emit items with no usable body — most notably GitHub *release* tags
(e.g. Apache Airflow provider releases whose Atom content is just "Release
2026-08-08 of providers"). There's nothing to summarize and the link leads to an
empty release page.

This step resolves such items to an authoritative source (the project's docs
changelog), fetches it, extracts the section for that version, and updates the
item's `raw_text` (so the gist/summary has material) AND its `url` (so the link
itself is useful). Best-effort and bounded; runs before score + summarize.

Resolvers are a small registry — add more projects as needed.
"""
from __future__ import annotations

import re
import sqlite3

import httpx
from bs4 import BeautifulSoup

from .db import get_conn, init_db, load_config, now_utc, parse_iso

UA = {"User-Agent": "personal-digest-tracker/1.0"}
TIMEOUT = 25.0
MAX_TEXT = 3000

# --- resolvers: (name, fn(row) -> (doc_url, version) | None) -----------------

_AIRFLOW_PROVIDER_RE = re.compile(r"^providers-([a-z0-9][a-z0-9-]*)/(\d[\w.]*)$")


def _airflow_provider(row) -> tuple[str, str] | None:
    """apache/airflow 'providers-<name>/<ver>' -> its docs changelog page."""
    if row["source_id"] != "airflow-releases":
        return None
    m = _AIRFLOW_PROVIDER_RE.match((row["title"] or "").strip())
    if not m:
        return None
    provider, version = m.group(1), m.group(2)
    if "rc" in version:  # release candidates aren't published to docs
        return None
    url = (
        f"https://airflow.apache.org/docs/apache-airflow-providers-{provider}/"
        f"{version}/changelog.html"
    )
    return url, version


RESOLVERS = [_airflow_provider]


def _resolve(row) -> tuple[str, str] | None:
    for fn in RESOLVERS:
        hit = fn(row)
        if hit:
            return hit
    return None


def _extract_changelog(html: str, version: str) -> str:
    """Pull the changelog text for `version` from a Sphinx docs page."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one('div[role="main"], article, div.body') or soup
    # Sphinx slugifies the version heading id, e.g. "10.21.0" -> "id" anchors vary;
    # try the dashed form, then fall back to slicing around the version string.
    node = main.find(id=version.replace(".", "-")) or main.find(id=version)
    if node is not None:
        text = node.get_text(" ", strip=True)
    else:
        full = " ".join(main.get_text(" ", strip=True).split())
        i = full.find(version)
        text = full[i : i + 1800] if i >= 0 else full[:1800]
    text = " ".join(text.split())
    # Keep only the target version's block: cut at the next version heading
    # (docs list newest-first, each version heading is "X.Y.Z ¶").
    heads = [m.start() for m in re.finditer(r"\d+\.\d+\.\d+\s*¶", text)]
    if len(heads) >= 2:
        text = text[: heads[1]].strip()
    return text[:MAX_TEXT]


def run(conn: sqlite3.Connection) -> dict:
    cfg = load_config()
    thin = int(cfg.get("enrich", {}).get("thin_chars", 200))
    max_per_run = int(cfg.get("enrich", {}).get("max_per_run", 12))
    window_days = float(cfg["digest"].get("render_window_days", 4))
    cutoff = now_utc().timestamp() - window_days * 86400

    rows = conn.execute(
        "SELECT id, source_id, title, url, raw_text, published_at "
        "FROM items WHERE state='new' AND source_type='github'"
    ).fetchall()
    todo = [
        r
        for r in rows
        if len(r["raw_text"] or "") < thin
        and parse_iso(r["published_at"]).timestamp() >= cutoff
    ]

    enriched, attempted = 0, 0
    for r in todo:
        if attempted >= max_per_run:
            break
        hit = _resolve(r)
        if not hit:
            continue
        attempted += 1
        doc_url, version = hit
        try:
            resp = httpx.get(doc_url, headers=UA, timeout=TIMEOUT, follow_redirects=True)
            if resp.status_code != 200:
                print(f"  [{resp.status_code}] {r['title']} (no docs page)")
                continue
            text = _extract_changelog(resp.text, version)
            if len(text) > len(r["raw_text"] or ""):
                conn.execute(
                    "UPDATE items SET raw_text = ?, url = ? WHERE id = ?",
                    (text, doc_url, r["id"]),
                )
                enriched += 1
                print(f"  [ok] {r['title']} -> {len(text)} chars from docs")
        except Exception as exc:  # noqa: BLE001 - best-effort
            print(f"  [skip] {r['title']}: {type(exc).__name__}: {exc}")
    conn.commit()
    return {"candidates": len(todo), "attempted": attempted, "enriched": enriched}


if __name__ == "__main__":
    conn = init_db()
    print("enrich: fetching docs for content-less items...")
    print(run(conn))
