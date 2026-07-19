"""Signal engine for Oil Price Radar.

Five signals, each scored 0-100 (0 = calm/bearish, 100 = crisis/bullish),
combined into a weighted Oil Risk Score. Every network fetch fails soft:
on error we fall back to the last good value stored in data/last_good.json
(marked stale); if there is no cached value the signal's score is None and
its weight is dropped from the composite.

No Streamlit imports here — this module is shared by app.py and the
headless snapshot.py used by the GitHub Actions cron.

To swap in a real AIS feed for the Hormuz signal later, replace
`signal_hormuz` with a fetcher that returns a Signal with the same key —
nothing else needs to change.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
CACHE_PATH = DATA_DIR / "last_good.json"
SNAPSHOT_PATH = DATA_DIR / "snapshots.csv"

EIA_BASE = "https://api.eia.gov/v2"
# DEMO_KEY is api.data.gov's public key: it works but is rate-limited to a
# handful of requests per hour. Register a free personal key at
# https://www.eia.gov/opendata/register.php for reliable refreshes.
EIA_FALLBACK_KEY = "DEMO_KEY"

WEIGHTS = {
    "momentum": 20,
    "curve": 25,
    "inventories": 20,
    "hormuz": 15,
    "bab": 10,
    "spare": 10,
}

# IMF PortWatch: free daily AIS-derived chokepoint transit counts (~1 week lag)
PORTWATCH_URL = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/"
                 "services/Daily_Chokepoints_Data/FeatureServer/0/query")
# Bab el-Mandeb mean tanker transits/day Jan-Oct 2023, the last stretch before
# the Red Sea attacks began — the "what normal used to look like" anchor.
BAB_PRE_CRISIS_TANKERS = 26.0

HORMUZ_LEVELS = {
    "Normal flow": 15,
    "Elevated tension": 60,
    "Disrupted": 95,
}

# CME month codes for building dated futures tickers like BZH27.NYM
MONTH_CODES = "FGHJKMNQUVXZ"


@dataclass
class Signal:
    key: str
    name: str
    weight: int
    score: float | None  # None = unavailable, weight dropped
    detail: str          # one-line plain-English reading
    stale: bool = False  # True when showing a cached last-good value
    value: float | None = None  # underlying raw value, for the snapshot CSV


# ---------------------------------------------------------------- last-good cache

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(key: str, payload: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    cache = _load_cache()
    cache[key] = {"saved_at": datetime.now(timezone.utc).isoformat(), **payload}
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=1))
    except OSError:
        pass  # read-only filesystem (e.g. some cloud hosts) — cache is best-effort


def _cached(key: str) -> dict | None:
    return _load_cache().get(key)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- price history

def fetch_brent_history(period: str = "1y") -> tuple[pd.DataFrame, str]:
    """Daily OHLC history for Brent, falling back to WTI, then to cache.

    Returns (df, label) where label is e.g. "BZ=F" or "BZ=F (stale)".
    Raises RuntimeError only if there is no live data AND no cache.
    """
    for ticker in ("BZ=F", "CL=F"):
        try:
            df = yf.Ticker(ticker).history(period=period)
            if len(df) >= 30 and not pd.isna(df["Close"].iloc[-1]):
                df.index = df.index.tz_localize(None)
                _save_cache("history", {
                    "ticker": ticker,
                    "dates": [d.strftime("%Y-%m-%d") for d in df.index],
                    "closes": [round(float(c), 4) for c in df["Close"]],
                })
                return df, ticker
        except Exception:
            continue
    cached = _cached("history")
    if cached:
        df = pd.DataFrame(
            {"Close": cached["closes"]},
            index=pd.to_datetime(cached["dates"]),
        )
        return df, f"{cached['ticker']} (stale)"
    raise RuntimeError("No price source available and no cached history.")


# ---------------------------------------------------------------- signal 1: momentum

def signal_momentum(hist: pd.DataFrame, stale: bool = False) -> Signal:
    """Price level vs 90-day range + 7-day momentum. Weight 25."""
    close = hist["Close"].dropna()
    last = float(close.iloc[-1])
    window = close[close.index >= close.index[-1] - timedelta(days=90)]
    lo, hi = float(window.min()), float(window.max())
    range_pos = 50.0 if hi == lo else (last - lo) / (hi - lo) * 100

    week_ago = close[close.index <= close.index[-1] - timedelta(days=7)]
    if len(week_ago):
        chg7 = (last / float(week_ago.iloc[-1]) - 1) * 100
    else:
        chg7 = 0.0
    # map +/-8% weekly move onto 0-100 around neutral 50
    mom = _clamp(50 + chg7 / 8 * 50)

    score = round(0.5 * range_pos + 0.5 * mom, 1)
    pos_word = "top" if range_pos > 66 else ("bottom" if range_pos < 33 else "middle")
    trend_word = "rising" if chg7 > 1 else ("falling" if chg7 < -1 else "flat")
    detail = (f"Price in the {pos_word} of its 90-day range "
              f"({lo:.0f}-{hi:.0f}), {trend_word} {chg7:+.1f}% over 7 days.")
    return Signal("momentum", "Price level & 7-day momentum", WEIGHTS["momentum"],
                  score, detail, stale=stale, value=round(chg7, 2))


# ---------------------------------------------------------------- signal 2: futures curve

def _dated_ticker(prefix: str, months_ahead: int) -> str:
    """Build a dated contract ticker (e.g. BZH27.NYM) months_ahead from now."""
    d = datetime.now(timezone.utc)
    y, m = d.year, d.month + months_ahead
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{prefix}{MONTH_CODES[m - 1]}{y % 100:02d}.NYM"


def _last_close(ticker: str) -> float | None:
    try:
        h = yf.Ticker(ticker).history(period="5d")
        if len(h) and not pd.isna(h["Close"].iloc[-1]):
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def signal_curve() -> Signal:
    """Backwardation vs contango: front month vs ~6-months-out. Weight 25.

    Brent front month (BZ=F) trades roughly 2 months ahead of the calendar,
    so the 6-month-out contract is ~8 calendar months away. We try a couple
    of offsets in case the target month has no listed/liquid contract.
    """
    w = WEIGHTS["curve"]
    for prefix, front_ticker in (("BZ", "BZ=F"), ("CL", "CL=F")):
        front = _last_close(front_ticker)
        if front is None:
            continue
        for offset in (8, 9, 7):
            far_ticker = _dated_ticker(prefix, offset)
            far = _last_close(far_ticker)
            if far is not None:
                spread_pct = (front - far) / front * 100
                # +10% backwardation -> 100, -10% contango -> 0
                score = round(_clamp(50 + spread_pct * 5), 1)
                shape = "backwardation" if spread_pct > 0.5 else (
                    "contango" if spread_pct < -0.5 else "flat")
                reading = {
                    "backwardation": "market expects near-term tightness",
                    "contango": "market sees comfortable supply",
                    "flat": "market is balanced",
                }[shape]
                detail = (f"Curve in {shape} ({spread_pct:+.1f}%: {front_ticker} "
                          f"{front:.2f} vs {far_ticker} {far:.2f}) — {reading}.")
                _save_cache("curve", {"score": score, "detail": detail,
                                      "spread_pct": round(spread_pct, 2)})
                return Signal("curve", "Futures curve structure", w, score,
                              detail, value=round(spread_pct, 2))
    cached = _cached("curve")
    if cached:
        return Signal("curve", "Futures curve structure", w, cached["score"],
                      cached["detail"], stale=True, value=cached.get("spread_pct"))
    return Signal("curve", "Futures curve structure", w, None,
                  "Futures curve unavailable — weight dropped.", stale=True)


# ---------------------------------------------------------------- signal 3: inventories

def _eia_get(route: str, params: dict, api_key: str) -> list[dict]:
    p = dict(params, api_key=api_key or EIA_FALLBACK_KEY)
    r = requests.get(f"{EIA_BASE}/{route}/data/", params=p, timeout=30)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    if not rows:
        raise ValueError(f"EIA returned no rows for {route}")
    return rows


def signal_inventories(api_key: str = "") -> Signal:
    """US commercial crude stocks vs their 5-year same-week average. Weight 20."""
    w = WEIGHTS["inventories"]
    try:
        rows = _eia_get("petroleum/stoc/wstk", {
            "frequency": "weekly", "data[0]": "value",
            "facets[series][]": "WCESTUS1",
            "sort[0][column]": "period", "sort[0][direction]": "desc",
            "length": 320,  # ~6 years of weekly data
        }, api_key)
        s = pd.Series(
            {pd.Timestamp(r["period"]): float(r["value"]) for r in rows}
        ).sort_index()
        latest_date, latest = s.index[-1], float(s.iloc[-1])
        same_week = []
        for k in range(1, 6):
            target = latest_date - pd.DateOffset(years=k)
            near = s[abs(s.index - target) <= pd.Timedelta(days=45)]
            if len(near):
                gaps = abs(near.index - target)
                same_week.append(float(near.iloc[int(gaps.argmin())]))
        if len(same_week) < 3:
            raise ValueError("Not enough history for a 5-year average")
        avg5 = sum(same_week) / len(same_week)
        dev_pct = (latest - avg5) / avg5 * 100
        # 10% below the 5-yr avg -> 100 (thin buffer), 10% above -> 0
        score = round(_clamp(50 - dev_pct * 5), 1)
        side = "below" if dev_pct < 0 else "above"
        detail = (f"US crude stocks {latest/1000:.0f}M bbl, {abs(dev_pct):.1f}% "
                  f"{side} the 5-yr average ({avg5/1000:.0f}M) as of "
                  f"{latest_date.date()} — "
                  f"{'thin buffer' if dev_pct < -3 else ('ample buffer' if dev_pct > 3 else 'normal buffer')}.")
        _save_cache("inventories", {"score": score, "detail": detail,
                                    "dev_pct": round(dev_pct, 2)})
        return Signal("inventories", "US crude inventories vs 5-yr avg", w,
                      score, detail, value=round(dev_pct, 2))
    except Exception:
        cached = _cached("inventories")
        if cached:
            return Signal("inventories", "US crude inventories vs 5-yr avg", w,
                          cached["score"], cached["detail"], stale=True,
                          value=cached.get("dev_pct"))
        return Signal("inventories", "US crude inventories vs 5-yr avg", w,
                      None, "EIA inventories unavailable — weight dropped.",
                      stale=True)


# ---------------------------------------------------------------- signal 4: Hormuz

def signal_hormuz(level: str) -> Signal:
    """Manual Strait of Hormuz status. Weight 20.

    A stub for a real AIS feed: replace this function with an API-backed
    fetcher returning the same Signal shape and the rest of the app is
    untouched.
    """
    level = level if level in HORMUZ_LEVELS else "Normal flow"
    score = HORMUZ_LEVELS[level]
    detail = {
        "Normal flow": "Hormuz traffic normal — no shipping disruption reported.",
        "Elevated tension": "Elevated tension around Hormuz — watch tanker rates and reroutings.",
        "Disrupted": "Hormuz flow disrupted — major supply risk in play.",
    }[level]
    return Signal("hormuz", "Strait of Hormuz shipping risk",
                  WEIGHTS["hormuz"], float(score), detail, value=float(score))


# ---------------------------------------------------------------- signal 5: Bab el-Mandeb

def signal_bab_mandab() -> Signal:
    """Oil tanker flow through the Bab el-Mandeb strait. Weight 10.

    Source: IMF PortWatch daily transit counts (chokepoint4), free, ~1 week
    lag. Two components, blended 60/40:
      - chronic: last-7-day tanker average vs the pre-crisis norm of ~26/day
        (100 at an 80% shortfall) — catches a structurally closed strait that
        purely trailing baselines would normalize away;
      - acute: last-7-day average vs the trailing 90-day median — catches a
        fresh collapse against whatever the current regime is.
    """
    w = WEIGHTS["bab"]
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=140)).strftime("%Y-%m-%d")
        r = requests.get(PORTWATCH_URL, params={
            "where": f"portid='chokepoint4' AND date >= '{since}'",
            "outFields": "date,n_tanker",
            "orderByFields": "date ASC",
            "resultRecordCount": 2000,
            "f": "json",
        }, timeout=30)
        r.raise_for_status()
        feats = r.json()["features"]
        vals = [f["attributes"]["n_tanker"] for f in feats
                if f["attributes"]["n_tanker"] is not None]
        if len(vals) < 40:
            raise ValueError("PortWatch returned too little history")
        last_date = feats[-1]["attributes"]["date"][:10]
        recent7 = sum(vals[-7:]) / 7
        trailing = sorted(vals[:-7])
        trail_med = trailing[len(trailing) // 2]

        chronic = _clamp((1 - recent7 / BAB_PRE_CRISIS_TANKERS) / 0.8 * 100)
        acute = _clamp(50 - (recent7 / trail_med - 1) * 100) if trail_med else 50.0
        score = round(0.6 * chronic + 0.4 * acute, 1)

        vs_norm = (1 - recent7 / BAB_PRE_CRISIS_TANKERS) * 100
        trend = ("falling further" if recent7 < trail_med * 0.85 else
                 "recovering" if recent7 > trail_med * 1.15 else "steady")
        state = ("severely disrupted" if vs_norm > 55 else
                 "disrupted" if vs_norm > 25 else "near normal")
        vs_word = (f"{vs_norm:.0f}% below" if vs_norm >= 0
                   else f"{-vs_norm:.0f}% above")
        detail = (f"Bab el-Mandeb {state}: {recent7:.0f} tankers/day "
                  f"({vs_word} pre-crisis ~{BAB_PRE_CRISIS_TANKERS:.0f}/day), "
                  f"{trend} vs 90-day trend (data to {last_date}, IMF PortWatch).")
        _save_cache("bab", {"score": score, "detail": detail,
                            "recent7": round(recent7, 1)})
        return Signal("bab", "Bab el-Mandeb tanker flow", w, score, detail,
                      value=round(recent7, 1))
    except Exception:
        cached = _cached("bab")
        if cached:
            return Signal("bab", "Bab el-Mandeb tanker flow", w,
                          cached["score"], cached["detail"], stale=True,
                          value=cached.get("recent7"))
        return Signal("bab", "Bab el-Mandeb tanker flow", w, None,
                      "IMF PortWatch unavailable — weight dropped.", stale=True)


# ---------------------------------------------------------------- signal 6: spare capacity

def signal_spare_capacity(api_key: str = "") -> Signal:
    """OPEC spare production capacity from EIA STEO (monthly). Weight 10."""
    w = WEIGHTS["spare"]
    try:
        rows = _eia_get("steo", {
            "frequency": "monthly", "data[0]": "value",
            "facets[seriesId][]": "COPS_OPEC",
            "sort[0][column]": "period", "sort[0][direction]": "desc",
            "length": 48,
        }, api_key)
        # STEO includes forecast months well past today — take the current
        # month (or the most recent one at/before it), not the first row.
        now_ym = datetime.now(timezone.utc).strftime("%Y-%m")
        past = [r for r in rows if r["period"] <= now_ym]
        row = past[0] if past else rows[-1]
        spare = float(row["value"])
        # >=5 mb/d -> 0 (deep cushion), <=1 mb/d -> 100 (no cushion)
        score = round(_clamp((5 - spare) / 4 * 100), 1)
        cushion = ("razor-thin" if spare < 2 else
                   "moderate" if spare < 3.5 else "comfortable")
        detail = (f"OPEC spare capacity {spare:.1f}M b/d ({row['period']}, EIA "
                  f"STEO) — {cushion} cushion against supply losses.")
        _save_cache("spare", {"score": score, "detail": detail, "spare": spare})
        return Signal("spare", "OPEC+ spare capacity", w, score, detail,
                      value=spare)
    except Exception:
        cached = _cached("spare")
        if cached:
            return Signal("spare", "OPEC+ spare capacity", w, cached["score"],
                          cached["detail"], stale=True, value=cached.get("spare"))
        return Signal("spare", "OPEC+ spare capacity", w, None,
                      "EIA STEO unavailable — weight dropped.", stale=True)


# ---------------------------------------------------------------- composite + prediction

def compute_risk_score(signals: list[Signal]) -> tuple[float, float]:
    """Weighted 0-100 score over the signals that are available.

    Unavailable signals (score None) drop out and the remaining weights are
    renormalized, so a dead feed degrades the score instead of skewing it.
    Returns (score, share_of_total_weight_available).
    """
    live = [s for s in signals if s.score is not None]
    total_w = sum(s.weight for s in live)
    if not total_w:
        return 50.0, 0.0
    score = sum(s.score * s.weight for s in live) / total_w
    return round(score, 1), total_w / sum(WEIGHTS.values())


def strongest_signal(signals: list[Signal]) -> Signal:
    """The available signal pushing hardest away from neutral, weight-adjusted."""
    live = [s for s in signals if s.score is not None]
    return max(live, key=lambda s: abs(s.score - 50) * s.weight)


def predict(hist: pd.DataFrame, risk_score: float) -> dict:
    """Daily and weekly direction + expected range.

    Range = last price +/- 20-day realized volatility (x sqrt(5) for the
    week), with the centre shifted by the risk score's deviation from 50.
    These are signal-weighted estimates, not forecasts with known skill.
    """
    close = hist["Close"].dropna()
    price = float(close.iloc[-1])
    rets = close.pct_change().dropna().tail(20)
    vol_abs = float(rets.std()) * price if len(rets) >= 10 else price * 0.02
    tilt = (risk_score - 50) / 50  # -1 .. +1

    out = {"price": price, "vol_daily": vol_abs}
    for horizon, factor in (("daily", 1.0), ("weekly", math.sqrt(5))):
        vol_h = vol_abs * factor
        shift = tilt * 0.5 * vol_h
        direction = "up" if tilt > 0.2 else ("down" if tilt < -0.2 else "flat")
        out[horizon] = {
            "direction": direction,
            "center": round(price + shift, 2),
            "low": round(price + shift - vol_h, 2),
            "high": round(price + shift + vol_h, 2),
        }
    return out


def gather_signals(hormuz_level: str, api_key: str = "",
                   hist: pd.DataFrame | None = None,
                   hist_stale: bool = False) -> list[Signal]:
    """All five signals in dashboard (weight) order."""
    if hist is None:
        hist, label = fetch_brent_history()
        hist_stale = "stale" in label
    return [
        signal_momentum(hist, stale=hist_stale),
        signal_curve(),
        signal_inventories(api_key),
        signal_hormuz(hormuz_level),
        signal_bab_mandab(),
        signal_spare_capacity(api_key),
    ]


def last_hormuz_level() -> str:
    """Most recent Hormuz selection persisted in the snapshot CSV."""
    try:
        df = pd.read_csv(SNAPSHOT_PATH)
        level = str(df["hormuz_level"].iloc[-1])
        if level in HORMUZ_LEVELS:
            return level
    except Exception:
        pass
    return "Normal flow"
