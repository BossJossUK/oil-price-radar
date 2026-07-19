"""Daily snapshot for the track record — run headless by GitHub Actions.

Appends one row per UTC day to data/snapshots.csv with the day's signals,
risk score and predictions, so the dashboard can later compare predictions
against what prices actually did.

Env vars:
  EIA_API_KEY   optional — falls back to the rate-limited DEMO_KEY
  HORMUZ_LEVEL  optional — overrides the carried-forward manual selection
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd

import signals as sig

COLUMNS = [
    "date", "ticker", "price",
    "sig_momentum", "sig_curve", "sig_inventories", "sig_hormuz", "sig_bab",
    "sig_spare",
    "hormuz_level", "risk_score", "weight_available",
    "daily_dir", "daily_center", "daily_low", "daily_high",
    "weekly_dir", "weekly_center", "weekly_low", "weekly_high",
    "stale_signals",
]


def take_snapshot() -> pd.DataFrame:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if sig.SNAPSHOT_PATH.exists():
        df = pd.read_csv(sig.SNAPSHOT_PATH, dtype={"date": str})
    else:
        df = pd.DataFrame(columns=COLUMNS)

    # If today's row exists, replace it rather than skip: the 06:00 UTC cron
    # logs first with the carried-forward Hormuz level, and a later manual
    # "Log today's snapshot" (after actually eyeballing the strait) should
    # win. Harmless for the cron itself — it runs once per day.
    replacing = today in set(df["date"])
    if replacing:
        df = df[df["date"] != today]

    hormuz_level = os.environ.get("HORMUZ_LEVEL") or sig.last_hormuz_level()
    api_key = os.environ.get("EIA_API_KEY", "")

    hist, label = sig.fetch_brent_history()
    all_signals = sig.gather_signals(hormuz_level, api_key, hist=hist,
                                     hist_stale="stale" in label)
    score, weight_avail = sig.compute_risk_score(all_signals)
    pred = sig.predict(hist, score)
    by_key = {s.key: s for s in all_signals}

    row = {
        "date": today,
        "ticker": label,
        "price": round(pred["price"], 2),
        **{f"sig_{k}": by_key[k].score for k in
           ("momentum", "curve", "inventories", "hormuz", "bab", "spare")},
        "hormuz_level": hormuz_level,
        "risk_score": score,
        "weight_available": round(weight_avail, 2),
        "daily_dir": pred["daily"]["direction"],
        "daily_center": pred["daily"]["center"],
        "daily_low": pred["daily"]["low"],
        "daily_high": pred["daily"]["high"],
        "weekly_dir": pred["weekly"]["direction"],
        "weekly_center": pred["weekly"]["center"],
        "weekly_low": pred["weekly"]["low"],
        "weekly_high": pred["weekly"]["high"],
        "stale_signals": ";".join(s.key for s in all_signals if s.stale),
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df = df.reindex(columns=COLUMNS)
    sig.DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(sig.SNAPSHOT_PATH, index=False)
    print(f"Snapshot {'updated' if replacing else 'saved'} for {today}: "
          f"price={row['price']} score={score} "
          f"daily={row['daily_dir']} weekly={row['weekly_dir']} "
          f"hormuz={hormuz_level}")
    return df


if __name__ == "__main__":
    take_snapshot()
