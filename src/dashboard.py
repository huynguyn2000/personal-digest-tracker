"""Generate a self-contained observability dashboard for the pipeline.

A dev/visualization tool (separate from the shipped digest): it snapshots the
live SQLite DB + config and renders one interactive HTML page — pipeline
health, sources, the item explorer, the actual digest selection, dedup
clusters, and the scoring config. No external assets; everything inlined.

    python -m src.dashboard          # -> out/dashboard.html
    python -m src.dashboard --inner  # also write out/_dashboard_inner.html
                                     #    (body-only, for publishing as an Artifact)
"""
from __future__ import annotations

import json
import sqlite3
import sys

from .db import OUT_DIR, get_conn, init_db, load_config, parse_iso, source_map
from .render import TRACKER_SPECS, humanize_age, select_digest
from .db import now_utc

DEAD_FEED_THRESHOLD = 5


def _collect(conn: sqlite3.Connection) -> dict:
    cfg = load_config()
    dcfg = cfg["digest"]
    srcmap = source_map()
    now = now_utc()

    item_counts = {
        r["source_id"]: r["n"]
        for r in conn.execute(
            "SELECT source_id, COUNT(*) n FROM items GROUP BY source_id"
        )
    }
    fs = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM feed_state")}

    # --- sources + health ---
    sources = []
    for sid, s in srcmap.items():
        st = fs.get(sid, {})
        ec = st.get("error_count", 0) or 0
        if ec >= DEAD_FEED_THRESHOLD:
            status = "dead"
        elif ec > 0:
            status = "degraded"
        elif st.get("last_ok_at"):
            status = "ok"
        else:
            status = "idle"
        sources.append(
            {
                "id": sid,
                "type": s["type"],
                "tags": s.get("tags", []),
                "weight": s.get("weight", 1.0),
                "item_count": item_counts.get(sid, 0),
                "error_count": ec,
                "last_ok_at": st.get("last_ok_at"),
                "last_error": st.get("last_error"),
                "status": status,
            }
        )
    sources.sort(key=lambda x: (x["status"] != "dead", -x["item_count"]))

    # --- items ---
    cluster_size = {
        r["cluster_id"]: r["n"]
        for r in conn.execute(
            "SELECT cluster_id, COUNT(*) n FROM items WHERE state='new' GROUP BY cluster_id"
        )
    }
    items = []
    for r in conn.execute("SELECT * FROM items ORDER BY score DESC").fetchall():
        snippet = (r["raw_text"] or "").strip()
        items.append(
            {
                "id": r["id"][:12],
                "source_id": r["source_id"],
                "type": r["source_type"],
                "title": r["title"],
                "url": r["url"],
                "age": humanize_age(r["published_at"], now),
                "published_at": r["published_at"],
                "score": round(r["score"], 3) if r["score"] is not None else None,
                "score_why": r["score_why"] or "",
                "state": r["state"],
                "is_primary": bool(r["is_primary"]),
                "cluster_size": cluster_size.get(r["cluster_id"], 1),
                "snippet": snippet[:180] + ("…" if len(snippet) > 180 else ""),
            }
        )

    # --- metrics (full history for charts) ---
    metrics = []
    for name, label, fmt in TRACKER_SPECS:
        rows = conn.execute(
            "SELECT ts, value FROM metrics WHERE name=? ORDER BY ts", (name,)
        ).fetchall()
        if not rows:
            continue
        vals = [r["value"] for r in rows]
        metrics.append(
            {
                "name": name,
                "label": label,
                "latest": fmt(vals[-1]),
                "points": vals,
                "n": len(vals),
            }
        )

    # --- the actual digest selection (reuse the real logic) ---
    digest = select_digest(conn)
    digest.pop("rendered_ids", None)

    # --- clusters (multi-member) ---
    clusters = []
    multi = [cid for cid, n in cluster_size.items() if n > 1]
    for cid in multi:
        members = [
            {
                "source_id": m["source_id"],
                "title": m["title"],
                "is_primary": bool(m["is_primary"]),
            }
            for m in conn.execute(
                "SELECT source_id, title, is_primary FROM items WHERE cluster_id=? AND state='new'",
                (cid,),
            )
        ]
        clusters.append({"cluster_id": cid[:10], "members": members})

    # --- scoring config ---
    scoring = {
        "formula": "score = source_weight × (1 + keyword_score) × recency_factor",
        "keywords": sorted(
            ([k, v] for k, v in cfg.get("keywords", {}).items()),
            key=lambda kv: -kv[1],
        ),
        "negatives": sorted(
            ([k, v] for k, v in cfg.get("negative_keywords", {}).items()),
            key=lambda kv: kv[1],
        ),
        "half_life_hours": dcfg["half_life_hours"],
        "dedup_threshold": dcfg["dedup_threshold"],
        "max_items": dcfg["max_items"],
        "window_hours": dcfg["window_hours"],
        "keyword_score_cap": dcfg.get("keyword_score_cap", 3.0),
    }

    n_ok = sum(1 for s in sources if s["status"] == "ok")
    n_dead = sum(1 for s in sources if s["status"] == "dead")
    n_new = sum(1 for i in items if i["state"] == "new")
    n_dig = sum(1 for i in items if i["state"] == "digested")
    n_scored = sum(1 for i in items if i["score"] is not None)
    n_merged = len(multi)
    digest_count = digest["counts"]["news"] + digest["counts"]["newsletters"]

    pipeline = [
        {"key": "fetch", "label": "Fetch", "value": len(items),
         "sub": f"{n_ok}/{len(sources)} feeds ok"},
        {"key": "dedup", "label": "Dedup", "value": len(cluster_size),
         "sub": f"{n_merged} merged"},
        {"key": "score", "label": "Score", "value": n_scored,
         "sub": "no LLM"},
        {"key": "render", "label": "Render", "value": digest_count,
         "sub": f"cap {scoring['max_items']}"},
        {"key": "notify", "label": "Notify", "value": "Telegram",
         "sub": "top 5"},
    ]

    summary = {
        "items_total": len(items),
        "sources_total": len(sources),
        "sources_ok": n_ok,
        "sources_dead": n_dead,
        "new": n_new,
        "digested": n_dig,
        "metrics_tracked": len(metrics),
        "clusters_merged": n_merged,
    }

    return {
        "generated": now.astimezone(
            __import__("zoneinfo").ZoneInfo(cfg.get("timezone", "UTC"))
        ).strftime("%Y-%m-%d %H:%M %Z"),
        "summary": summary,
        "pipeline": pipeline,
        "sources": sources,
        "items": items,
        "metrics": metrics,
        "digest": digest,
        "clusters": clusters,
        "scoring": scoring,
    }


def build(data: dict) -> tuple[str, str]:
    """Return (full_html_doc, inner_body_html)."""
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    inner = _INNER.replace("/*__DATA__*/null", payload)
    full = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>Pipeline Console — {data['generated']}</title>\n"
        "</head>\n<body>\n" + inner + "\n</body>\n</html>\n"
    )
    return full, inner


def run(conn: sqlite3.Connection, write_inner: bool = False) -> dict:
    data = _collect(conn)
    full, inner = build(data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "dashboard.html").write_text(full, encoding="utf-8")
    if write_inner:
        (OUT_DIR / "_dashboard_inner.html").write_text(inner, encoding="utf-8")
    print(
        f"dashboard: wrote out/dashboard.html "
        f"({data['summary']['items_total']} items, {data['summary']['sources_total']} sources, "
        f"{len(data['metrics'])} metrics, {data['summary']['clusters_merged']} merged clusters)"
    )
    return data


# The entire page (style + markup + script) as a body fragment. The JSON payload
# is injected in place of the `/*__DATA__*/null` token. Kept as one string so
# there are no Jinja/JS brace collisions.
_INNER = r"""
<style>
:root{
  --bg:#f4f6fa; --surface:#ffffff; --raised:#eef2f8; --border:#dfe6ef;
  --text:#111826; --muted:#5b6b80;
  --accent:#0e7fa6; --accent-ink:#0e7fa6; --accent-weak:rgba(14,127,166,.10);
  --ok:#0f9d6b; --warn:#c67a08; --crit:#d63b5b;
  --grid:rgba(17,24,38,.07); --shadow:0 1px 2px rgba(17,24,38,.06),0 8px 24px rgba(17,24,38,.05);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:"SF Mono","JetBrains Mono","Fira Code",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0a0d13; --surface:#121822; --raised:#19212e; --border:#26303f;
    --text:#e7ecf3; --muted:#8a97a8; --accent:#38cdf0; --accent-ink:#7fe0f6;
    --accent-weak:rgba(56,205,240,.12);
    --ok:#37d39b; --warn:#f4b740; --crit:#fb7185;
    --grid:rgba(231,236,243,.08); --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
  }
}
:root[data-theme="light"]{
  --bg:#f4f6fa; --surface:#ffffff; --raised:#eef2f8; --border:#dfe6ef;
  --text:#111826; --muted:#5b6b80; --accent:#0e7fa6; --accent-ink:#0e7fa6;
  --accent-weak:rgba(14,127,166,.10); --ok:#0f9d6b; --warn:#c67a08; --crit:#d63b5b;
  --grid:rgba(17,24,38,.07); --shadow:0 1px 2px rgba(17,24,38,.06),0 8px 24px rgba(17,24,38,.05);
}
:root[data-theme="dark"]{
  --bg:#0a0d13; --surface:#121822; --raised:#19212e; --border:#26303f;
  --text:#e7ecf3; --muted:#8a97a8; --accent:#38cdf0; --accent-ink:#7fe0f6;
  --accent-weak:rgba(56,205,240,.12); --ok:#37d39b; --warn:#f4b740; --crit:#fb7185;
  --grid:rgba(231,236,243,.08); --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
}
*{box-sizing:border-box;}
.pdt{background:var(--bg);color:var(--text);font-family:var(--sans);
  font-size:15px;line-height:1.5;min-height:100vh;
  -webkit-font-smoothing:antialiased;}
.pdt .wrap{max-width:1120px;margin:0 auto;padding:28px 20px 80px;}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;}
.pdt a{color:var(--accent-ink);text-decoration:none;}
.pdt a:hover{text-decoration:underline;}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px;}

/* topbar */
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:26px;}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;
  color:var(--muted);margin-bottom:8px;}
.topbar h1{font-size:26px;font-weight:680;margin:0;letter-spacing:-.01em;text-wrap:balance;}
.topbar h1 .dim{color:var(--muted);font-weight:400;}
.gen{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:6px;}
.theme{background:var(--surface);border:1px solid var(--border);color:var(--text);
  width:40px;height:40px;border-radius:10px;font-size:17px;cursor:pointer;box-shadow:var(--shadow);
  display:flex;align-items:center;justify-content:center;flex:none;}
.theme:hover{border-color:var(--accent);}

/* pipeline stepper */
.pipeline{display:flex;align-items:stretch;gap:0;background:var(--surface);border:1px solid var(--border);
  border-radius:14px;padding:8px;margin-bottom:22px;box-shadow:var(--shadow);overflow-x:auto;}
.stage{flex:1 1 0;min-width:120px;display:flex;flex-direction:column;gap:3px;padding:12px 14px;position:relative;}
.stage + .stage::before{content:"";position:absolute;left:-1px;top:50%;width:22px;height:2px;
  background:linear-gradient(90deg,transparent,var(--accent));transform:translate(-50%,-50%);}
.stage .st-label{font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);}
.stage .st-value{font-size:22px;font-weight:680;letter-spacing:-.01em;}
.stage .st-value.text{font-size:17px;}
.stage .st-sub{font-family:var(--mono);font-size:11px;color:var(--muted);}
.stage .dot{width:7px;height:7px;border-radius:50%;background:var(--accent);position:absolute;top:14px;right:14px;
  box-shadow:0 0 0 4px var(--accent-weak);}

/* summary cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:26px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow);}
.card .c-label{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);}
.card .c-value{font-size:28px;font-weight:700;letter-spacing:-.02em;margin-top:4px;}
.card .c-sub{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:2px;}
.card.crit{border-color:color-mix(in srgb,var(--crit) 45%,var(--border));}
.card.crit .c-value{color:var(--crit);}

/* section heads */
.sec-head{display:flex;align-items:baseline;gap:12px;margin:0 0 12px;}
.sec-head h2{font-size:14px;font-weight:640;letter-spacing:.02em;margin:0;text-transform:uppercase;}
.sec-head .hint{font-family:var(--mono);font-size:11px;color:var(--muted);}

/* trackers */
.tracker-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:30px;}
.tk{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow);}
.tk .tk-top{display:flex;justify-content:space-between;align-items:baseline;}
.tk .tk-label{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);}
.tk .tk-n{font-family:var(--mono);font-size:10px;color:var(--muted);}
.tk .tk-value{font-size:24px;font-weight:700;margin:2px 0 8px;letter-spacing:-.01em;}
.tk svg{display:block;width:100%;height:52px;overflow:visible;}

/* tabs */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:18px;overflow-x:auto;}
.tab{font-family:var(--mono);font-size:12.5px;letter-spacing:.04em;background:none;border:none;color:var(--muted);
  padding:10px 14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap;}
.tab:hover{color:var(--text);}
.tab[aria-selected="true"]{color:var(--accent-ink);border-bottom-color:var(--accent);}
.tab .cnt{color:var(--muted);font-size:11px;}

/* toolbar */
.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;align-items:center;}
.toolbar input,.toolbar select{font-family:var(--mono);font-size:12.5px;background:var(--surface);
  color:var(--text);border:1px solid var(--border);border-radius:8px;padding:7px 10px;}
.toolbar input{flex:1 1 220px;min-width:180px;}
.toolbar input::placeholder{color:var(--muted);}
.toolbar .grow{flex:1;}

/* item rows */
.rows{display:flex;flex-direction:column;}
.row{display:grid;grid-template-columns:64px 1fr 92px;gap:12px;padding:12px 4px;border-bottom:1px solid var(--border);align-items:start;}
.row:last-child{border-bottom:none;}
.row .r-state{display:flex;flex-direction:column;gap:5px;align-items:flex-start;}
.row .r-title{font-weight:600;font-size:14.5px;line-height:1.35;}
.row .r-title a{color:var(--text);}
.row .r-title a:hover{color:var(--accent-ink);}
.row .r-meta{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-top:3px;display:flex;flex-wrap:wrap;gap:8px;}
.row .r-why{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:5px;
  background:var(--raised);border:1px solid var(--border);border-radius:6px;padding:3px 7px;display:inline-block;}
.row .r-snip{font-size:12.5px;color:var(--muted);margin-top:5px;}
.r-score{text-align:right;font-family:var(--mono);}
.r-score .sv{font-size:15px;font-weight:680;}
.r-bar{height:4px;border-radius:3px;background:var(--raised);margin-top:5px;overflow:hidden;}
.r-bar > i{display:block;height:100%;background:var(--accent);border-radius:3px;}

/* badges + pills */
.badge{font-family:var(--mono);font-size:9.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  padding:2px 6px;border-radius:5px;color:#fff;white-space:nowrap;}
.badge.rss{background:#0e8fb0;} .badge.youtube{background:#d6455a;}
.badge.github{background:#7c66d6;} .badge.imap{background:#0f9d6b;}
.pill{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  padding:2px 8px;border-radius:999px;border:1px solid transparent;white-space:nowrap;}
.pill.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent);border-color:color-mix(in srgb,var(--ok) 30%,transparent);}
.pill.degraded{color:var(--warn);background:color-mix(in srgb,var(--warn) 14%,transparent);border-color:color-mix(in srgb,var(--warn) 32%,transparent);}
.pill.dead{color:var(--crit);background:color-mix(in srgb,var(--crit) 14%,transparent);border-color:color-mix(in srgb,var(--crit) 32%,transparent);}
.pill.idle{color:var(--muted);background:var(--raised);border-color:var(--border);}
.pill.new{color:var(--accent-ink);background:var(--accent-weak);border-color:color-mix(in srgb,var(--accent) 30%,transparent);}
.pill.digested{color:var(--muted);background:var(--raised);border-color:var(--border);}
.chip{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:999px;background:var(--raised);
  border:1px solid var(--border);color:var(--muted);}
.tags{display:flex;flex-wrap:wrap;gap:5px;}

/* tables */
.tbl{width:100%;border-collapse:collapse;font-size:13px;}
.tbl th{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  text-align:left;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);}
.tbl td{padding:11px 10px;border-bottom:1px solid var(--border);vertical-align:top;}
.tbl td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;}
.tbl tr:hover td{background:var(--raised);}
.err{font-family:var(--mono);font-size:11px;color:var(--crit);word-break:break-word;}

/* digest tab */
.digest-tag{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  border-bottom:1px solid var(--border);padding-bottom:6px;margin:20px 0 8px;}
.di{padding:9px 0;border-bottom:1px solid var(--border);}
.di:last-child{border-bottom:none;}
.di .di-t{font-weight:600;font-size:14px;}
.di .di-m{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-top:2px;}

/* clusters */
.cluster{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:10px;}
.cluster .cl-id{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:6px;}
.cluster .cl-m{padding:3px 0;font-size:13px;}
.cluster .cl-m.primary{font-weight:640;}

/* scoring */
.formula{font-family:var(--mono);font-size:13px;background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:14px 16px;margin-bottom:20px;color:var(--accent-ink);overflow-x:auto;box-shadow:var(--shadow);}
.kw-wrap{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:22px;}
.kw{font-family:var(--mono);font-size:12px;padding:5px 10px;border-radius:8px;border:1px solid var(--border);
  display:flex;gap:8px;align-items:center;background:var(--surface);}
.kw .w{font-weight:700;}
.kw.pos{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));}
.kw.pos .w{color:var(--accent-ink);}
.kw.neg{border-color:color-mix(in srgb,var(--crit) 35%,var(--border));}
.kw.neg .w{color:var(--crit);}
.knobs{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;}
.knob{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;}
.knob .k-l{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);}
.knob .k-v{font-family:var(--mono);font-size:20px;font-weight:700;margin-top:3px;}

.empty{color:var(--muted);font-size:13px;padding:24px;text-align:center;border:1px dashed var(--border);border-radius:10px;}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--border);
  font-family:var(--mono);font-size:11px;color:var(--muted);display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;}
.hide{display:none!important;}
@media (max-width:640px){
  .row{grid-template-columns:52px 1fr;}
  .r-score{grid-column:2;text-align:left;display:flex;gap:10px;align-items:center;}
  .r-bar{width:80px;margin-top:0;}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;}}
</style>

<div class="pdt">
  <div class="wrap">
    <header class="topbar">
      <div>
        <div class="eyebrow">Deterministic digest · pipeline console</div>
        <h1>Personal Digest <span class="dim">/ tracker</span></h1>
        <div class="gen" id="gen"></div>
      </div>
      <button class="theme" id="themeBtn" title="Toggle light / dark" aria-label="Toggle theme">◐</button>
    </header>

    <section class="pipeline" id="pipeline" aria-label="Pipeline stages"></section>
    <section class="cards" id="summary"></section>

    <div class="sec-head"><h2>Trackers</h2><span class="hint">history &middot; hand-rolled SVG, no chart lib</span></div>
    <section class="tracker-grid" id="trackers"></section>

    <nav class="tabs" id="tabs" role="tablist"></nav>
    <section id="panel"></section>

    <footer>
      <span>No LLM in the loop · deterministic scoring &amp; dedup</span>
      <span id="foot-gen"></span>
    </footer>
  </div>
</div>

<script>
const DATA = /*__DATA__*/null;
const $ = (s,r=document)=>r.querySelector(s);
const root = document.documentElement;

function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function elFrom(html){const t=document.createElement("template");t.innerHTML=html.trim();return t.content;}

/* ---- theme ---- */
function currentTheme(){return root.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light");}
function setTheme(t){root.setAttribute("data-theme",t);try{localStorage.setItem("pdt-theme",t);}catch(e){}drawAll();}
$("#themeBtn").addEventListener("click",()=>setTheme(currentTheme()==="dark"?"light":"dark"));
try{const s=localStorage.getItem("pdt-theme");if(s)root.setAttribute("data-theme",s);}catch(e){}

/* ---- charts ---- */
function accent(){return getComputedStyle(root).getPropertyValue("--accent").trim()||"#38cdf0";}
function areaChart(vals,w=260,h=52,pad=4){
  if(!vals||!vals.length) return "";
  if(vals.length===1){const cy=h/2;return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="${pad}" y1="${cy}" x2="${w-pad}" y2="${cy}" stroke="var(--grid)" stroke-width="1"/><circle cx="${w-pad}" cy="${cy}" r="3.5" fill="${accent()}"/></svg>`;}
  const lo=Math.min(...vals),hi=Math.max(...vals),span=(hi-lo)||1,n=vals.length;
  const X=i=>pad+(w-2*pad)*(i/(n-1));
  const Y=v=>pad+(h-2*pad)*(1-(v-lo)/span);
  let line=vals.map((v,i)=>`${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  let area=`${pad},${h-pad} `+line+` ${w-pad},${h-pad}`;
  const ex=X(n-1).toFixed(1),ey=Y(vals[n-1]).toFixed(1);
  const gid="g"+Math.round(X(0)+hi);
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${accent()}" stop-opacity="0.22"/>
      <stop offset="1" stop-color="${accent()}" stop-opacity="0"/></linearGradient></defs>
    <line x1="${pad}" y1="${(h-pad).toFixed(1)}" x2="${w-pad}" y2="${(h-pad).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>
    <polygon points="${area}" fill="url(#${gid})"/>
    <polyline points="${line}" fill="none" stroke="${accent()}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${ex}" cy="${ey}" r="3.2" fill="${accent()}"/></svg>`;
}

/* ---- header / pipeline / summary ---- */
function drawHeader(){$("#gen").textContent="snapshot · "+DATA.generated;$("#foot-gen").textContent=DATA.generated;}
function drawPipeline(){
  $("#pipeline").innerHTML = DATA.pipeline.map(s=>`
    <div class="stage">
      <span class="dot"></span>
      <span class="st-label">${esc(s.label)}</span>
      <span class="st-value ${typeof s.value==="string"?"text":""}">${esc(s.value)}</span>
      <span class="st-sub">${esc(s.sub)}</span>
    </div>`).join("");
}
function drawSummary(){
  const s=DATA.summary;
  const cards=[
    {l:"Items",v:s.items_total,sub:`${s.new} new · ${s.digested} digested`},
    {l:"Sources",v:s.sources_total,sub:`${s.sources_ok} ok`},
    {l:"Dead feeds",v:s.sources_dead,sub:s.sources_dead?"needs attention":"all healthy",crit:s.sources_dead>0},
    {l:"Merged clusters",v:s.clusters_merged,sub:"near-duplicates"},
    {l:"Trackers",v:s.metrics_tracked,sub:"live metrics"},
  ];
  $("#summary").innerHTML=cards.map(c=>`
    <div class="card ${c.crit?"crit":""}">
      <div class="c-label">${esc(c.l)}</div>
      <div class="c-value">${esc(c.v)}</div>
      <div class="c-sub">${esc(c.sub)}</div>
    </div>`).join("");
}
function drawTrackers(){
  const g=$("#trackers");
  if(!DATA.metrics.length){g.innerHTML='<div class="empty">No metrics yet — run fetch_metrics.</div>';return;}
  g.innerHTML=DATA.metrics.map(m=>`
    <div class="tk">
      <div class="tk-top"><span class="tk-label">${esc(m.label)}</span><span class="tk-n">${m.n} pt${m.n>1?"s":""}</span></div>
      <div class="tk-value">${esc(m.latest)}</div>
      ${areaChart(m.points)}
    </div>`).join("");
}

/* ---- tabs ---- */
const TABS=[
  {k:"items",l:"Items",c:()=>DATA.items.length},
  {k:"sources",l:"Sources",c:()=>DATA.sources.length},
  {k:"digest",l:"Digest",c:()=>DATA.digest.counts.news+DATA.digest.counts.newsletters},
  {k:"clusters",l:"Clusters",c:()=>DATA.clusters.length},
  {k:"scoring",l:"Scoring",c:()=>DATA.scoring.keywords.length},
];
let activeTab="items";
function drawTabs(){
  $("#tabs").innerHTML=TABS.map(t=>`<button class="tab" role="tab" data-k="${t.k}"
     aria-selected="${t.k===activeTab}">${esc(t.l)} <span class="cnt">${t.c()}</span></button>`).join("");
  $("#tabs").querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{activeTab=b.dataset.k;drawTabs();drawPanel();}));
}
function drawPanel(){({items:panelItems,sources:panelSources,digest:panelDigest,clusters:panelClusters,scoring:panelScoring})[activeTab]();}

/* ---- items ---- */
let itemState={q:"",type:"",state:"",source:"",sort:"score"};
const maxScore=Math.max(1e-9,...DATA.items.map(i=>i.score||0));
function panelItems(){
  const sources=[...new Set(DATA.items.map(i=>i.source_id))].sort();
  const types=[...new Set(DATA.items.map(i=>i.type))].sort();
  const states=[...new Set(DATA.items.map(i=>i.state))].sort();
  $("#panel").innerHTML=`
    <div class="toolbar">
      <input id="q" type="search" placeholder="Search title, source, or score reason…" value="${esc(itemState.q)}">
      <select id="f-type"><option value="">all types</option>${types.map(t=>`<option ${t===itemState.type?"selected":""}>${t}</option>`).join("")}</select>
      <select id="f-state"><option value="">any state</option>${states.map(t=>`<option ${t===itemState.state?"selected":""}>${t}</option>`).join("")}</select>
      <select id="f-source"><option value="">all sources</option>${sources.map(t=>`<option ${t===itemState.source?"selected":""}>${t}</option>`).join("")}</select>
      <select id="f-sort">
        <option value="score" ${itemState.sort==="score"?"selected":""}>sort: score ↓</option>
        <option value="score-asc" ${itemState.sort==="score-asc"?"selected":""}>sort: score ↑</option>
        <option value="age" ${itemState.sort==="age"?"selected":""}>sort: newest</option>
      </select>
    </div>
    <div class="rows" id="rows"></div>`;
  const bind=(id,key)=>{const e=$("#"+id);const ev=e.tagName==="INPUT"?"input":"change";e.addEventListener(ev,()=>{itemState[key]=e.value;renderRows();});};
  bind("q","q");bind("f-type","type");bind("f-state","state");bind("f-source","source");bind("f-sort","sort");
  renderRows();
}
function renderRows(){
  let list=DATA.items.filter(i=>{
    if(itemState.type&&i.type!==itemState.type)return false;
    if(itemState.state&&i.state!==itemState.state)return false;
    if(itemState.source&&i.source_id!==itemState.source)return false;
    if(itemState.q){const q=itemState.q.toLowerCase();
      if(!((i.title||"").toLowerCase().includes(q)||(i.source_id||"").toLowerCase().includes(q)||(i.score_why||"").toLowerCase().includes(q)))return false;}
    return true;
  });
  if(itemState.sort==="score")list=list.slice().sort((a,b)=>(b.score||0)-(a.score||0));
  else if(itemState.sort==="score-asc")list=list.slice().sort((a,b)=>(a.score||0)-(b.score||0));
  else if(itemState.sort==="age")list=list.slice().sort((a,b)=>(b.published_at||"").localeCompare(a.published_at||""));
  const rows=$("#rows");
  if(!list.length){rows.innerHTML='<div class="empty">No items match these filters.</div>';return;}
  rows.innerHTML=list.slice(0,300).map(i=>{
    const w=Math.max(2,Math.round((i.score||0)/maxScore*100));
    const titleHtml=i.url?`<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>`:esc(i.title);
    return `<div class="row">
      <div class="r-state">
        <span class="pill ${i.state}">${esc(i.state)}</span>
        ${i.cluster_size>1?`<span class="chip">×${i.cluster_size}</span>`:""}
      </div>
      <div>
        <div class="r-title">${titleHtml}</div>
        <div class="r-meta"><span class="badge ${i.type}">${esc(i.type)}</span><span>${esc(i.source_id)}</span><span>${esc(i.age)} ago</span>${i.is_primary?"":'<span>· sibling</span>'}</div>
        ${i.score_why?`<div class="r-why">${esc(i.score_why)}</div>`:""}
        ${i.snippet?`<div class="r-snip">${esc(i.snippet)}</div>`:""}
      </div>
      <div class="r-score">
        <div class="sv">${i.score==null?"—":i.score.toFixed(2)}</div>
        <div class="r-bar"><i style="width:${w}%"></i></div>
      </div>
    </div>`;
  }).join("");
  if(list.length>300)rows.insertAdjacentHTML("beforeend",`<div class="empty">showing first 300 of ${list.length}</div>`);
}

/* ---- sources ---- */
function panelSources(){
  const rows=DATA.sources.map(s=>`
    <tr>
      <td><strong>${esc(s.id)}</strong></td>
      <td><span class="badge ${s.type}">${esc(s.type)}</span></td>
      <td><div class="tags">${s.tags.map(t=>`<span class="chip">${esc(t)}</span>`).join("")}</div></td>
      <td class="num">${Number(s.weight).toFixed(1)}</td>
      <td class="num">${s.item_count}</td>
      <td><span class="pill ${s.status}">${esc(s.status)}</span></td>
      <td>${s.last_error?`<span class="err">${esc(s.last_error)}</span>`:'<span style="color:var(--muted)">—</span>'}</td>
    </tr>`).join("");
  $("#panel").innerHTML=`<div style="overflow-x:auto"><table class="tbl">
    <thead><tr><th>Source</th><th>Type</th><th>Tags</th><th>Weight</th><th>Items</th><th>Status</th><th>Last error</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

/* ---- digest ---- */
function panelDigest(){
  const d=DATA.digest;
  let html="";
  html+=`<div class="sec-head" style="margin-top:4px"><h2>What ships this run</h2><span class="hint">${d.counts.news} items · ${d.counts.newsletters} newsletters</span></div>`;
  if(d.trackers&&d.trackers.length){
    html+='<div class="tracker-grid" style="margin-bottom:24px">'+d.trackers.map(t=>`
      <div class="tk"><div class="tk-label">${esc(t.label)}</div><div class="tk-value">${esc(t.value)}</div>
      ${t.spark?`<svg viewBox="0 0 110 26" preserveAspectRatio="none"><polyline points="${esc(t.spark)}" fill="none" stroke="${accent()}" stroke-width="1.5"/></svg>`:""}</div>`).join("")+"</div>";
  }
  (d.sections||[]).forEach(sec=>{
    html+=`<div class="digest-tag">${esc(sec.tag)}</div>`;
    html+=sec.entries.map(diItem).join("");
  });
  if(d.newsletters&&d.newsletters.length){html+=`<div class="digest-tag">newsletters</div>`+d.newsletters.map(diItem).join("");}
  if(d.dead_feeds&&d.dead_feeds.length){
    html+=`<div class="digest-tag" style="color:var(--crit)">dead feeds</div>`;
    html+=d.dead_feeds.map(f=>`<div class="di"><div class="di-t">${esc(f.source_id)}</div><div class="di-m err">${esc(f.error_count)} errors · ${esc(f.last_error)}</div></div>`).join("");
  }
  if(!(d.sections||[]).length && !(d.newsletters||[]).length){html+='<div class="empty">Digest window is empty right now.</div>';}
  $("#panel").innerHTML=html;
}
function diItem(it){
  const t=it.url?`<a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>`:esc(it.title);
  const sib=(it.siblings&&it.siblings.length)?` · also: ${esc(it.siblings.join(", "))}`:"";
  return `<div class="di"><div class="di-t"><span class="badge ${it.source_type}">${esc(it.source_type)}</span> ${t}</div>
    <div class="di-m">${esc(it.source_id)} · ${esc(it.age)} ago${sib}</div></div>`;
}

/* ---- clusters ---- */
function panelClusters(){
  if(!DATA.clusters.length){$("#panel").innerHTML='<div class="empty">No multi-source duplicates in the current window. Level-1 (canonical-URL) dedup still ran on every item.</div>';return;}
  $("#panel").innerHTML=DATA.clusters.map(c=>`
    <div class="cluster"><div class="cl-id">cluster ${esc(c.cluster_id)} · ${c.members.length} members</div>
    ${c.members.map(m=>`<div class="cl-m ${m.is_primary?"primary":""}">${m.is_primary?"★ ":"↳ "}<span style="color:var(--muted)">[${esc(m.source_id)}]</span> ${esc(m.title)}</div>`).join("")}
    </div>`).join("");
}

/* ---- scoring ---- */
function panelScoring(){
  const s=DATA.scoring;
  const kw=s.keywords.map(([k,v])=>`<span class="kw pos"><span>${esc(k)}</span><span class="w">+${Number(v).toFixed(1)}</span></span>`).join("");
  const neg=s.negatives.map(([k,v])=>`<span class="kw neg"><span>${esc(k)}</span><span class="w">${Number(v).toFixed(1)}</span></span>`).join("");
  const knob=(l,v)=>`<div class="knob"><div class="k-l">${esc(l)}</div><div class="k-v">${esc(v)}</div></div>`;
  $("#panel").innerHTML=`
    <div class="formula">${esc(s.formula)}</div>
    <div class="sec-head"><h2>Keyword weights</h2><span class="hint">matched against title + first 500 chars · summed, capped at ${s.keyword_score_cap}</span></div>
    <div class="kw-wrap">${kw}</div>
    <div class="sec-head"><h2>Negative keywords</h2><span class="hint">demote clickbait / promo</span></div>
    <div class="kw-wrap">${neg}</div>
    <div class="sec-head"><h2>Knobs</h2><span class="hint">config.yaml</span></div>
    <div class="knobs">
      ${knob("Half-life",s.half_life_hours+"h")}
      ${knob("Dedup threshold",s.dedup_threshold)}
      ${knob("Max items",s.max_items)}
      ${knob("Dedup window",s.window_hours+"h")}
    </div>`;
}

/* ---- redraw everything that depends on the accent color (theme change) ---- */
function drawAll(){drawPipeline();drawSummary();drawTrackers();if(activeTab==="digest"||activeTab==="scoring"||activeTab==="clusters")drawPanel();}

drawHeader();drawPipeline();drawSummary();drawTrackers();drawTabs();drawPanel();
</script>
"""

if __name__ == "__main__":
    conn = init_db()
    run(conn, write_inner="--inner" in sys.argv)
