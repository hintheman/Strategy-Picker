# strategy_picker.py
# converted from strategy-picker v2.7 (pine script)
# all variable and function names are lowercase

VERSION = "2.7"

import argparse
import glob
import logging
import logging.handlers
import math
import os
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
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

# futures / known-name ticker map
ticker_map = {
    "SPX":  "^GSPC",
    "ES":   "ES=F",
    "NQ":   "NQ=F",
    "MES":  "MES=F",
    "MNQ":  "MNQ=F",
    "YM":   "YM=F",
    "RTY":  "RTY=F",
    "CL":   "CL=F",
    "GC":   "GC=F",
    "BTC":  "BTC=F",
    "ETH":  "ETH=F",
}

# display labels (what to show in output / discord)
label_map = {
    "^GSPC": "SPX",
    "ES=F":  "ES",
    "NQ=F":  "NQ",
    "MES=F": "MES",
    "MNQ=F": "MNQ",
}

# ─── logging ──────────────────────────────────────────────────────────────────

log_dir         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
log_retain_days = 30
ET              = ZoneInfo("America/New_York")


def _purge_old_logs():
    cutoff = datetime.now() - timedelta(days=log_retain_days)
    for path in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                os.remove(path)
        except OSError:
            pass


def setup_logging(debug: bool = False):
    os.makedirs(log_dir, exist_ok=True)
    _purge_old_logs()
    log_path = os.path.join(log_dir, f"{date.today().isoformat()}.log")
    fmt      = "%(asctime)s %(levelname)-8s %(message)s"
    datefmt  = "%Y-%m-%d %H:%M:%S"
    root     = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    fh = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", interval=1,
        backupCount=log_retain_days, utc=False, encoding="utf-8")
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.addHandler(ch)
    logging.info("log → %s  (retain %d days)", log_path, log_retain_days)

# ─── discord ──────────────────────────────────────────────────────────────────

# embed colors keyed by trend_state (1=bull, -1=bear, 0=neutral)
embed_color = {
    1:  3447003,   # blue   — bull
    -1: 15158332,  # red    — bear
    0:  8421504,   # grey   — neutral
}
color_trend_change = 15844367  # yellow — trend state change (warning)


def send_discord(payload):
    """send a discord webhook payload (embed dict or plain-text dict)."""
    try:
        r = requests.post(discord_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            logging.warning("discord %s: %s", r.status_code, r.text[:80])
    except Exception as e:
        logging.warning("discord error: %s", e)


def build_embed(tag, label, trend_state, fields, footer=None, color=None):
    """build a discord embed payload with color-coded by trend."""
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    title   = f"strategy-picker v{VERSION} | {label} | {tag}"
    color   = color if color is not None else embed_color.get(trend_state, embed_color[0])
    embed   = {
        "title":  title,
        "color":  color,
        "fields": [{"name": k, "value": v, "inline": False} for k, v in fields],
        "footer": {"text": footer or f"tastydaytraders | strategy-picker v{VERSION} | {now_str}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    return {"embeds": [embed]}

# ─── data fetching ─────────────────────────────────────────────────────────────

def resolve(symbol):
    """map friendly name to yfinance symbol."""
    return ticker_map.get(symbol.upper(), symbol)

def fetch(symbol, interval, period="5d"):
    yf_sym = resolve(symbol)
    df = yf.download(yf_sym, interval=interval, period=period,
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

    adx_flag = "🔥" if adx_trending else "–"
    logging.info("sp | %-6s | trend:%-8s | %-32s | adx %.1f%s | vix %.2f",
                 display, trend_text(trend_state), strategy[:32],
                 adx_val, adx_flag, vix)
    logging.debug("     5m:%-8s 15m:%-8s 60m:%s  di+:%.1f di-:%.1f",
                  tf5_txt, tf15_txt, tf60_txt, dip_val, dim_val)

    if send_alert:
        tag    = f"MANUAL — {note}" if note else "15m BAR"
        fields = [
            ("timeframes",
             f"5m: {tf5_txt}  {ema_pos_text(ep5)}\n"
             f"15m: {tf15_txt}  {ema_pos_text(ep15)}\n"
             f"60m: {tf60_txt}  {ema_pos_text(ep60)}"),
            ("trend",
             f"{trend_text(trend_state)}  |  adx {adx_val:.1f}{adx_flag}  "
             f"di+:{dip_val:.1f}  di-:{dim_val:.1f}"),
            ("strategy", strategy),
        ]
        if note:
            fields.append(("note", note))
        send_discord(build_embed(tag, display, trend_state, fields))
        logging.info("  → discord sent (%s)", display)

    # return both so callers can track trend changes independently
    return strategy, trend_state, {
        "tf5": tf5_txt, "tf15": tf15_txt, "tf60": tf60_txt,
        "ep5": ep5, "ep15": ep15, "ep60": ep60,
        "adx": adx_val, "adx_flag": adx_flag,
        "dip": dip_val, "dim": dim_val, "vix": vix,
    }


# ─── continuous run helpers ───────────────────────────────────────────────────

bar_secs = 15 * 60   # 15-minute bars


def secs_to_next_bar():
    """seconds until the next 15m bar close, plus 5s for data propagation."""
    now  = time.time()
    next_bar = math.ceil(now / bar_secs) * bar_secs
    return max(5, next_bar - now + 5)


def is_open_bar():
    """true during the 9:30–9:45 am et bar on weekdays (rth open)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return now.hour == 9 and 30 <= now.minute < 45


def _discord_full(label, tag, strat, prev_strat, d, trend_state, color=None):
    """send a full color-coded strategy embed to discord."""
    fields = [
        ("timeframes",
         f"5m: {d['tf5']}  {ema_pos_text(d['ep5'])}\n"
         f"15m: {d['tf15']}  {ema_pos_text(d['ep15'])}\n"
         f"60m: {d['tf60']}  {ema_pos_text(d['ep60'])}"),
        ("trend",
         f"{d['trend_txt']}  |  adx {d['adx']:.1f}{d['adx_flag']}  "
         f"di+:{d['dip']:.1f}  di-:{d['dim']:.1f}"),
        ("strategy", strat),
    ]
    if prev_strat:
        fields.append(("previous strategy", prev_strat))
    send_discord(build_embed(tag, label, trend_state, fields, color=color))


def run_all(symbol_labels, send_alert, note, prev_strategies, prev_trends, prev_adx):
    """scan all symbols; fire discord on open bar, trend change, adx flip, or strategy change."""
    open_bar = is_open_bar()
    for symbol, label in symbol_labels:
        try:
            strategy, trend_state, d = run(symbol, label=label,
                                           send_alert=False, note=note)
            d["trend_txt"] = trend_text(trend_state)
            adx_trending   = d["adx"] >= adx_trend_th

            prev_s = prev_strategies.get(label)
            prev_t = prev_trends.get(label)
            prev_a = prev_adx.get(label)           # bool or None
            strat_changed = prev_s is not None and strategy != prev_s
            trend_changed = prev_t is not None and trend_state != prev_t
            adx_flipped   = prev_a is not None and adx_trending != prev_a
            first_run     = prev_s is None

            if send_alert:
                if open_bar or first_run:
                    tag = "📈 OPEN" if open_bar else "📈 START"
                    logging.info("open alert %s — %s", label, strategy)
                    _discord_full(label, tag, strategy, None, d, trend_state)

                else:
                    # trend change — highest priority non-open alert
                    if trend_changed:
                        old_t = trend_text(prev_t)
                        new_t = trend_text(trend_state)
                        tag   = f"📊 TREND: {old_t} → {new_t}"
                        logging.info("trend change %s: %s → %s  strategy: %s",
                                     label, old_t, new_t, strategy)
                        _discord_full(label, tag, strategy, prev_s, d, trend_state,
                                      color=color_trend_change)

                    # adx momentum flip — fires independently of trend change
                    if adx_flipped:
                        implication = ("credit spreads unlocked" if adx_trending
                                       else "credit spreads locked — prefer debits")
                        adx_tag = (f"⚡ ADX TRENDING ({d['adx']:.1f} ≥ {adx_trend_th:.0f})"
                                   if adx_trending else
                                   f"⚡ ADX FLAT ({d['adx']:.1f} < {adx_trend_th:.0f})")
                        fields = [
                            ("trend",
                             f"{d['trend_txt']}  |  adx {d['adx']:.1f}{d['adx_flag']}  "
                             f"di+:{d['dip']:.1f}  di-:{d['dim']:.1f}"),
                            ("momentum", implication),
                            ("strategy", strategy),
                        ]
                        send_discord(build_embed(adx_tag, label, trend_state, fields))
                        logging.info("adx flip %s: %s → %s  adx=%.1f  %s",
                                     label,
                                     "trending" if prev_a else "flat",
                                     "trending" if adx_trending else "flat",
                                     d["adx"], implication)

                    # strategy changed without a trend or adx flip
                    elif strat_changed and not trend_changed:
                        tag = "🔄 STRATEGY CHANGED"
                        logging.info("strategy change %s: %s → %s", label, prev_s, strategy)
                        _discord_full(label, tag, strategy, prev_s, d, trend_state)

            prev_strategies[label] = strategy
            prev_trends[label]     = trend_state
            prev_adx[label]        = adx_trending

        except Exception as e:
            logging.error("✗ %s: %s", label, e)

    return prev_strategies, prev_trends, prev_adx


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"strategy-picker v{VERSION} — options strategy router")
    parser.add_argument("--tickers", "-t",  nargs="+", type=str, default=None,
                        help="symbols to run (e.g. SPX QQQ ES NQ); defaults to SPX + QQQ")
    parser.add_argument("--run", "-r",      choices=["once", "continuous"], default="once",
                        help="once=run now (default); continuous=loop every 15m bar")
    parser.add_argument("--note", "-n",     type=str, default=None,
                        help="context label for discord post (manual runs)")
    parser.add_argument("--no-discord",     action="store_true",
                        help="print locally only, skip discord")
    parser.add_argument("--debug",          action="store_true",
                        help="print debug to console (always written to log file)")
    args = parser.parse_args()

    setup_logging(args.debug)

    if args.tickers:
        raw = [(t.upper(), label_map.get(resolve(t.upper()), t.upper())) for t in args.tickers]
        symbol_labels = [(resolve(s), lbl) for s, lbl in raw]
    else:
        symbol_labels = [("^GSPC", "SPX"), ("QQQ", "QQQ")]

    send_alert = not args.no_discord

    if args.run == "once":
        for symbol, label in symbol_labels:
            try:
                strategy, trend_state, d = run(symbol, label=label,
                                               note=args.note, send_alert=send_alert)
            except Exception as e:
                logging.error("✗ %s: %s", label, e)

    else:  # continuous
        logging.info("continuous mode — 15m bars — symbols: %s",
                     [lbl for _, lbl in symbol_labels])
        logging.info("alerts: open bar (9:30 ET) + trend change + strategy change")
        prev_s, prev_t, prev_a = {}, {}, {}
        while True:
            prev_s, prev_t, prev_a = run_all(symbol_labels, send_alert,
                                             args.note, prev_s, prev_t, prev_a)
            secs = secs_to_next_bar()
            logging.info("next bar in %.0fs  (%s ET)",
                         secs, datetime.fromtimestamp(time.time() + secs,
                         tz=ET).strftime("%H:%M:%S"))
            time.sleep(secs)
