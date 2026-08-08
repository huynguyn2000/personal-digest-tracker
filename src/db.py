"""Schema init, connection helper, and small shared utilities.

Everything else in the pipeline imports from here: the DB connection, config /
feeds loading, URL canonicalization (the basis of the primary key and Level-1
dedup), and UTC time helpers.
"""
from __future__ import annotations

import hashlib
import pathlib
import sqlite3
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
DB_PATH = DATA_DIR / "digest.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL,
  source_type   TEXT NOT NULL,
  title         TEXT NOT NULL,
  url           TEXT NOT NULL,
  author        TEXT,
  published_at  TEXT NOT NULL,
  fetched_at    TEXT NOT NULL,
  raw_text      TEXT,
  cluster_id    TEXT,
  is_primary    INTEGER DEFAULT 0,
  score         REAL,
  score_why     TEXT,
  state         TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_items_pub   ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_state ON items(state);

CREATE TABLE IF NOT EXISTS feed_state (
  source_id     TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  last_ok_at    TEXT,
  last_error    TEXT,
  error_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
  name    TEXT NOT NULL,
  ts      TEXT NOT NULL,
  value   REAL NOT NULL,
  meta    TEXT,
  PRIMARY KEY (name, ts)
);
"""


# --- connection / schema ---------------------------------------------------

def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    own = conn is None
    if own:
        conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- config / feeds --------------------------------------------------------

def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_feeds() -> dict:
    with open(ROOT / "feeds.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def source_map() -> dict[str, dict]:
    """source_id -> normalized source dict (weight/tags defaults applied)."""
    feeds = load_feeds()
    default_weight = (feeds.get("defaults") or {}).get("weight", 1.0)
    out: dict[str, dict] = {}
    for src in feeds.get("sources") or []:
        s = dict(src)
        s.setdefault("weight", default_weight)
        s.setdefault("tags", [])
        out[s["id"]] = s
    return out


# --- time ------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_utc().isoformat()


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- ids / urls ------------------------------------------------------------

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


_DROP_PARAM_PREFIXES = ("utm_",)
_DROP_PARAMS = {"ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"}


def canonicalize_url(url: str | None) -> str:
    """Normalize a URL so the same resource hashes to one id (Level-1 dedup).

    lowercase scheme+host, strip www., drop tracking params, strip trailing
    slash + fragment, and rewrite youtu.be/X -> youtube.com/watch?v=X.
    """
    if not url:
        return url or ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path
    query = [
        (k, v)
        for (k, v) in parse_qsl(parts.query, keep_blank_values=False)
        if k not in _DROP_PARAMS and not k.startswith(_DROP_PARAM_PREFIXES)
    ]

    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        host, path = "youtube.com", "/watch"
        query = [("v", vid)] + [(k, v) for (k, v) in query if k != "v"]

    # For a youtube watch URL, the v param is the only identity that matters.
    if host in ("youtube.com", "m.youtube.com") and path == "/watch":
        host = "youtube.com"
        query = [(k, v) for (k, v) in query if k == "v"]

    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, host, path, urlencode(query), ""))


if __name__ == "__main__":
    conn = init_db()
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    print(f"digest.db initialized at {DB_PATH}")
    print("tables:", ", ".join(tables))
