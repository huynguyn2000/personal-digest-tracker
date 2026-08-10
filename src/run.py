"""Orchestrator: fetch -> dedup -> score -> render -> notify -> mark digested.

The only entrypoint for a full run (`python -m src.run`). Fetch and notify
stages are best-effort: a failure there is logged and the run continues, because
a stale-but-rendered digest beats no digest. dedup/score/render are core.
"""
from __future__ import annotations

import sys

from . import (
    dashboard,
    dedup,
    fetch_imap,
    fetch_metrics,
    fetch_rss,
    notify,
    render,
    score,
    summarize,
)
from .db import init_db


def _stage(name: str, fn, *, fatal: bool):
    print(f"\n== {name} ==")
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  !! {name} failed: {type(exc).__name__}: {exc}")
        if fatal:
            raise
        return False


def main() -> int:
    conn = init_db()

    # --- fetch (best-effort) ---
    _stage("fetch_rss", lambda: fetch_rss.run(conn), fatal=False)
    _stage("fetch_imap", lambda: fetch_imap.run(conn), fatal=False)
    _stage("fetch_metrics", lambda: fetch_metrics.run(conn), fatal=False)

    # --- process (core) ---
    _stage("dedup", lambda: dedup.run(conn), fatal=True)
    _stage("score", lambda: score.run(conn), fatal=True)

    # --- summarize (best-effort; Gemini) ---
    _stage("summarize", lambda: summarize.run(conn), fatal=False)

    print("\n== render ==")
    payload = render.run(conn)

    # --- notify (best-effort) ---
    _stage("notify", lambda: notify.run(conn), fatal=False)

    # --- mark rendered items digested (idempotent; never resurrected) ---
    rendered_ids = payload.get("rendered_ids", [])
    if rendered_ids:
        conn.executemany(
            "UPDATE items SET state='digested' WHERE id=?",
            [(i,) for i in rendered_ids],
        )
        conn.commit()
    print(f"\n== done == marked {len(rendered_ids)} items digested")

    # --- dashboard (best-effort; reflects post-run state) ---
    _stage("dashboard", lambda: dashboard.run(conn), fatal=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
