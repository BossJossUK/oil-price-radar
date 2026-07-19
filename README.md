# 🛢️ Oil Price Radar

A single-page Streamlit dashboard that predicts the daily and weekly direction
of Brent crude and explains itself with a 0–100 **Oil Risk Score** built from
five signals. It is a **procurement decision-support tool, not trading advice** —
robustness and clarity over tick-by-tick precision.

## The five signals

| # | Signal | Weight | Source |
|---|--------|--------|--------|
| 1 | Brent price level & 7-day momentum | 25 | Yahoo Finance `BZ=F` (falls back to `CL=F`) |
| 2 | Futures curve (backwardation vs contango) | 25 | Yahoo Finance dated contracts (front vs ~6 months out) |
| 3 | US crude inventories vs 5-year average | 20 | EIA Open Data v2, weekly series `WCESTUS1` |
| 4 | Strait of Hormuz shipping risk | 20 | Manual sidebar selector (AIS-API-ready stub) |
| 5 | OPEC+ spare capacity | 10 | EIA STEO series `COPS_OPEC` (monthly) |

Every fetch fails soft: if a source is down the last good value is shown with a
🟠 `STALE` badge, and a signal with no data at all has its weight dropped and
the score renormalized — the app never crashes and never silently shows wrong data.

## 1. Get a free EIA API key (2 minutes)

1. Go to <https://www.eia.gov/opendata/register.php>, enter your email, submit.
2. The key arrives by email immediately.
3. Locally: copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
   and paste the key in.

Without a key the app still works using the shared, rate-limited `DEMO_KEY` —
fine for trying it out, not for daily use.

## 2. Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## 3. Deploy free on Streamlit Community Cloud

1. Push this folder to a GitHub repo.
2. Go to <https://share.streamlit.io>, sign in with GitHub, **Create app**,
   pick the repo and `app.py`.
3. In the app's **Settings → Secrets**, paste:
   ```toml
   EIA_API_KEY = "your-key-here"
   ```
4. Done — the app rebuilds on every push.

## 4. Daily snapshots & track record

`.github/workflows/snapshot.yml` runs `snapshot.py` every day at **05:30
Hong Kong time** (21:30 UTC — GitHub crons are defined in UTC) and commits
that day's signals + prediction to `data/snapshots.csv`. Once the
next day's actual price is known, the dashboard's collapsible **Track record**
section shows the hit rate of past direction calls.

To make the cron use your EIA key: repo **Settings → Secrets and variables →
Actions → New repository secret**, name `EIA_API_KEY`.

The cron carries your last Hormuz selection forward (it is persisted in the
snapshot CSV); update it any morning from the sidebar after a 30-second glance
at [MarineTraffic](https://www.marinetraffic.com/en/ais/home/centerx:56.8/centery:26.5/zoom:8)
or [VesselFinder](https://www.vesselfinder.com/?lat=26.5&lon=56.8&zoom=8).

## How the prediction works

- 20-day realized volatility gives the width of the expected range
  (× √5 for the weekly band).
- The Risk Score's deviation from 50 tilts the centre of the band up or down
  and sets the ▲/▼/→ direction call.
- The one-line reason cites whichever signal is pushing hardest from neutral.

## Extending later

- **Real AIS feed**: replace `signal_hormuz()` in `signals.py` with an
  API-backed fetcher returning the same `Signal` shape — nothing else changes.
- Email alerts, a WTI tab, or other markets bolt on the same way: one signal
  function + one entry in `WEIGHTS`.
