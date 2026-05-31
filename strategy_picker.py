# strategy_picker.py
# converted from strategy-picker v2.1 (pine script)
# all variable and function names are lowercase

import argparse
import yfinance as yf
import pandas as pd
import requests
from ta.trend import MACD, EMAIndicator, ADXIndicator

# ─── inputs ────────────────────────────────────────────────────────────────────

fast_len          = 12
slow_len          = 26
sig_len           = 9
ema_len           = 8

adx_len           = 14
adx_trend_th      = 20.0
score_th          = 3       # of 4 signals required for bull/bear

vix_low_th        = 16.0
vix_ic_low        = 16.0
vix_ic_high       = 19.0
vix_ibf_low       = 19.0
vix_ibf_high      = 23.0
vix_cs_mid        = 18.0
vix_cs_high       = 19.0

dte_credit_spread = 0
dte_iron_wings    = 5
dte_debit         = 14

use_iron_condor    = True
use_iron_butterfly = True
use_60m_bias       = True

tickers      = ["^GSPC", "QQQ"]
discord_url  = "https://discord.com/api/webhooks/1510324247023587399/z_cQLOeCiOIGW31CnRvcnSlMgkoUPExGSePjReP4CUcrWf39jx-Rv-6_YT5dSdxcKt1L"

# ─── discord alert ─────────────────────────────────────────────────────────────

def send_discord(msg):
    try:
        requests.post(discord_url, json={"content": msg}, timeout=10)
    except Exception as e:
        print(f"  discord error: {e}")

# ─── data fetching ─────────────────────────────────────────────────────────────

def fetch(symbol, interval, period="5d"):
    df = yf.download(symbol, interval=interval, period=period,
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df

# ─── indicator helpers ─────────────────────────────────────────────────────────

def calc_macd(close):
    m = MACD(close, window_slow=slow_len, window_fast=fast_len, window_sign=sig_len)
    return m.macd(), m.macd_signal()

def calc_ema(close):
    return EMAIndicator(close, window=ema_len).ema_indicator()

def calc_adx(high, low, close):
    a = ADXIndicator(high, low, close, window=adx_len)
    return a.adx(), a.adx_pos(), a.adx_neg()

# ─── scoring ──────────────────────────────────────────────────────────────────

def tf_score(macd, signal, close, ema, dip, dim):
    bull = int(macd > 0) + int(macd > signal) + int(close > ema) + int(dip > dim)
    bear = int(macd < 0) + int(macd < signal) + int(close < ema) + int(dim > dip)
    if bull >= score_th:
        return 1
    if bear >= score_th:
        return -1
    return 0

def ema_pos(close, ema):
    diff = (close - ema) / ema * 100
    if diff > 0.05:
        return 1
    if diff < -0.05:
        return -1
    return 0

def ema_pos_text(pos):
    return {1: "↑ ema 8", -1: "↓ ema 8", 0: "@ ema 8"}[pos]

def trend_text(ts):
    return {1: "bull", -1: "bear", 0: "neutral"}[ts]

# ─── trend confirmation (2-bar smoothing) ─────────────────────────────────────

def confirm_trend(raw_states, window=2):
    state = 0
    prev  = 0
    count = 0
    confirmed = []
    for val in raw_states:
        if val == prev:
            count += 1
        else:
            count = 1
            prev  = val
        if count >= window:
            state = val
        confirmed.append(state)
    return confirmed

# ─── strategy logic ──────────────────────────────────────────────────────────

def cs_allowed(vix, adx_trending):
    return vix >= vix_cs_high or (vix >= vix_cs_mid and adx_trending)

def cs_label(vix):
    return "" if vix >= vix_cs_high else " (reduced size)"

def dte_tag(dte):
    return " [0 dte — same day]" if dte == 0 else f" [{dte} dte]"

def pick_strategy(trend_state, adx_trending, adx_bias, vix):
    effective = trend_state if trend_state != 0 else (adx_bias if adx_trending else 0)
    cs_ok  = cs_allowed(vix, adx_trending)
    cs_lbl = cs_label(vix)
    dte_cs = dte_tag(dte_credit_spread)
    dte_iw = dte_tag(dte_iron_wings)
    dte_db = dte_tag(dte_debit)

    if effective == 1 and adx_trending and cs_ok:
        return "bull put credit spread (pcs)" + cs_lbl + dte_cs
    if effective == -1 and adx_trending and cs_ok:
        return "bear call credit spread (ccs)" + cs_lbl + dte_cs
    if effective == 0:
        if vix < vix_low_th:
            direction = ("bull call debit spread" if adx_bias == 1 else
                         "bear put debit spread"  if adx_bias == -1 else
                         "debit spread (wait for direction)")
            return direction + dte_db
        if use_iron_butterfly and not adx_trending and vix_ibf_low <= vix <= vix_ibf_high:
            return "neutral — iron butterfly" + dte_iw
        if use_iron_condor and not adx_trending and vix_ic_low <= vix <= vix_ic_high:
            return "neutral — iron condor" + dte_iw
        return "pause for reckoning and coffee"
    if effective == 1:
        return ("bull call debit spread" if adx_trending else "bull call debit spread (weak)") + dte_db
    if effective == -1:
        return ("bear put debit spread" if adx_trending else "bear put debit spread (weak)") + dte_db
    return "pause for reckoning and coffee"

# ─── main ─────────────────────────────────────────────────────────────────────

def run(symbol="^GSPC", label=None, send_alert=True, note=None):
    display = label or symbol
    df5    = fetch(symbol, "5m")
    df15   = fetch(symbol, "15m")
    df60   = fetch(symbol, "60m", period="30d")
    vix_df = fetch("^VIX", "15m")

    frames = {}
    for tf, df in [("5m", df5), ("15m", df15), ("60m", df60)]:
        macd, signal  = calc_macd(df["close"])
        ema           = calc_ema(df["close"])
        adx, dip, dim = calc_adx(df["high"], df["low"], df["close"])
        frames[tf] = dict(macd=macd, signal=signal, close=df["close"],
                          ema=ema, dip=dip, dim=dim, adx=adx)

    def last(d, key):
        return float(d[key].dropna().iloc[-1])

    s5  = tf_score(last(frames["5m"],  "macd"), last(frames["5m"],  "signal"),
                   last(frames["5m"],  "close"), last(frames["5m"],  "ema"),
                   last(frames["5m"],  "dip"),   last(frames["5m"],  "dim"))
    s15 = tf_score(last(frames["15m"], "macd"), last(frames["15m"], "signal"),
                   last(frames["15m"], "close"), last(frames["15m"], "ema"),
                   last(frames["15m"], "dip"),   last(frames["15m"], "dim"))
    s60 = tf_score(last(frames["60m"], "macd"), last(frames["60m"], "signal"),
                   last(frames["60m"], "close"), last(frames["60m"], "ema"),
                   last(frames["60m"], "dip"),   last(frames["60m"], "dim"))

    def score_bar(tf, idx):
        d = frames[tf]
        return tf_score(float(d["macd"].dropna().iloc[idx]),
                        float(d["signal"].dropna().iloc[idx]),
                        float(d["close"].dropna().iloc[idx]),
                        float(d["ema"].dropna().iloc[idx]),
                        float(d["dip"].dropna().iloc[idx]),
                        float(d["dim"].dropna().iloc[idx]))

    def raw_state(r60, r15):
        if r60 == 1  and r15 == 1:  return  1
        if r60 == -1 and r15 == -1: return -1
        if use_60m_bias and r60 != 0: return r60
        return 0

    raw_series  = [raw_state(score_bar("60m", i), score_bar("15m", i)) for i in range(-3, 0)]
    trend_state = confirm_trend(raw_series, window=2)[-1]

    adx_val      = last(frames["15m"], "adx")
    dip_val      = last(frames["15m"], "dip")
    dim_val      = last(frames["15m"], "dim")
    adx_trending = adx_val >= adx_trend_th
    adx_bias     = 1 if dip_val > dim_val else (-1 if dim_val > dip_val else 0)

    vix = float(vix_df["close"].dropna().iloc[-1])

    ep5  = ema_pos(last(frames["5m"],  "close"), last(frames["5m"],  "ema"))
    ep15 = ema_pos(last(frames["15m"], "close"), last(frames["15m"], "ema"))
    ep60 = ema_pos(last(frames["60m"], "close"), last(frames["60m"], "ema"))

    strategy = pick_strategy(trend_state, adx_trending, adx_bias, vix)

    tf5_txt  = "bull ▲" if s5  == 1 else "bear ▼" if s5  == -1 else "neutral"
    tf15_txt = "bull ▲" if s15 == 1 else "bear ▼" if s15 == -1 else "neutral"
    tf60_txt = "bull ▲" if s60 == 1 else "bear ▼" if s60 == -1 else "neutral"

    print(f"\nstrategy picker — {display}")
    print("─" * 54)
    print(f"  5m    : {tf5_txt:<12}  {ema_pos_text(ep5)}")
    print(f"  15m   : {tf15_txt:<12}  {ema_pos_text(ep15)}")
    print(f"  60m   : {tf60_txt:<12}  {ema_pos_text(ep60)}")
    print(f"  trend : {trend_text(trend_state):<12}  "
          f"adx {adx_val:.1f}{'🔥' if adx_trending else ' –'}  "
          f"di+:{dip_val:.1f} di-:{dim_val:.1f}")
    print(f"  strat : {strategy}")
    print(f"  vix   : {vix:.2f}")
    print("─" * 54)

    if send_alert:
        adx_flag  = "🔥" if adx_trending else "–"
        tag       = f"MANUAL — {note}" if note else "OPEN"
        note_line = f"\n> {note}" if note else ""
        msg = (f"**SP | {display} | {tag}**{note_line}\n"
               f"5m: {tf5_txt}  {ema_pos_text(ep5)}\n"
               f"15m: {tf15_txt}  {ema_pos_text(ep15)}\n"
               f"60m: {tf60_txt}  {ema_pos_text(ep60)}\n"
               f"trend: {trend_text(trend_state)}  |  adx {adx_val:.1f}{adx_flag}  di+:{dip_val:.1f} di-:{dim_val:.1f}\n"
               f"**strat: {strategy}**\n"
               f"vix: {vix:.2f}")
        send_discord(msg)
        print("  → alert sent to discord")

    return strategy


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--note", "-n", type=str, default=None,
                        help="optional context label for the discord post (e.g. 'after fed news')")
    parser.add_argument("--no-discord", action="store_true",
                        help="print locally only, do not post to discord")
    parser.add_argument("--tickers", "-t", nargs="+", type=str, default=None,
                        help="tickers to run (e.g. --tickers AMD NVDA QCOM); defaults to SPX + QQQ")
    args = parser.parse_args()

    if args.tickers:
        symbol_labels = [(t.upper(), t.upper()) for t in args.tickers]
    else:
        symbol_labels = [("^GSPC", "SPX"), ("QQQ", "QQQ")]

    for symbol, label in symbol_labels:
        try:
            run(symbol, label=label, note=args.note, send_alert=not args.no_discord)
        except Exception as e:
            print(f"\n  ✗ {label}: skipped — {e}")
