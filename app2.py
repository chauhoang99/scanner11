import os
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Strat Type 1 Break Probability", layout="wide")

OANDA_GRANULARITY = {
    "M15": "M15",
    "H1": "H1",
    "H4": "H4",
    "H8": "H8",
    "D1": "D",
    "W1": "W",
    "MN": "M",
}

DEFAULT_INSTRUMENTS = [
    "EUR_USD","GBP_USD","USD_JPY","AUD_USD","USD_CAD","NZD_USD",
    "EUR_JPY","GBP_JPY","USD_CHF","EUR_GBP","AUD_JPY","CAD_JPY",
    "SGD_JPY","USD_SGD","EUR_SGD","GBP_SGD","AUD_SGD","CAD_SGD",
    "XAU_USD","XAG_USD"
]

def get_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)

def oanda_host(environment):
    return (
        "https://api-fxpractice.oanda.com"
        if environment == "practice"
        else "https://api-fxtrade.oanda.com"
    )

@st.cache_data(ttl=3600, show_spinner=False)
def discover_account_id(token, environment):
    r = requests.get(
        f"{oanda_host(environment)}/v3/accounts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:500]}")
    accounts = r.json().get("accounts", [])
    if not accounts:
        raise RuntimeError("No OANDA accounts are authorized for this API token.")
    return accounts[0]["id"]

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_oanda_instruments(token, environment, account_id):
    r = requests.get(
        f"{oanda_host(environment)}/v3/accounts/{account_id}/instruments",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:500]}")
    items = r.json().get("instruments", [])
    items.sort(key=lambda x: (x.get("type", ""), x.get("displayName", x.get("name", ""))))
    return items

@st.cache_data(ttl=300, show_spinner=False)
def fetch_oanda_candles(token, environment, instrument, granularity, count):
    url = f"{oanda_host(environment)}/v3/instruments/{instrument}/candles"
    params = {
        "price": "M",
        "granularity": granularity,
        "count": min(int(count), 5000),
    }
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:500]}")

    rows = []
    for c in r.json().get("candles", []):
        if not c.get("complete", False):
            continue
        m = c["mid"]
        rows.append({
            "time": pd.to_datetime(c["time"], utc=True),
            "open": float(m["o"]),
            "high": float(m["h"]),
            "low": float(m["l"]),
            "close": float(m["c"]),
            "volume": int(c.get("volume", 0)),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("OANDA returned no completed candles.")
    return df.reset_index(drop=True)

def find_setups(df, candle_type):
    """Find The Strat Type 1 or Type 2 candles."""
    setups = []
    for i in range(1, len(df)):
        cur = df.iloc[i]
        prev = df.iloc[i - 1]
        inside = cur.high <= prev.high and cur.low >= prev.low
        breaks_high = cur.high > prev.high
        breaks_low = cur.low < prev.low

        if candle_type == "Type 1":
            if inside:
                setups.append(i)
        else:  # Type 2 = breaks exactly one side of previous candle
            if breaks_high != breaks_low:
                setups.append(i)
    return setups

def analyze_type1(df, setup_indices, forward_bars):
    """
    For each Type 1 candle, observe the next N completed candles.

    A side is considered broken by PRICE TRADE/WICK:
      high side: a future high > selected candle high
      low side:  a future low  < selected candle low

    Outcomes:
      Both sides   = both high and low are broken within N bars
      High only    = only high is broken
      Low only     = only low is broken
      Neither      = neither side is broken

    'One side broken' = High only + Low only.
    It is mutually exclusive from 'Both sides broken'.
    """
    rows = []

    for i in setup_indices:
        setup = df.iloc[i]
        hi = float(setup.high)
        lo = float(setup.low)

        high_broken = False
        low_broken = False
        high_break_bar = None
        low_break_bar = None

        end_i = min(len(df) - 1, i + forward_bars)
        available = end_i - i

        # Do not include setups without the full requested forward window.
        if available < forward_bars:
            continue

        for j in range(i + 1, end_i + 1):
            bar = df.iloc[j]

            if not high_broken and bar.high > hi:
                high_broken = True
                high_break_bar = j - i

            if not low_broken and bar.low < lo:
                low_broken = True
                low_break_bar = j - i

            if high_broken and low_broken:
                break

        if high_broken and low_broken:
            outcome = "Both sides"
        elif high_broken:
            outcome = "High only"
        elif low_broken:
            outcome = "Low only"
        else:
            outcome = "Neither"

        rows.append({
            "Setup Bar": i,
            "Setup Time": setup.time,
            "Type 1 High": hi,
            "Type 1 Low": lo,
            "Outcome": outcome,
            "High Broken": high_broken,
            "Low Broken": low_broken,
            "High Break After Bars": high_break_bar,
            "Low Break After Bars": low_break_bar,
        })

    return pd.DataFrame(rows)

st.title("The Strat — Type 1 / Type 2 Break Probability")
st.caption(
    "Measures what happens after a completed Strat Type 1 or Type 2 candle using real completed OANDA midpoint candles."
)

with st.sidebar:
    st.header("Controls")

    environment = st.selectbox(
        "OANDA environment",
        ["practice", "live"],
        index=0 if get_secret("OANDA_ENV", "practice") == "practice" else 1,
    )

    token = get_secret("OANDA_API_KEY") or get_secret("OANDA_API_TOKEN")
    if not token:
        token = st.text_input("OANDA API token", type="password")

    account_id = get_secret("OANDA_ACCOUNT_ID")

    if token:
        try:
            if not account_id:
                account_id = discover_account_id(token, environment)
            instrument_meta = fetch_oanda_instruments(token, environment, account_id)
            instrument_names = [x["name"] for x in instrument_meta]
            meta_by_name = {x["name"]: x for x in instrument_meta}
            default_idx = instrument_names.index("CAD_SGD") if "CAD_SGD" in instrument_names else 0
            instrument = st.selectbox(
                f"Instrument ({len(instrument_names)} available)",
                instrument_names,
                index=default_idx,
                format_func=lambda x: f'{meta_by_name[x].get("displayName", x)} · {x}',
            )
        except Exception as e:
            st.warning(f"Could not load all OANDA instruments: {e}")
            default_idx = DEFAULT_INSTRUMENTS.index("CAD_SGD")
            instrument = st.selectbox(
                "Instrument (fallback list)",
                DEFAULT_INSTRUMENTS,
                index=default_idx,
            )
    else:
        default_idx = DEFAULT_INSTRUMENTS.index("CAD_SGD")
        instrument = st.selectbox("Instrument", DEFAULT_INSTRUMENTS, index=default_idx)

    candle_type = st.radio("Candle Type", ["Type 1", "Type 2"], horizontal=True)

    tf_keys = list(OANDA_GRANULARITY)
    tf = st.radio(
        "Timeframe",
        tf_keys,
        index=tf_keys.index("D1"),
        horizontal=True,
    )

    lookback = st.slider("Historical lookback bars", 300, 5000, 3000, 100)
    forward_bars = st.slider(
        "Bars allowed to break Type 1 range",
        1, 50, 10, 1,
        help="Each Type 1 is observed for exactly this many completed bars."
    )

if not token:
    st.info('Add OANDA_API_KEY to `.streamlit/secrets.toml`, or enter the token in the sidebar.')
    st.stop()

try:
    with st.spinner("Loading OANDA candles…"):
        df = fetch_oanda_candles(
            token,
            environment,
            instrument,
            OANDA_GRANULARITY[tf],
            lookback,
        )
except Exception as e:
    st.error(str(e))
    st.stop()

setup_indices = find_setups(df, candle_type)
results = analyze_type1(df, setup_indices, forward_bars)

st.caption(
    f"Source: OANDA v20 ({environment}) • {instrument} • {tf} • "
    f"{len(df):,} completed candles • UTC timestamps"
)

if results.empty:
    st.warning("No selected candle-type setups with a complete forward observation window were found.")
    st.stop()

n = len(results)
counts = results["Outcome"].value_counts()

high_only = int(counts.get("High only", 0))
low_only = int(counts.get("Low only", 0))
one_side = high_only + low_only
both = int(counts.get("Both sides", 0))
neither = int(counts.get("Neither", 0))

one_prob = 100 * one_side / n
both_prob = 100 * both / n
neither_prob = 100 * neither / n

c1, c2, c3, c4 = st.columns(4)
c1.metric(f"{candle_type} samples", f"{n:,}")
c2.metric("One side only", f"{one_prob:.1f}%", f"{one_side} samples")
c3.metric("Both sides", f"{both_prob:.1f}%", f"{both} samples")
c4.metric("Neither side", f"{neither_prob:.1f}%", f"{neither} samples")

st.subheader("Outcome probabilities")
summary = pd.DataFrame([
    {"Outcome": "High only", "Samples": high_only, "Probability %": round(100 * high_only / n, 2)},
    {"Outcome": "Low only", "Samples": low_only, "Probability %": round(100 * low_only / n, 2)},
    {"Outcome": "One side only (High + Low)", "Samples": one_side, "Probability %": round(one_prob, 2)},
    {"Outcome": "Both sides", "Samples": both, "Probability %": round(both_prob, 2)},
    {"Outcome": "Neither side", "Samples": neither, "Probability %": round(neither_prob, 2)},
])
st.dataframe(summary, use_container_width=True, hide_index=True)

st.bar_chart(
    summary[summary["Outcome"].isin(["One side only (High + Low)", "Both sides", "Neither side"])]
    .set_index("Outcome")["Probability %"]
)

st.subheader(f"{candle_type} observations")
st.dataframe(results, use_container_width=True, hide_index=True)

st.download_button(
    "Export Type 1 Results (CSV)",
    results.to_csv(index=False).encode(),
    f"Strat_{candle_type.replace(' ', '')}_{instrument}_{tf}_{forward_bars}bars.csv",
    "text/csv",
)

with st.expander("Exact definitions used"):
    st.markdown(
        """
- **Type 1:** current high ≤ previous high AND current low ≥ previous low.
- **Type 2:** breaks exactly one side of the previous candle: 2U breaks the previous high but not its low; 2D breaks the previous low but not its high.
- **High broken:** within the selected forward window, a future candle's high is above the selected candle high.
- **Low broken:** within the selected forward window, a future candle's low is below the selected candle low.
- **One side only:** exactly one side is broken during the entire observation window.
- **Both sides:** both the selected candle high and selected candle low are broken during the observation window, regardless of which breaks first.
- **Neither:** neither side is broken during the observation window.
- Wick/traded-price breaks count; a closing-price break is not required.
        """
    )
