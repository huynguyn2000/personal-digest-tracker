"""Fetch tracker metrics into `metrics`.

Each metric is an independent function returning a list of (name, value, meta)
tuples. Any failure is logged and skipped, never fatal. All values timestamped
at run time (UTC); the renderer draws sparklines from the accumulated history.
"""
from __future__ import annotations

import json
import sqlite3

import httpx

from .db import get_conn, init_db, now_iso

HCMC_LAT, HCMC_LON = 10.82, 106.63
TIMEOUT = 20.0
UA = {"User-Agent": "personal-digest-tracker/1.0"}


def _get_json(url: str, params: dict) -> dict:
    r = httpx.get(url, params=params, headers=UA, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def weather_hcmc() -> list[tuple]:
    """Current temperature + today's max rain probability (open-meteo, no key)."""
    data = _get_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": HCMC_LAT,
            "longitude": HCMC_LON,
            "current": "temperature_2m,precipitation,weather_code",
            "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Ho_Chi_Minh",
            "forecast_days": 1,
        },
    )
    cur = data.get("current", {})
    daily = data.get("daily", {})
    out = []
    if "temperature_2m" in cur:
        out.append(("hcmc_temp", float(cur["temperature_2m"]), {"unit": "C"}))
    if cur.get("weather_code") is not None:
        out.append(("hcmc_weathercode", float(cur["weather_code"]), {"scale": "wmo"}))
    probs = daily.get("precipitation_probability_max") or []
    if probs:
        out.append(("hcmc_rain_prob", float(probs[0]), {"unit": "%"}))
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    if hi is not None:
        out.append(("hcmc_temp_hi", float(hi), {"unit": "C"}))
    if lo is not None:
        out.append(("hcmc_temp_lo", float(lo), {"unit": "C"}))
    return out


def fx_vcb() -> list[tuple]:
    """USD/VND and CNY/VND from the Vietcombank public XML rate feed.

    Fragile endpoint — verify it still responds before relying on it. Uses the
    "transfer" (wire) rate, which is the commonly-quoted mid figure.
    """
    import xml.etree.ElementTree as ET

    r = httpx.get(
        "https://portal.vietcombank.com.vn/UserControls/TVPortal.TyGia/pXML.aspx",
        params={"b": "10"},
        headers=UA,
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    wanted = {"USD": "usd_vnd", "CNY": "cny_vnd", "SGD": "sgd_vnd"}
    out = []
    for ex in root.iter("Exrate"):
        code = ex.get("CurrencyCode")
        if code in wanted:
            raw = (ex.get("Transfer") or ex.get("Sell") or "").replace(",", "").strip()
            if raw and raw != "-":
                out.append((wanted[code], float(raw), {"rate": "transfer"}))
    return out


def gold_sjc() -> list[tuple]:
    """SJC gold buy/sell + spread.

    STRETCH GOAL / currently disabled. Verified 2026-08-08: sjc.com.vn sits
    behind a Cloudflare JS challenge (every endpoint returns a "Just a moment..."
    interstitial), so there is no clean JSON/XML to read without a headless
    browser. Per the plan we do NOT sink time into bypassing that. This function
    is left as a starting point: if you find a working free source, make it
    return (name, value, meta) tuples and add it back to METRIC_FNS below.
    """
    r = httpx.get(
        "https://sjc.com.vn/GiaVang/services/PriceService",
        headers={**UA, "Accept": "application/json"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    r.raise_for_status()
    data = r.json()
    rows = data.get("data") or data.get("Data") or []
    for row in rows:
        name = (row.get("TypeName") or row.get("type") or "").lower()
        if "sjc" in name or "1l" in name or "nhẫn" not in name:
            buy = row.get("BuyValue") or row.get("buy")
            sell = row.get("SellValue") or row.get("sell")
            if buy and sell:
                buy_f, sell_f = float(buy), float(sell)
                return [
                    ("sjc_gold_buy", buy_f, {"type": name}),
                    ("sjc_gold_sell", sell_f, {"type": name}),
                    ("sjc_gold_spread", sell_f - buy_f, {"type": name}),
                ]
    raise RuntimeError("no recognizable SJC row in response")


# gold_sjc intentionally omitted (Cloudflare-blocked, stretch goal). Re-add here
# once you have a working source.
METRIC_FNS = [weather_hcmc, fx_vcb]


def run(conn: sqlite3.Connection) -> list[dict]:
    ts = now_iso()
    results = []
    for fn in METRIC_FNS:
        try:
            rows = fn()
            for name, value, meta in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO metrics (name, ts, value, meta) VALUES (?, ?, ?, ?)",
                    (name, ts, float(value), json.dumps(meta) if meta else None),
                )
            conn.commit()
            names = ", ".join(f"{n}={v:g}" for n, v, _ in rows) or "(no rows)"
            results.append({"fn": fn.__name__, "status": "ok", "rows": len(rows)})
            print(f"  [   ok] {fn.__name__}: {names}")
        except Exception as exc:  # noqa: BLE001 - metrics are best-effort
            results.append({"fn": fn.__name__, "status": "error", "error": str(exc)})
            print(f"  [error] {fn.__name__}: {type(exc).__name__}: {exc}")
    return results


if __name__ == "__main__":
    conn = init_db()
    print("fetch_metrics: fetching trackers...")
    run(conn)
    n = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    print(f"metric rows in db: {n}")
