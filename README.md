# Personal Digest + Tracker

One HTML page and one Telegram message every morning: AI/data-engineering news
(RSS, YouTube, GitHub releases), your Gmail newsletters, and trackers (FX,
weather, air quality). Deterministic ranking — **no LLM anywhere in the
pipeline**. See [PLAN.md](PLAN.md) for the full spec and hard constraints.

## How it works

```
fetch (rss/imap/metrics) → dedup → score → summarize (Gemini) → render (HTML) → notify (Telegram) → mark digested
```

Ranking and dedup are strictly deterministic (no LLM). Gemini is used only to
write a one-line **gist** per item and a short **day overview** — best-effort,
skipped entirely if `GEMINI_API_KEY` is unset (items then show raw snippets).

Everything lives in one SQLite file (`data/digest.db`), committed back to the
repo by the daily GitHub Action. The rendered page is `out/index.html`.

**The page** leads with the day overview, then trackers (FX / weather / air
quality — each with a day-over-day delta, the AQI air-quality category, a
sparkline, and a `↗` to a larger-window view), then one **summary per section**
(AWS · Data Engineering · AI · Newsletters) with inline `[n]` citation links to
sources; the underlying headlines sit in a collapsible drawer. It has a
light/dark toggle, a "generated Xh ago" freshness stamp, and jump-nav.

**Enrichment** — some GitHub *release* tags carry no notes (e.g. Airflow provider
releases). `src/enrich.py` fetches the project's official docs changelog for that
version, uses it as the item body (so the summary is informative) and repoints
the link there. Resolvers are a small registry; add more projects as needed.

## Quick start (local)

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# full run
./.venv/bin/python -m src.run

# open the result
open out/index.html
```

## Running a single stage (debugging)

Each stage runs standalone and prints what it did:

```bash
./.venv/bin/python -m src.db            # init/inspect schema
./.venv/bin/python -m src.fetch_rss     # rss + youtube + github feeds
./.venv/bin/python -m src.fetch_imap    # gmail newsletters
./.venv/bin/python -m src.fetch_metrics # fx / weather / aqi
./.venv/bin/python -m src.dedup         # cluster + print multi-member clusters
./.venv/bin/python -m src.enrich        # fetch docs changelog for content-less items
./.venv/bin/python -m src.score         # score + print top items with reasons
./.venv/bin/python -m src.summarize     # Gemini: overview + section summaries + gists
./.venv/bin/python -m src.render        # write out/index.html (no state change)
./.venv/bin/python -m src.dashboard     # write out/dashboard.html (pipeline console)
./.venv/bin/python -m src.notify        # preview + send Telegram message
```

`render` and `notify` are side-effect-free (they don't mark items digested);
only `python -m src.run` advances item state. So you can re-render freely.

## Dashboard (pipeline console)

A self-contained observability GUI over the live DB — pipeline health, sources,
an interactive item explorer (search / filter / sort with score bars and
`score_why`), the actual digest selection, dedup clusters, and tracker charts:

```bash
./.venv/bin/python -m src.dashboard   # -> out/dashboard.html (open in a browser)
```

It regenerates automatically at the end of every `python -m src.run`, and the
GitHub Action commits it, so it's served next to the digest on Pages at
`/dashboard.html`. It's a read-only visualization — a dev/ops view, separate
from the shipped digest.

## Adding a feed

Add one entry to [`feeds.yaml`](feeds.yaml):

```yaml
- id: my-blog          # unique, stable id
  type: rss            # rss | youtube | github | imap
  url: https://example.com/feed.xml
  tags: [ai]           # drives the section it lands in
  weight: 1.0          # optional; higher = ranks higher and wins dedup ties
```

- **youtube**: use `channel_id: UC...` instead of `url` (the feed URL is built for you).
- **github**: point `url` at a repo's `releases.atom`.
- **imap**: use `label: Newsletters/Foo` (a Gmail label; nested labels use `/`).

## Tuning the ranking

Everything is in [`config.yaml`](config.yaml). Score is:

```
score = source_weight * (1 + keyword_score) * recency_factor
```

- **`keywords` / `negative_keywords`** — substring-matched (case-insensitive)
  against title + first 500 chars of body. Positive matches sum, capped at
  `keyword_score_cap`.
- **`half_life_hours`** — how fast items decay (36h ≈ a day-old item at half
  weight).
- **`dedup_threshold`** — trigram-Jaccard cutoff for merging near-duplicate
  titles. 0.6 is a starting guess; run `python -m src.dedup` and eyeball the
  clusters, then adjust. Lower = merges more aggressively.
- **`max_items`** — hard cut on the news section. Newsletters ignore this (they
  always appear, in their own section).
- **`per_source_cap`** — max items any single source can contribute (before
  `max_items`), so a chatty feed (e.g. Airflow's many provider releases) can't
  crowd out low-volume sources you follow (a YouTube channel, a blog).
- **`section_order`** — order of tag sections in the HTML (e.g. `[aws, dataeng, ai]`).
- **`tracker_links`** — per-metric URL for the tracker's `↗` (larger-window view).
- **`enrich`** — `thin_chars` / `max_per_run` for docs-changelog enrichment of
  content-less GitHub release items (see below).

Every item stores a human-readable `score_why` (e.g.
`src:1.4 kw:+0.8 rec:0.64 [airflow(+0.8)]`) so you can see exactly why it
surfaced. `python -m src.score` prints the top items with their reasons.

## Environment variables

| Var | Needed for | Notes |
|---|---|---|
| `GMAIL_USER` | Gmail newsletters | full email address |
| `GMAIL_APP_PASSWORD` | Gmail newsletters | requires 2FA on the account; generate an [app password](https://myaccount.google.com/apppasswords) |
| `TELEGRAM_BOT_TOKEN` | Telegram message | from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram message | your chat id (message the bot, then check `getUpdates`) |
| `GEMINI_API_KEY` | LLM summaries | [Google AI Studio](https://aistudio.google.com/apikey). Optional — without it, items show raw snippets and there's no day overview |
| `GEMINI_MODEL` | LLM summaries | optional; defaults to `gemini-2.0-flash` |
| `PAGES_URL` | link in Telegram | the public URL of your GitHub Pages site (optional) |

All are optional locally — stages without their env vars skip cleanly.

## Deploying (GitHub Actions)

[`.github/workflows/digest.yml`](.github/workflows/digest.yml) runs daily at
00:00 UTC (07:00 Asia/Ho_Chi_Minh) and on manual `workflow_dispatch`.

1. Make the repo **private** (the DB contains newsletter content).
2. Add the four secrets above under *Settings → Secrets and variables → Actions*
   (and optionally a `PAGES_URL` **variable**).
3. Enable Pages: *Settings → Pages → Source: GitHub Actions*.
4. Trigger a manual run from the Actions tab to confirm it's green.

The workflow commits the updated `data/digest.db` and `out/index.html` back to
the repo, and uses `concurrency: digest` so runs never overlap and corrupt the DB.

## Known limitations / stretch

- **Gold price** is not fetched: `sjc.com.vn` is behind a Cloudflare JS
  challenge. `gold_sjc()` in `src/fetch_metrics.py` is left as a starting point;
  wire in a working source and add it back to `METRIC_FNS`.
- Sparklines need ≥2 days of history before they draw.
- Old un-rendered items linger as `state='new'`; harmless, they age out of the
  render window.
