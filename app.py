import os
import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go

st.set_page_config(page_title="QuantBreakout — OANDA", layout="wide")

OANDA_GRANULARITY = {"M15":"M15","H1":"H1","H4":"H4","D1":"D","W1":"W","MN":"M"}
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

@st.cache_data(ttl=300, show_spinner=False)
def fetch_oanda_candles(token, environment, instrument, granularity, count):
    host = "https://api-fxpractice.oanda.com" if environment == "practice" else "https://api-fxtrade.oanda.com"
    url = f"{host}/v3/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {token}"}
    # OANDA caps a single candles request at 5000.
    params = {"price":"M", "granularity":granularity, "count":min(int(count), 5000)}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:500]}")
    rows = []
    for c in r.json().get("candles", []):
        if not c.get("complete", False):
            continue
        m = c["mid"]
        rows.append({
            "time": pd.to_datetime(c["time"], utc=True),
            "open": float(m["o"]), "high": float(m["h"]),
            "low": float(m["l"]), "close": float(m["c"]),
            "volume": int(c.get("volume", 0))
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("OANDA returned no completed candles.")
    return df.reset_index(drop=True)

def detect_patterns(df, enabled):
    out = []
    for i in range(1, len(df)):
        c, p = df.iloc[i], df.iloc[i-1]
        is_ob = c.high > p.high and c.low < p.low
        curr_bull, curr_bear = c.close > c.open, c.close < c.open
        prev_bull, prev_bear = p.close > p.open, p.close < p.open
        bull_eb = curr_bull and prev_bear and c.close >= p.open and c.open <= p.close
        bear_eb = curr_bear and prev_bull and c.close <= p.open and c.open >= p.close
        is_eb = bull_eb or bear_eb
        is_oeb = is_ob and is_eb
        typ = None
        # Preserve original precedence: an OEB is classified once, not also as OB/EB.
        if is_oeb and "OEB" in enabled: typ = "OEB"
        elif is_ob and "OB" in enabled: typ = "OB"
        elif is_eb and "EB" in enabled: typ = "EB"
        if typ:
            out.append({"barIndex":i, "type":typ, "high":c.high, "low":c.low,
                        "isBullish":bool(curr_bull), "risk":c.high-c.low})
    return out

def simulate_trade(df, entry_i, trade, tp_r):
    current_sl = trade["initialSL"]
    trade["slHistory"] = [{"bar":entry_i, "sl":current_sl}]
    for j in range(entry_i + 1, len(df)):
        bar, prev = df.iloc[j], df.iloc[j-1]
        if trade["direction"] == "LONG":
            if trade["tpPrice"] is not None and bar.high >= trade["tpPrice"]:
                trade.update(exitBar=j, exitPrice=trade["tpPrice"], returnR=tp_r,
                             exitReason="Take Profit Target")
                return j, trade
            current_sl = max(current_sl, prev.low)
            trade["slHistory"].append({"bar":j, "sl":current_sl})
            if bar.low <= current_sl:
                rr = (current_sl-trade["entryPrice"])/trade["riskR"]
                trade.update(exitBar=j, exitPrice=current_sl, returnR=round(rr,2),
                             exitReason="Trailing SL Hit")
                return j, trade
        else:
            if trade["tpPrice"] is not None and bar.low <= trade["tpPrice"]:
                trade.update(exitBar=j, exitPrice=trade["tpPrice"], returnR=tp_r,
                             exitReason="Take Profit Target")
                return j, trade
            current_sl = min(current_sl, prev.high)
            trade["slHistory"].append({"bar":j, "sl":current_sl})
            if bar.high >= current_sl:
                rr = (trade["entryPrice"]-current_sl)/trade["riskR"]
                trade.update(exitBar=j, exitPrice=current_sl, returnR=round(rr,2),
                             exitReason="Trailing SL Hit")
                return j, trade
    j = len(df)-1
    px = float(df.iloc[j].close)
    rr = ((px-trade["entryPrice"])/trade["riskR"] if trade["direction"]=="LONG"
          else (trade["entryPrice"]-px)/trade["riskR"])
    trade.update(exitBar=j, exitPrice=px, returnR=round(rr,2), exitReason="Open Position")
    return j, trade

def backtest(df, patterns, forward_window, tp_r):
    trades, active = [], []
    pattern_map = {p["barIndex"]:p for p in patterns}
    i = 0
    while i < len(df):
        active = [s for s in active if i-s["barIndex"] <= forward_window]
        if i in pattern_map:
            active.append(pattern_map[i])
        triggered = False
        for setup in list(active):
            if not (i > setup["barIndex"] and i-setup["barIndex"] <= forward_window):
                continue
            c = df.iloc[i]
            direction = None
            if c.close > setup["high"]: direction = "LONG"
            elif c.close < setup["low"]: direction = "SHORT"
            if not direction: continue
            entry = float(c.close)
            initial_sl = setup["low"] if direction=="LONG" else setup["high"]
            risk = entry-initial_sl if direction=="LONG" else initial_sl-entry
            if risk <= 0: continue
            tp = None if tp_r == 0 else (entry + tp_r*risk if direction=="LONG" else entry-tp_r*risk)
            trade = dict(id=len(trades)+1, patternType=setup["type"], direction=direction,
                         setupBar=setup["barIndex"], entryBar=i, entryPrice=entry,
                         initialSL=float(initial_sl), currentSL=float(initial_sl),
                         riskR=float(risk), tpPrice=tp)
            i, trade = simulate_trade(df, i, trade, tp_r)
            trades.append(trade)
            active = []
            triggered = True
            break
        i += 1
    return trades

def stats(trades, patterns, typ="ALL"):
    ts = trades if typ=="ALL" else [t for t in trades if t["patternType"]==typ]
    ps = patterns if typ=="ALL" else [p for p in patterns if p["type"]==typ]
    if not ts:
        return dict(patterns=len(ps), trades=0, win_rate=0., total_r=0., pf=0., expectancy=0.,
                    avg_win=0., avg_loss=0.)
    rs = np.array([t["returnR"] for t in ts], dtype=float)
    wins, losses = rs[rs>0], rs[rs<=0]
    gp, gl = wins.sum() if len(wins) else 0., abs(losses.sum()) if len(losses) else 0.
    pf = 99.99 if gl==0 and gp>0 else (gp/gl if gl else 0.)
    return dict(patterns=len(ps), trades=len(ts), win_rate=round(100*len(wins)/len(ts),1),
                total_r=round(rs.sum(),2), pf=round(pf,2), expectancy=round(rs.mean(),2),
                avg_win=round(wins.mean(),2) if len(wins) else 0.,
                avg_loss=round(abs(losses.mean()),2) if len(losses) else 0.)

def chart(df, patterns, trades):
    fig = go.Figure(go.Candlestick(x=df.time, open=df.open, high=df.high, low=df.low, close=df.close, name="OHLC"))
    for typ, symbol in [("OB","circle"),("EB","diamond"),("OEB","star")]:
        ps = [p for p in patterns if p["type"]==typ]
        if ps:
            idx = [p["barIndex"] for p in ps]
            fig.add_trace(go.Scatter(x=df.iloc[idx].time, y=df.iloc[idx].high,
                mode="markers", marker=dict(size=9, symbol=symbol), name=typ,
                customdata=np.column_stack([idx]),
                hovertemplate=typ+"<br>%{x}<br>Bar #%{customdata[0]}<extra></extra>"))
    for t in trades:
        i=t["entryBar"]
        fig.add_trace(go.Scatter(x=[df.iloc[i].time], y=[t["entryPrice"]], mode="markers",
            marker=dict(size=11, symbol="triangle-up" if t["direction"]=="LONG" else "triangle-down"),
            name=t["direction"], showlegend=False,
            hovertemplate=f'{t["direction"]} {t["patternType"]}<br>%{{x}}<br>Entry {t["entryPrice"]}<extra></extra>'))
        hist=t.get("slHistory",[])
        if hist:
            ids=[h["bar"] for h in hist]
            fig.add_trace(go.Scatter(x=df.iloc[ids].time, y=[h["sl"] for h in hist],
                mode="lines", line=dict(dash="dot"), name="Trailing SL", showlegend=False))
    fig.update_layout(height=620, xaxis_rangeslider_visible=False, hovermode="x unified",
                      margin=dict(l=10,r=10,t=35,b=10))
    return fig

st.title("QuantBreakout — OANDA v20")
st.caption("Real completed OANDA mid-price candles • OB / EB / OEB trend-following breakout backtest")

with st.sidebar:
    st.header("Control Panel")
    environment = st.selectbox("OANDA environment", ["practice","live"],
                               index=0 if get_secret("OANDA_ENV","practice")=="practice" else 1)
    token = get_secret("OANDA_API_KEY") or get_secret("OANDA_API_TOKEN")
    if not token:
        token = st.text_input("OANDA API token", type="password")
    instrument = st.selectbox("Instrument", DEFAULT_INSTRUMENTS)
    tf = st.radio("Timeframe", list(OANDA_GRANULARITY), index=1, horizontal=True)
    lookback = st.slider("Historical lookback bars", 300, 5000, 1000, 100)
    forward = st.slider("Breakout window (forward bars)", 1, 10, 3)
    tp_r = st.slider("Take-profit R (0 = trailing only)", 0.0, 5.0, 0.0, 0.5)
    enabled = st.multiselect("Active pattern types", ["OB","EB","OEB"], default=["OB","EB","OEB"])
    run = st.button("Run Strategy", type="primary", use_container_width=True)

if not token:
    st.info('Add OANDA_API_KEY to `.streamlit/secrets.toml`, or enter the token in the sidebar.')
    st.stop()

try:
    with st.spinner("Loading OANDA candles…"):
        df = fetch_oanda_candles(token, environment, instrument, OANDA_GRANULARITY[tf], lookback)
except Exception as e:
    st.error(str(e))
    st.stop()

patterns = detect_patterns(df, set(enabled))
trades = backtest(df, patterns, forward, tp_r)
groups = {k:stats(trades, patterns, k) for k in ["ALL","OB","EB","OEB"]}

st.caption(f"Source: OANDA v20 ({environment}) • {instrument} • {tf} • {len(df):,} completed candles • timestamps shown in UTC")

cols=st.columns(4)
for col, key, label in zip(cols,["ALL","OB","EB","OEB"],["Overall","Outside Bar","Engulfing Bar","Outside Engulf"]):
    s=groups[key]
    with col:
        st.metric(label, f'{s["total_r"]:+.2f} R', f'{s["win_rate"]:.1f}% win')
        st.caption(f'{s["trades"]} trades · PF {s["pf"]:.2f} · Exp {s["expectancy"]:+.2f} R')

tab1,tab2,tab3,tab4=st.tabs(["Interactive Chart","Pattern Matrix & Analytics","Cumulative Equity Curve","Trade Log & Export"])

with tab1:
    st.plotly_chart(chart(df, patterns, trades), use_container_width=True)
    st.caption("Hover any candle for its full UTC date/time and OHLC values.")

with tab2:
    matrix=pd.DataFrame([
        {"Pattern Type":name,"Total Signals":groups[k]["patterns"],"Triggered Trades":groups[k]["trades"],
         "Win Rate %":groups[k]["win_rate"],"Total Return (R)":groups[k]["total_r"],
         "Profit Factor":groups[k]["pf"],"Avg Win (R)":groups[k]["avg_win"],
         "Avg Loss (R)":groups[k]["avg_loss"],"Expectancy (R)":groups[k]["expectancy"]}
        for k,name in [("OB","Outside Bar (OB)"),("EB","Engulfing Bar (EB)"),("OEB","Outside Engulf (OEB)")]
    ])
    st.dataframe(matrix, use_container_width=True, hide_index=True)
    c1,c2=st.columns(2)
    with c1: st.bar_chart(matrix.set_index("Pattern Type")["Win Rate %"])
    with c2: st.bar_chart(matrix.set_index("Pattern Type")["Expectancy (R)"])

with tab3:
    if trades:
        eq=pd.DataFrame({"Trade":range(1,len(trades)+1),"Cumulative R":np.cumsum([t["returnR"] for t in trades])}).set_index("Trade")
        st.line_chart(eq)
        curve=np.r_[0,eq["Cumulative R"].values]
        peaks=np.maximum.accumulate(curve)
        st.caption(f"Max drawdown: {(peaks-curve).max():.2f} R • Max profit peak: {curve.max():.2f} R")
    else:
        st.info("No triggered trades for these settings.")

with tab4:
    log=[]
    for t in trades:
        log.append({
            "ID":t["id"],"Pattern":t["patternType"],"Dir":t["direction"],
            "Setup Bar":t["setupBar"],"Setup Time":df.iloc[t["setupBar"]].time,
            "Entry Bar":t["entryBar"],"Entry Time":df.iloc[t["entryBar"]].time,
            "Entry Price":t["entryPrice"],"Exit Bar":t["exitBar"],
            "Exit Time":df.iloc[t["exitBar"]].time,"Exit Price":t["exitPrice"],
            "Return (R)":t["returnR"],"Reason":t["exitReason"]
        })
    logdf=pd.DataFrame(log)
    if len(logdf):
        f1,f2,f3=st.columns(3)
        pat=f1.selectbox("Pattern filter",["ALL","OB","EB","OEB"])
        outcome=f2.selectbox("Outcome filter",["ALL","WIN","LOSS"])
        direction=f3.selectbox("Direction filter",["ALL","LONG","SHORT"])
        shown=logdf.copy()
        if pat!="ALL": shown=shown[shown.Pattern==pat]
        if outcome=="WIN": shown=shown[shown["Return (R)"]>0]
        elif outcome=="LOSS": shown=shown[shown["Return (R)"]<=0]
        if direction!="ALL": shown=shown[shown.Dir==direction]
        st.dataframe(shown,use_container_width=True,hide_index=True)
        st.download_button("Export Trade Log (CSV)", shown.to_csv(index=False).encode(),
                           f"QuantBreakout_{instrument}_{tf}_TradeLog.csv","text/csv")
    else:
        st.info("No trades to display.")
