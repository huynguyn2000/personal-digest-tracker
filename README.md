# Personal Digest + Tracker

One HTML page and one Telegram message every morning: AI/data-engineering news
(RSS, YouTube, GitHub releases), your Gmail newsletters, and trackers (FX,
weather, air quality). Deterministic ranking — **no LLM anywhere in the
pipeline**. See [PLAN.md](PLAN.md) for the full spec and hard constraints.

## How it works

```
fetch (rss/imap/metrics) → dedup → score → render (HTML) → notify (Telegram) → mark digested
```

Everything lives in one SQLite file (`data/digest.db`), committed back to the
repo by the daily GitHub Action. The rendered page is `out/index.html`.

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
./.venv/bin/python -m src.score         # score + print top items with reasons
./.venv/bin/python -m src.render        # write out/index.html (no state change)
./.venv/bin/python -m src.notify        # preview + send Telegram message
```

`render` and `notify` are side-effect-free (they don't mark items digested);
only `python -m src.run` advances item state. So you can re-render freely.

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
- **`section_order`** — order of tag sections in the HTML.

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
