"""Oil Price Radar — Brent crude procurement decision-support dashboard.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import signals as sig

st.set_page_config(page_title="Oil Price Radar", page_icon="🛢️", layout="wide")

ARROWS = {"up": "▲", "down": "▼", "flat": "→"}
ARROW_COLORS = {"up": "#d62728", "down": "#2ca02c", "flat": "#7f7f7f"}
FLAT_BAND_PCT = 0.5  # a "flat" call counts as correct within +/- this move


def eia_key() -> str:
    try:
        return st.secrets.get("EIA_API_KEY", "") or os.environ.get("EIA_API_KEY", "")
    except Exception:
        return os.environ.get("EIA_API_KEY", "")


# ------------------------------------------------------------------ cached fetches

@st.cache_data(ttl=3600, show_spinner="Fetching Brent prices…")
def load_history():
    hist, label = sig.fetch_brent_history(period="1y")
    return hist, label


@st.cache_data(ttl=3600, show_spinner="Reading the futures curve…")
def load_curve():
    return sig.signal_curve()


@st.cache_data(ttl=3600, show_spinner="Fetching EIA inventories…")
def load_inventories(api_key: str):
    return sig.signal_inventories(api_key)


@st.cache_data(ttl=6 * 3600, show_spinner="Fetching OPEC spare capacity…")
def load_spare(api_key: str):
    return sig.signal_spare_capacity(api_key)


# ------------------------------------------------------------------ sidebar

with st.sidebar:
    st.title("🛢️ Oil Price Radar")

    st.subheader("Strait of Hormuz check")
    st.markdown(
        "Eyeball tanker traffic (~30 s), then set today's status:\n"
        "- [MarineTraffic — Hormuz](https://www.marinetraffic.com/en/ais/home/centerx:56.8/centery:26.5/zoom:8)\n"
        "- [VesselFinder — Hormuz](https://www.vesselfinder.com/?lat=26.5&lon=56.8&zoom=8)"
    )
    hormuz_level = st.radio(
        "Shipping status",
        list(sig.HORMUZ_LEVELS),
        index=list(sig.HORMUZ_LEVELS).index(sig.last_hormuz_level()),
        help="Manual input for now; signals.py is structured so a real AIS "
             "API can replace it later.",
    )

    st.divider()
    if st.button("🔄 Refresh now", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    if st.button("📸 Log today's snapshot", width="stretch",
                 help="Appends today's signals & prediction to data/snapshots.csv "
                      "(the GitHub Actions cron also does this daily at 05:30 "
                      "Hong Kong time). Re-logging the same day overwrites it."):
        os.environ["HORMUZ_LEVEL"] = hormuz_level
        import snapshot
        snapshot.take_snapshot()
        st.success("Snapshot logged.")

    st.divider()
    if eia_key():
        st.caption("EIA API key: ✅ configured")
    else:
        st.caption(
            "EIA API key: ⚠️ using the shared rate-limited DEMO_KEY. "
            "Get a free key at [eia.gov/opendata](https://www.eia.gov/opendata/register.php) "
            "and add `EIA_API_KEY` to `.streamlit/secrets.toml` — see README."
        )
    st.caption("Decision support, not trading advice.")

# ------------------------------------------------------------------ data

try:
    hist, hist_label = load_history()
except RuntimeError as e:
    st.error(f"Cannot load any price data (live or cached): {e}")
    st.stop()

hist_stale = "stale" in hist_label
key = eia_key()
all_signals = [
    sig.signal_momentum(hist, stale=hist_stale),
    load_curve(),
    load_inventories(key),
    sig.signal_hormuz(hormuz_level),
    load_spare(key),
]
risk_score, weight_avail = sig.compute_risk_score(all_signals)
pred = sig.predict(hist, risk_score)
strongest = sig.strongest_signal(all_signals)

# ------------------------------------------------------------------ header

close = hist["Close"].dropna()
price, prev = float(close.iloc[-1]), float(close.iloc[-2])
head_l, head_r = st.columns([2, 1])

with head_l:
    st.markdown("## Oil Price Radar")
    stale_badge = " 🟠 `STALE`" if hist_stale else ""
    st.caption(f"Brent crude ({hist_label}) — last close "
               f"{close.index[-1].date()}{stale_badge}")
    st.metric("Brent price", f"${price:,.2f}",
              f"{price - prev:+.2f} ({(price / prev - 1) * 100:+.2f}%)",
              delta_color="inverse")  # for a buyer, up is bad
    if weight_avail < 1:
        st.warning(f"Only {weight_avail:.0%} of signal weight available — "
                   "some sources are down; score is renormalized over the rest.")

with head_r:
    zone = "#2ca02c" if risk_score < 35 else ("#f0ad4e" if risk_score <= 65 else "#d62728")
    gauge = go.Figure(go.Indicator(
        mode="gauge+number", value=risk_score,
        title={"text": "Oil Risk Score", "font": {"size": 16}},
        number={"font": {"size": 40, "color": zone}},
        gauge={
            "axis": {"range": [0, 100], "tickvals": [0, 35, 65, 100]},
            "bar": {"color": zone, "thickness": 0.35},
            "steps": [
                {"range": [0, 35], "color": "rgba(44,160,44,0.25)"},
                {"range": [35, 65], "color": "rgba(240,173,78,0.30)"},
                {"range": [65, 100], "color": "rgba(214,39,40,0.25)"},
            ],
        },
    ))
    gauge.update_layout(height=210, margin=dict(l=25, r=25, t=40, b=5))
    st.plotly_chart(gauge, width="stretch")

# ------------------------------------------------------------------ prediction cards

reason = f"Driven mostly by {strongest.name.lower()} (score {strongest.score:.0f}): {strongest.detail}"

c1, c2 = st.columns(2)
for col, title, p in ((c1, "Tomorrow", pred["daily"]),
                      (c2, "Next week (5 trading days)", pred["weekly"])):
    with col:
        with st.container(border=True):
            arrow = ARROWS[p["direction"]]
            st.markdown(
                f"#### {title} &nbsp; "
                f"<span style='color:{ARROW_COLORS[p['direction']]};font-size:1.6em'>{arrow}</span>",
                unsafe_allow_html=True,
            )
            st.markdown(f"Expected range **${p['low']:,.2f} – ${p['high']:,.2f}** "
                        f"(centre ${p['center']:,.2f})")
            st.caption(reason)
st.caption("⚠️ Signal-weighted estimates with wide error bars — "
           "**decision support, not trading advice.**")

# ------------------------------------------------------------------ signal breakdown

st.markdown("### Signal breakdown")
live_weight = sum(s.weight for s in all_signals if s.score is not None)
for s in all_signals:
    r1, r2, r3, r4 = st.columns([3, 2, 2, 5])
    stale_tag = " 🟠 `STALE`" if s.stale and s.score is not None else ""
    r1.markdown(f"**{s.name}**{stale_tag}")
    if s.score is None:
        r2.markdown("`unavailable`")
        r3.markdown(f"weight {s.weight} → **dropped**")
    else:
        r2.progress(int(s.score), text=f"{s.score:.0f} / 100")
        contrib = s.score * s.weight / live_weight
        r3.markdown(f"weight {s.weight} → contributes **{contrib:.1f}** pts")
    r4.caption(s.detail)

# ------------------------------------------------------------------ price chart

st.markdown("### Brent — last 6 months, with prediction band")
six_mo = close[close.index >= close.index[-1] - timedelta(days=183)]
recent = close[close.index >= close.index[-1] - timedelta(days=7)]

t0 = close.index[-1]
t1 = t0 + pd.tseries.offsets.BDay(1)
t5 = t0 + pd.tseries.offsets.BDay(5)
band_x = [t0, t1, t5]
band_hi = [price, pred["daily"]["high"], pred["weekly"]["high"]]
band_lo = [price, pred["daily"]["low"], pred["weekly"]["low"]]
band_mid = [price, pred["daily"]["center"], pred["weekly"]["center"]]

fig = go.Figure()
fig.add_trace(go.Scatter(x=six_mo.index, y=six_mo, name="Brent close",
                         line=dict(color="#1f77b4", width=1.6)))
fig.add_trace(go.Scatter(x=recent.index, y=recent, name="Last 7 days",
                         line=dict(color="#ff7f0e", width=3.5)))
fig.add_trace(go.Scatter(x=band_x, y=band_hi, showlegend=False,
                         line=dict(width=0), hoverinfo="skip"))
fig.add_trace(go.Scatter(x=band_x, y=band_lo, name="Expected range",
                         fill="tonexty", fillcolor="rgba(214,39,40,0.15)",
                         line=dict(width=0), hoverinfo="skip"))
fig.add_trace(go.Scatter(x=band_x, y=band_mid, name="Expected path",
                         line=dict(color="#d62728", width=2, dash="dot")))
fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10),
                  yaxis_title="USD / bbl", hovermode="x unified",
                  legend=dict(orientation="h", y=1.05))
st.plotly_chart(fig, width="stretch")

# ------------------------------------------------------------------ track record

with st.expander("📒 Track record — does this thing actually work?"):
    try:
        snaps = pd.read_csv(sig.SNAPSHOT_PATH, dtype={"date": str})
    except Exception:
        snaps = pd.DataFrame()

    if snaps.empty:
        st.info("No snapshots yet. The GitHub Actions cron logs one every day "
                "at 05:30 Hong Kong time, or press **Log today's snapshot** "
                "in the sidebar.")
    else:
        trading_days = close.index.normalize()

        def actual_after(date_str: str, n: int) -> float | None:
            later = close[trading_days > pd.Timestamp(date_str)]
            return float(later.iloc[n - 1]) if len(later) >= n else None

        def verdict(direction: str, snap_price: float, actual: float | None):
            if actual is None or pd.isna(snap_price):
                return None
            chg = (actual / snap_price - 1) * 100
            if direction == "up":
                return chg > 0
            if direction == "down":
                return chg < 0
            return abs(chg) <= FLAT_BAND_PCT

        rows = []
        for _, r in snaps.iterrows():
            a1 = actual_after(r["date"], 1)
            a5 = actual_after(r["date"], 5)
            rows.append({
                "date": r["date"],
                "price": r["price"],
                "risk score": r["risk_score"],
                "daily call": ARROWS.get(r["daily_dir"], "?"),
                "next close": a1,
                "daily ✓": verdict(r["daily_dir"], r["price"], a1),
                "weekly call": ARROWS.get(r["weekly_dir"], "?"),
                "close +5d": a5,
                "weekly ✓": verdict(r["weekly_dir"], r["price"], a5),
            })
        rec = pd.DataFrame(rows)

        m1, m2, m3 = st.columns(3)
        for col, label, series in ((m1, "Daily hit rate", rec["daily ✓"]),
                                   (m2, "Weekly hit rate", rec["weekly ✓"])):
            resolved = series.dropna()
            col.metric(label,
                       f"{resolved.mean():.0%}" if len(resolved) else "—",
                       f"{len(resolved)} resolved calls", delta_color="off")
        m3.metric("Snapshots logged", len(rec))

        show = rec.copy()
        for c in ("daily ✓", "weekly ✓"):
            show[c] = show[c].map({True: "✅", False: "❌", None: "⏳"}).fillna("⏳")
        st.dataframe(show.iloc[::-1], width="stretch", hide_index=True)
        st.caption(f"A '→' call counts as correct when the move stays within "
                   f"±{FLAT_BAND_PCT}%. ⏳ = actual price not known yet.")
