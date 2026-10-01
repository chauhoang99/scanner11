import os
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Strat Type 1 / 2 / 3 Break Probability", layout="wide")

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
