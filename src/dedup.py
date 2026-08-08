"""Near-duplicate clustering (Level 2).

Level 1 (exact) already happened at ingest: every item's id is the sha256 of its
canonical URL, so the same link from two sources collapses to one row.

Level 2 merges the same story arriving under different URLs within a rolling
window: normalize titles, compare character-trigram Jaccard, single-link
cluster, and pick a representative. Deterministic, no embeddings.
"""
from __future__ import annotations

import re
import sqlite3

from .db import get_conn, init_db, load_config, now_utc, parse_iso, source_map

_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "is", "are", "was", "were", "be", "with", "by", "from", "as", "how", "why",
    "what", "new", "using", "use", "your", "you",
}
# Leading source-style prefixes: "HN: ", "[Blog] ", "Show HN: ", "Ask HN — ".
_PREFIX_RE = re.compile(r"^\s*(\[[^\]]{1,20}\]|[A-Za-z][\w ]{0,15}:)\s*")
_NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    t = title or ""
    # strip one leading bracket/prefix token
    t = _PREFIX_RE.sub("", t, count=1)
    t = t.lower()
    t = _NONWORD_RE.sub(" ", t)
    tokens = [w for w in t.split() if w not in _STOPWORDS]
    return _WS_RE.sub(" ", " ".join(tokens)).strip()


def trigrams(s: str) -> set[str]:
    s = s.replace(" ", "")
    if len(s) < 3:
        return {s} if s else set()
    return {s[i : i + 3] for i in range(len(s) - 2)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def run(conn: sqlite3.Connection) -> dict:
    cfg = load_config()["digest"]
    threshold = float(cfg["dedup_threshold"])
    window_hours = float(cfg["window_hours"])
    srcmap = source_map()

    # 1) Reset every undigested item to its own singleton cluster.
    conn.execute(
        "UPDATE items SET cluster_id = id, is_primary = 1 WHERE state = 'new'"
    )

    # 2) Cluster within the rolling window.
    cutoff = now_utc().timestamp() - window_hours * 3600
    rows = [
        r
        for r in conn.execute(
            "SELECT id, source_id, title, raw_text, published_at FROM items WHERE state='new'"
        ).fetchall()
        if parse_iso(r["published_at"]).timestamp() >= cutoff
    ]

    n = len(rows)
    grams = [trigrams(normalize_title(r["title"])) for r in rows]
    uf = _UnionFind(n)
    comparisons = 0
    for i in range(n):
        for j in range(i + 1, n):
            comparisons += 1
            if jaccard(grams[i], grams[j]) >= threshold:
                uf.union(i, j)

    # group indices by cluster root
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)

    def weight(idx: int) -> float:
        return float(srcmap.get(rows[idx]["source_id"], {}).get("weight", 1.0))

    def rep_key(idx: int):
        return (weight(idx), len(rows[idx]["raw_text"] or ""))

    merged = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        merged += 1
        rep = max(members, key=rep_key)
        cluster_id = rows[rep]["id"]
        for idx in members:
            conn.execute(
                "UPDATE items SET cluster_id = ?, is_primary = ? WHERE id = ?",
                (cluster_id, 1 if idx == rep else 0, rows[idx]["id"]),
            )
    conn.commit()

    return {
        "window_items": n,
        "comparisons": comparisons,
        "clusters_merged": merged,
        "threshold": threshold,
    }


def print_clusters(conn: sqlite3.Connection):
    """Eyeball helper: show every multi-member cluster and its members."""
    rows = conn.execute(
        """
        SELECT cluster_id, id, source_id, is_primary, title
        FROM items
        WHERE state='new' AND cluster_id IN (
            SELECT cluster_id FROM items WHERE state='new'
            GROUP BY cluster_id HAVING COUNT(*) > 1
        )
        ORDER BY cluster_id, is_primary DESC
        """
    ).fetchall()
    if not rows:
        print("  (no multi-member clusters in the current window)")
        return
    current = None
    for r in rows:
        if r["cluster_id"] != current:
            current = r["cluster_id"]
            print(f"\ncluster {current[:10]}:")
        mark = "*" if r["is_primary"] else " "
        print(f"  {mark} [{r['source_id']}] {r['title'][:80]}")


if __name__ == "__main__":
    conn = init_db()
    stats = run(conn)
    print(f"dedup: {stats}")
    print_clusters(conn)
