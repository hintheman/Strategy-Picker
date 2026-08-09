# strategy_picker.py
# converted from strategy-picker v2.7 (pine script)
# all variable and function names are lowercase

VERSION = "2.7"

import argparse
import glob
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date, time as dt_time, timedelta, UTC
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from ta.trend import MACD, EMAIndicator, ADXIndicator
from dotenv import load_dotenv

load_dotenv("/opt/tastydaytraders/ingestion.env")

from home.schwab_broker.SchwabAuthManager import SchwabAuthManager, get_point_value, round_to_tick
from home.schwab_broker.schwab_option_chain import SchwabOptionChainClient
from home.MarketData import BrokerRouter

try:
    from home.DiscordNotifier import DiscordNotifier
except Exception as import_exc:
    DiscordNotifier = None
    _NOTIFIER_IMPORT_ERROR = import_exc
else:
    _NOTIFIER_IMPORT_ERROR = None

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
dte_iron_wings    = 0   # was 5 -- unified to 0dte (IC and Iron Butterfly), also updates dte_tag() text labels
dte_debit         = 0   # was 14 -- unified to 0dte (debit spreads), also updates dte_tag() text labels

use_iron_condor    = True
use_iron_butterfly = True
use_60m_bias       = True

# ─── trade tracking (--track-trades) ───────────────────────────────────────────
# Directional CS picks only (PCS/CCS) -- real strikes/TP/SL off a live Schwab
# chain, tracked locally, Discord-only. Ported from meic-alerts.py's proven
# OptionChainSuggester/position-tracking pattern; conventions match exactly
# (target_delta/max_delta/tp_pct/sl_pct) rather than inventing new numbers.
# Kept as named constants (not inlined) so a future conservative/moderate/
# aggressive selector can swap them in without a rework.
TRACK_TARGET_DELTA = 0.20
TRACK_MAX_DELTA    = 0.25
TRACK_MIN_CREDIT   = 1.50
TRACK_WIDTH        = 20
TRACK_TP_PCT       = 70
TRACK_SL_PCT       = 150

# debit spreads (bull call / bear put) -- inverted economics vs CS/IC/IBF:
# value is expected to RISE, so TP sits above entry debit and SL sits below.
TRACK_DEBIT_LONG_DELTA = 0.60
TRACK_DEBIT_WIDTH      = 10
TRACK_DEBIT_TP_PCT     = 75   # close when value rises 75% over entry debit
TRACK_DEBIT_SL_PCT     = 50   # close when value falls 50% from entry debit

# futures long/short -- no options chain (Schwab /chains doesn't cover
# futures), so TP/SL are a fixed % of entry price rather than %-of-premium.
TRACK_FUT_TP_PCT = 0.6
TRACK_FUT_SL_PCT = 0.3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_POSITIONS_FILE   = os.path.join(SCRIPT_DIR, "strategy_picker_positions.json")
TRACK_DAILY_STATE_FILE = os.path.join(SCRIPT_DIR, "strategy_picker_daily_state.json")

# placeholder -- resolved for real from --discord/--no-discord in __main__ below
discord_url  = ""

INGEST_HOST       = os.getenv("INGEST_HOST", "http://127.0.0.1:8000").rstrip("/")
SIGNAL_EVENTS_URL = INGEST_HOST + "/api/v1/signal-events"

SCHWAB_BROKER_DIR = Path("/opt/tastydaytraders/home/schwab_broker")

# futures / known-name ticker map (yfinance-style resolution)
ticker_map = {
    "SPX":  "^GSPC",
    "VIX":  "^VIX",
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
    "^VIX":  "VIX",
    "ES=F":  "ES",
    "NQ=F":  "NQ",
    "MES=F": "MES",
    "MNQ=F": "MNQ",
}

# schwab-native symbols that diverge from yfinance -- cash indices need a "$"
# prefix on schwab; equities and futures roots pass through unchanged (the
# fleet's other schwab bots don't prefix futures roots either -- see
# as-kbn-futures.py, which passes raw tickers like "NQ" straight through).
SCHWAB_INDEX_MAP = {"SPX": "$SPX", "NDX": "$NDX", "VIX": "$VIX"}

# schwab's pricehistory endpoint only recognizes "1h", not "60m"
SCHWAB_INTERVAL_MAP = {"60m": "1h"}

TIMEFRAME_SECS = {"5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1h": 3600}

# ─── logging ──────────────────────────────────────────────────────────────────

log_dir         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
log_retain_days = 30
ET              = ZoneInfo("America/New_York")

RTH_OPEN  = dt_time(9, 30)   # literal market bell -- drives is_open_bar()'s once-daily
                             # "OPEN" milestone alert; not the active-listening window
ACTIVE_OPEN  = dt_time(9, 0)     # is_rth() gate: start listening 30min before the bell
ACTIVE_CLOSE = dt_time(16, 30)   # ...and keep going 30min after the close
FUT_BREAK_START = dt_time(17, 0)   # 5:00pm ET daily futures maintenance break
FUT_BREAK_END   = dt_time(18, 0)   # 6:00pm ET


def _purge_old_logs():
    cutoff = datetime.now(UTC) - timedelta(days=log_retain_days)
    for path in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), UTC) < cutoff:
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
    if not DiscordNotifier:
        logging.warning("DiscordNotifier unavailable: %s", _NOTIFIER_IMPORT_ERROR)

# ─── discord ──────────────────────────────────────────────────────────────────

NOTIFIER = DiscordNotifier(
    bot_name="strategy-picker", bot_version=f"v{VERSION}", webhook_env="DISCORD_WEBHOOK_STRATEGY"
) if DiscordNotifier else None

COLOR_BULLISH = DiscordNotifier.BULLISH if DiscordNotifier else 0x22C55E
COLOR_BEARISH = DiscordNotifier.BEARISH if DiscordNotifier else 0xEC4899
COLOR_NEUTRAL = DiscordNotifier.NEUTRAL if DiscordNotifier else 0xEAB308
TREND_COLOR   = {1: COLOR_BULLISH, -1: COLOR_BEARISH, 0: COLOR_NEUTRAL}


def _ansi(line, color_code=None, bold=False):
    return DiscordNotifier.ansi(line, color_code, bold) if DiscordNotifier else line


def send_discord(payload):
    """send a discord webhook payload (embed dict or plain-text dict)."""
    if not discord_url:
        logging.debug("discord webhook not resolved (--discord/--no-discord) — skipping send")
        return
    if NOTIFIER is not None:
        NOTIFIER.send(payload, discord_url, logger=logging.getLogger())
        return
    try:
        r = requests.post(discord_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            logging.warning("discord %s: %s", r.status_code, r.text[:80])
    except Exception as e:
        logging.warning("discord error: %s", e)


def build_embed(tag, label, trend_state, fields, color=None, broker_name="schwab"):
    """build a discord embed payload -- ansi-colored horizontal body (one
    "Name  value" line per field), matching the color scheme used across the
    fleet (as-kbn-futures / db-alerts / meic-alerts). The headline lives only
    in the title (not repeated in the body); bot identity/broker/timestamp
    live only in the footer (not the body) -- each piece of info appears
    exactly once, and no "timestamp" key means Discord won't append its own
    redundant "Today at ..." badge next to the footer."""
    headline = f"{tag} | {label}"
    color    = color if color is not None else TREND_COLOR[trend_state]
    palette  = ["32", "36", "37", "35"]  # green, cyan, white, magenta -- cycles per field

    desc_lines = ["```ansi"]
    broker_field_name, broker_field_value = (
        DiscordNotifier.build_broker_field(broker_name) if DiscordNotifier else ("Broker", broker_name.capitalize())
    )
    desc_lines.append(_ansi(f"{broker_field_name:<10} {broker_field_value}", "33"))
    for i, (name, value) in enumerate(fields):
        desc_lines.append(_ansi(f"{name:<10} {value}", palette[i % len(palette)]))
    desc_lines.append("```")

    now = NOTIFIER.now_edt() if NOTIFIER else datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    _tag = f" | 🏷 {NOTIFIER.state_tag}" if (NOTIFIER and NOTIFIER.state_tag) else ""
    footer_text = f"strategy-picker v{VERSION}{_tag} | {now}"

    return {"embeds": [{
        "title": headline,
        "description": "\n".join(desc_lines)[:4096],
        "color": color,
        "footer": {"text": footer_text},
    }]}


def build_session_start_embed(symbol_labels, args, any_futures):
    """--run startup banner, mirroring as-kbn-futures' SESSION STARTED embed."""
    fields = [
        ("Tickers",   ", ".join(lbl for _, lbl in symbol_labels)),
        ("Timeframe", args.timeframe),
        ("Mode",      args.run),
        ("Session",   "23hr futures" if any_futures else "rth"),
    ]
    return build_embed("🚀 SESSION STARTED", "strategy-picker", 0, fields,
                       color=COLOR_BULLISH, broker_name=args.broker)

# ─── signal events ─────────────────────────────────────────────────────────────

def build_signal_event(
    *,
    bot_name,
    bot_version,
    alert_family,
    strategy_code,
    action_stage,
    symbol,
    timeframe,
    position_side,
    qty,
    entry_price,
    exit_price,
    credit,
    debit,
    pnl,
    status,
    reason_code,
    trade_ref,
    event_ts,
    json_payload,
    notes=None,
):
    return {
        "event_key": f"{bot_name}|{strategy_code}|{symbol}|{trade_ref}|{action_stage}",
        "bot_name": bot_name,
        "bot_version": bot_version,
        "alert_family": alert_family,
        "strategy_code": strategy_code,
        "action_stage": action_stage,
        "symbol": symbol,
        "timeframe": timeframe,
        "position_side": position_side,
        "qty": qty,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "credit": credit,
        "debit": debit,
        "pnl": pnl,
        "status": status,
        "reason_code": reason_code,
        "trade_ref": trade_ref,
        "event_ts": event_ts.isoformat(),
        "json_payload": json_payload,
        "notes": notes,
    }


def post_signal_event(event):
    try:
        resp = requests.post(SIGNAL_EVENTS_URL, json=event, timeout=10)
        resp.raise_for_status()
        logging.info("signal_event posted: action_stage=%s symbol=%s trade_ref=%s",
                     event.get("action_stage"), event.get("symbol"), event.get("trade_ref"))
        return resp.json()
    except Exception as e:
        logging.error("signal_event post failed: action_stage=%s symbol=%s trade_ref=%s err=%s",
                      event.get("action_stage"), event.get("symbol"), event.get("trade_ref"), e)
        return None


def build_session_start_event(symbol_labels, args, any_futures):
    now = datetime.now(ET)
    tickers = [lbl for _, lbl in symbol_labels]
    return build_signal_event(
        bot_name="strategy-picker",
        bot_version=f"v{VERSION}",
        alert_family="strategy_picker",
        strategy_code="STRATEGY_PICKER",
        action_stage="SESSION_START",
        symbol=",".join(tickers),
        timeframe=args.timeframe,
        position_side=None,
        qty=None,
        entry_price=None,
        exit_price=None,
        credit=None,
        debit=None,
        pnl=None,
        status="START",
        reason_code="SESSION_START",
        trade_ref=f"strategy-picker-SESSION_START-{now.strftime('%Y%m%d-%H%M%S')}",
        event_ts=now,
        json_payload={
            "tickers": tickers,
            "run_mode": args.run,
            "timeframe": args.timeframe,
            "broker": args.broker,
            "broker_fallback": args.broker_fallback,
            "session": "futures_23hr" if any_futures else "rth",
        },
        notes=f"strategy-picker session started — {args.run} mode, {args.timeframe} bars",
    )

# ─── data fetching ─────────────────────────────────────────────────────────────

# CME futures month codes (F=Jan ... Z=Dec) -- used to peel the expiry off a
# Schwab full-contract symbol like /MNQU26 (U=Sep, 26=2026) to get the root MNQ.
_FUT_MONTH_CODES = "FGHJKMNQUVXZ"


def _futures_root(symbol):
    """Root of a Schwab-style futures contract: '/MNQU26' -> 'MNQ', '/ESZ25' -> 'ES'.
    Anything that doesn't look like a futures contract is returned unchanged (upper)."""
    s = symbol.upper().lstrip("/")
    m = re.match(rf"^([A-Z]+?)[{_FUT_MONTH_CODES}]\d{{1,2}}$", s)
    return m.group(1) if m else s


def resolve(symbol):
    """map friendly name to yfinance symbol."""
    s = symbol.upper()
    if s.startswith("/"):
        # Schwab full-contract futures symbol (e.g. /MNQU26) -> yfinance
        # continuous future (MNQ=F). yfinance has no per-expiry futures.
        root = _futures_root(s)
        return ticker_map.get(root, f"{root}=F")
    return ticker_map.get(s, s)


def resolve_schwab(symbol):
    """map friendly name to schwab-native symbol."""
    s = symbol.upper()
    if s.startswith("/"):
        return s  # already a Schwab futures contract symbol (e.g. /MNQU26)
    return SCHWAB_INDEX_MAP.get(s, s)


def is_futures_ticker(symbol):
    # Schwab full-contract futures symbols start with "/" (e.g. /MNQU26); the
    # short roots (MNQ/ES/...) resolve to a yfinance "=F" symbol.
    return symbol.upper().startswith("/") or resolve(symbol).upper().endswith("=F")


def build_broker_router(args):
    fallback = None if args.broker_fallback == "none" else args.broker_fallback
    auth = None
    if args.broker == "schwab" or fallback == "schwab":
        auth = SchwabAuthManager(
            env_file=SCHWAB_BROKER_DIR / "schwab.env",
            token_file=SCHWAB_BROKER_DIR / "schwab_token.env",
        )
        auth.ensure_valid_token()
    logging.debug("broker router: primary=%s fallback=%s", args.broker, fallback or "none")
    return BrokerRouter(primary=args.broker, fallback=fallback, auth=auth)


def fetch(router, symbol, interval, bars=300):
    """fetch OHLCV bars for a friendly ticker, trying router.primary then
    router.fallback -- resolving the correct symbol per provider (schwab
    cash-index symbols like $SPX differ from yfinance's ^GSPC)."""
    order = [router.primary] + ([router.fallback] if router.fallback else [])
    last_err = None
    for name in order:
        provider = router.providers.get(name)
        if provider is None:
            continue
        sym = resolve_schwab(symbol) if name == "schwab" else resolve(symbol)
        fetch_interval = SCHWAB_INTERVAL_MAP.get(interval, interval) if name == "schwab" else interval
        try:
            df = provider.fetch_bars(sym, fetch_interval, bars)
        except Exception as e:
            last_err = e
            logging.debug("fetch %s via %s (%s, %s) raised: %s", symbol, name, sym, fetch_interval, e)
            continue
        if df is not None and not df.empty:
            logging.debug("fetch %s via %s (%s, %s) -> %d bars, last=%s",
                          symbol, name, sym, fetch_interval, len(df), df.index[-1])
            return df
        logging.debug("fetch %s via %s (%s, %s) returned no data", symbol, name, sym, fetch_interval)
    logging.warning("fetch %s: no data from any provider %s%s",
                    symbol, order, f" (last error: {last_err})" if last_err else "")
    return None

# ─── session gating ────────────────────────────────────────────────────────────

def is_rth(now=None):
    """true during the active listening window (9:00am-4:30pm ET, weekdays --
    30min either side of the actual 9:30/16:00 bell, so a trend/strategy shift
    just before the open or just after the close doesn't go unreported)."""
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return ACTIVE_OPEN <= now.time() < ACTIVE_CLOSE


def is_futures_session(now=None):
    """true during the ~23hr cme globex session: closed saturday, sunday
    before 6pm et, friday after 5pm et, and the daily 5–6pm et maintenance
    break."""
    now = now or datetime.now(ET)
    wd, t = now.weekday(), now.time()
    if wd == 5:
        return False
    if wd == 6 and t < FUT_BREAK_END:
        return False
    if wd == 4 and t >= FUT_BREAK_START:
        return False
    if FUT_BREAK_START <= t < FUT_BREAK_END:
        return False
    return True


def symbol_session_ok(symbol, now=None):
    return is_futures_session(now) if is_futures_ticker(symbol) else is_rth(now)


def seconds_until_rth_open(now=None):
    now = now or datetime.now(ET)
    candidate = datetime.combine(now.date(), ACTIVE_OPEN, tzinfo=ET)
    if candidate <= now:
        candidate += timedelta(days=1)
        candidate = datetime.combine(candidate.date(), ACTIVE_OPEN, tzinfo=ET)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(5, (candidate - now).total_seconds())


def seconds_until_futures_session(now=None):
    now = now or datetime.now(ET)
    wd, t = now.weekday(), now.time()
    if wd == 5:  # saturday -> sunday 6pm
        candidate = datetime.combine(now.date() + timedelta(days=1), FUT_BREAK_END, tzinfo=ET)
    elif wd == 6 and t < FUT_BREAK_END:  # sunday before 6pm
        candidate = datetime.combine(now.date(), FUT_BREAK_END, tzinfo=ET)
    elif wd == 4 and t >= FUT_BREAK_START:  # friday after 5pm -> sunday 6pm
        candidate = datetime.combine(now.date() + timedelta(days=2), FUT_BREAK_END, tzinfo=ET)
    elif FUT_BREAK_START <= t < FUT_BREAK_END:  # daily maintenance break
        candidate = datetime.combine(now.date(), FUT_BREAK_END, tzinfo=ET)
    else:
        candidate = now + timedelta(minutes=5)
    return max(5, (candidate - now).total_seconds())

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

# ─── trade tracking (--track-trades) ───────────────────────────────────────────
# Directional CS picks only (PCS/CCS), real strikes off a live Schwab chain,
# tracked locally in JSON, Discord-only (no signal_events/daily_summary_reports
# writes -- this must never be mistaken for real trading activity in the PnL
# dashboards). Ported and trimmed from meic-alerts.py's proven
# SchwabChainProvider/OptionChainSuggester/position-tracking pattern -- this
# bot's own self-contained copy, not a shared import, matching the fleet's
# convention of each bot carrying its own strike-selection logic.

@dataclass
class OptionLeg:
    option_type: str  # "call" | "put"
    position: str     # "short" | "long"
    strike: float
    bid: float
    ask: float
    mid: float
    symbol: str = ""
    expiration_date: str = ""
    days_to_expiration: Optional[int] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None


@dataclass
class CSSuggestion:
    expiry: str
    spread_type: str  # "call" | "put"
    short_leg: OptionLeg
    long_leg: OptionLeg
    credit: float
    width: float
    tp_pct: int
    sl_pct: int
    tp_debit: float
    sl_debit: float


@dataclass
class ICSuggestion:
    expiry: str
    put_cs: CSSuggestion
    call_cs: CSSuggestion
    total_credit: float


@dataclass
class IBFSuggestion:
    expiry: str
    put_short: OptionLeg
    put_long: OptionLeg
    call_short: OptionLeg
    call_long: OptionLeg
    put_credit: float
    call_credit: float
    total_credit: float
    width: float


@dataclass
class DebitSuggestion:
    expiry: str
    spread_type: str  # "call" | "put"
    long_leg: OptionLeg
    short_leg: OptionLeg
    debit: float
    width: float
    tp_pct: int
    sl_pct: int
    tp_value: float
    sl_value: float


class TrackChainProvider:
    """Lazy-singleton Schwab option-chain fetcher, selecting the expiration
    closest to a target DTE. Same tolerance-window snap-to-nearest-expiration
    logic as meic-alerts.py's SchwabChainProvider."""
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            cls._client = SchwabOptionChainClient()
        return cls._client

    @staticmethod
    def _add_mid(df):
        work = df.copy()
        for col in ("bid", "ask", "last", "mark"):
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0) if col in work.columns else 0.0
        mids = []
        for _, row in work.iterrows():
            bid, ask = float(row["bid"]), float(row["ask"])
            if bid > 0 and ask > 0 and ask >= bid:
                mids.append(round((bid + ask) / 2.0, 2))
            elif float(row["mark"]) > 0:
                mids.append(round(float(row["mark"]), 2))
            elif float(row["last"]) > 0:
                mids.append(round(float(row["last"]), 2))
            else:
                mids.append(0.0)
        work["mid"] = mids
        return work

    @classmethod
    def _rows_to_df(cls, rows):
        if not rows:
            return pd.DataFrame(columns=[
                "strike", "bid", "ask", "mid", "symbol",
                "expirationDate", "daysToExpiration", "delta", "gamma",
            ])
        df = pd.DataFrame(rows).rename(columns={"strikePrice": "strike"})
        for col in ("strike", "bid", "ask", "daysToExpiration", "delta", "gamma"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return cls._add_mid(df)

    @classmethod
    def fetch_chain(cls, ticker, dte=0):
        client = cls.get_client()
        window = 4 if dte <= 7 else 5
        dte_min = max(dte - window, 0)
        dte_max = dte + window
        payload = client.fetch_option_chain(symbol=ticker, dte_min=dte_min, dte_max=dte_max, strikes="ALL")
        norm = client.normalize_chain(ticker, payload)
        calls = cls._rows_to_df(norm.get("calls") or [])
        puts = cls._rows_to_df(norm.get("puts") or [])
        if calls.empty and puts.empty:
            raise ValueError(f"Empty option chain for {ticker} (dte={dte}, window={dte_min}-{dte_max})")
        all_dte = pd.concat([
            calls["daysToExpiration"] if "daysToExpiration" in calls.columns else pd.Series(dtype=float),
            puts["daysToExpiration"] if "daysToExpiration" in puts.columns else pd.Series(dtype=float),
        ]).dropna()
        if all_dte.empty:
            raise ValueError(f"No DTE data in option chain for {ticker}")
        best_dte = min(all_dte.unique(), key=lambda d: abs(d - dte))
        calls_d = calls[calls["daysToExpiration"] == best_dte].dropna(subset=["strike"]).copy()
        puts_d = puts[puts["daysToExpiration"] == best_dte].dropna(subset=["strike"]).copy()
        expiry_str = ""
        if not calls_d.empty and "expirationDate" in calls_d.columns:
            expiry_str = str(calls_d["expirationDate"].iloc[0])
        elif not puts_d.empty and "expirationDate" in puts_d.columns:
            expiry_str = str(puts_d["expirationDate"].iloc[0])
        return puts_d, calls_d, expiry_str, int(best_dte)


class TrackOptionSuggester:
    """Selects short+long strikes for a directional credit spread via a
    delta-target scan over live OTM strikes. Trimmed from meic-alerts.py's
    OptionChainSuggester: pop_mode path only -- no IC, no per-ticker
    override tables (this bot doesn't tune per-ticker like meic-alerts
    does), no gamma-safety walk (--track-trades doesn't expose
    --gamma-level)."""

    @staticmethod
    def pick_leg(df, strike):
        if df.empty:
            return None
        work = df.copy()
        work["dist"] = (work["strike"] - strike).abs()
        work = work.sort_values(["dist", "strike"])
        return work.iloc[0] if not work.empty else None

    @staticmethod
    def pick_nearest_delta(df, target_abs_delta, is_call):
        """picks the strike whose delta is closest to target_abs_delta --
        signed to match calls (positive delta) vs puts (negative)."""
        if df.empty or "delta" not in df.columns:
            return None
        work = df.dropna(subset=["delta"]).copy()
        if work.empty:
            return None
        target = target_abs_delta if is_call else -target_abs_delta
        work["dist"] = (work["delta"] - target).abs()
        work = work.sort_values(["dist", "strike"])
        return work.iloc[0]

    @staticmethod
    def _num(row, key):
        v = row.get(key)
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if v != v else v  # NaN check

    @classmethod
    def build_leg(cls, row, option_type, position):
        dte = row.get("daysToExpiration")
        return OptionLeg(
            option_type=option_type, position=position, strike=float(row["strike"]),
            bid=float(row.get("bid", 0.0) or 0.0), ask=float(row.get("ask", 0.0) or 0.0),
            mid=float(row.get("mid", 0.0) or 0.0), symbol=str(row.get("symbol", "") or ""),
            expiration_date=str(row.get("expirationDate", "") or ""),
            days_to_expiration=int(dte) if pd.notna(dte) else None,
            delta=cls._num(row, "delta"), gamma=cls._num(row, "gamma"),
        )

    def _candidates(self, otm, width, other_df, is_call):
        if otm.empty or "delta" not in otm.columns:
            return []
        work = otm.dropna(subset=["delta"]).copy()
        work["abs_delta"] = work["delta"].abs()
        work = work[work["abs_delta"] <= TRACK_MAX_DELTA]
        if work.empty:
            return []
        candidates = []
        for _, short_row in work.iterrows():
            short_strike = float(short_row["strike"])
            long_row = self.pick_leg(other_df, short_strike + width if is_call else short_strike - width)
            if long_row is None:
                continue
            credit = float(short_row["mid"]) - float(long_row["mid"])
            if credit <= 0 or credit < TRACK_MIN_CREDIT:
                continue
            abs_delta = abs(float(short_row.get("delta", 0.0) or 0.0))
            score = abs(abs_delta - TRACK_TARGET_DELTA)
            candidates.append((score, short_row, long_row, credit))
        candidates.sort(key=lambda x: x[0])
        return candidates

    def suggest_cs(self, ticker, spot_price, spread_type):
        puts, calls, expiry, dte = TrackChainProvider.fetch_chain(ticker, dte=dte_credit_spread)
        if spread_type == "call":
            otm = calls[calls["strike"] > spot_price].copy()
            other_df, is_call = calls, True
        else:
            otm = puts[puts["strike"] < spot_price].copy()
            other_df, is_call = puts, False

        candidates = self._candidates(otm, TRACK_WIDTH, other_df, is_call)
        if not candidates:
            raise ValueError(f"No {spread_type} spread met delta<={TRACK_MAX_DELTA} "
                              f"and credit>={TRACK_MIN_CREDIT} for {ticker}")
        _, short_row, long_row, credit = candidates[0]

        credit = round(credit, 2)
        tp_debit = round(credit * (1 - TRACK_TP_PCT / 100.0), 2)
        sl_debit = round(credit * (TRACK_SL_PCT / 100.0), 2)
        return CSSuggestion(
            expiry=expiry, spread_type=spread_type,
            short_leg=self.build_leg(short_row, spread_type, "short"),
            long_leg=self.build_leg(long_row, spread_type, "long"),
            credit=credit, width=TRACK_WIDTH, tp_pct=TRACK_TP_PCT, sl_pct=TRACK_SL_PCT,
            tp_debit=tp_debit, sl_debit=sl_debit,
        )

    def suggest_ic(self, ticker, spot_price):
        """Iron Condor -- reuses suggest_cs's exact machinery on both sides
        (same width/target-delta/max-delta/min-credit), rather than a
        balanced-credit pairing search: this is an informational tracker,
        not a bot sizing real risk, so leg formation stays identical to CS."""
        put_cs = self.suggest_cs(ticker, spot_price, "put")
        call_cs = self.suggest_cs(ticker, spot_price, "call")
        return ICSuggestion(
            expiry=put_cs.expiry, put_cs=put_cs, call_cs=call_cs,
            total_credit=round(put_cs.credit + call_cs.credit, 2),
        )

    def suggest_ibf(self, ticker, spot_price):
        """Iron Butterfly -- one shared ATM short strike for both legs
        (nearest live strike to spot), wings at TRACK_WIDTH away on each
        side. No meic-alerts/db-alerts equivalent exists; written fresh."""
        puts, calls, expiry, dte = TrackChainProvider.fetch_chain(ticker, dte=dte_iron_wings)
        call_short_row = self.pick_leg(calls, spot_price)
        if call_short_row is None:
            raise ValueError(f"No ATM strike found for {ticker} iron butterfly")
        atm_strike = float(call_short_row["strike"])
        put_short_row = self.pick_leg(puts, atm_strike)
        put_long_row = self.pick_leg(puts, atm_strike - TRACK_WIDTH)
        call_long_row = self.pick_leg(calls, atm_strike + TRACK_WIDTH)
        if put_short_row is None or put_long_row is None or call_long_row is None:
            raise ValueError(f"Missing wing strikes for {ticker} iron butterfly")

        put_credit = float(put_short_row["mid"]) - float(put_long_row["mid"])
        call_credit = float(call_short_row["mid"]) - float(call_long_row["mid"])
        if put_credit <= 0 or call_credit <= 0:
            raise ValueError(f"Non-positive wing credit for {ticker} iron butterfly")
        total_credit = round(put_credit + call_credit, 2)
        if total_credit < TRACK_MIN_CREDIT:
            raise ValueError(f"IBF combined credit {total_credit} below {TRACK_MIN_CREDIT} for {ticker}")

        return IBFSuggestion(
            expiry=expiry,
            put_short=self.build_leg(put_short_row, "put", "short"),
            put_long=self.build_leg(put_long_row, "put", "long"),
            call_short=self.build_leg(call_short_row, "call", "short"),
            call_long=self.build_leg(call_long_row, "call", "long"),
            put_credit=round(put_credit, 2), call_credit=round(call_credit, 2),
            total_credit=total_credit, width=TRACK_WIDTH,
        )

    def suggest_debit(self, ticker, spot_price, side):
        """Bull call / bear put debit spread -- inverted economics vs CS/IC/
        IBF (buy a near-the-money-ish long leg, sell an OTM short leg,
        value expected to RISE). Trimmed from db-alerts.py's
        build_vertical_spread_candidate: single TP/SL, no TP1/TP2 scale-out
        (that lifecycle belongs to db-alerts' own paper-position tracking,
        out of place in this Discord-only tracker)."""
        is_call = side == "call"
        puts, calls, expiry, dte = TrackChainProvider.fetch_chain(ticker, dte=dte_debit)
        chain = calls if is_call else puts

        long_row = self.pick_nearest_delta(chain, TRACK_DEBIT_LONG_DELTA, is_call)
        if long_row is None:
            long_row = self.pick_leg(chain, spot_price)
        if long_row is None or float(long_row.get("mid", 0.0) or 0.0) <= 0:
            raise ValueError(f"No priced {side} long strike near delta={TRACK_DEBIT_LONG_DELTA} for {ticker}")
        long_strike = float(long_row["strike"])

        target_short = long_strike + TRACK_DEBIT_WIDTH if is_call else long_strike - TRACK_DEBIT_WIDTH
        otm_side = chain[chain["strike"] > long_strike] if is_call else chain[chain["strike"] < long_strike]
        short_row = self.pick_leg(otm_side, target_short)
        if short_row is None or float(short_row.get("mid", 0.0) or 0.0) <= 0:
            raise ValueError(f"No priced {side} short strike near {target_short} for {ticker}")
        short_strike = float(short_row["strike"])

        long_mid, short_mid = float(long_row["mid"]), float(short_row["mid"])
        debit = round(long_mid - short_mid, 2)
        if debit <= 0:
            raise ValueError(f"Non-positive debit for {ticker} {side} debit spread")

        tp_value = round(debit * (1 + TRACK_DEBIT_TP_PCT / 100.0), 2)
        sl_value = round(debit * (1 - TRACK_DEBIT_SL_PCT / 100.0), 2)
        return DebitSuggestion(
            expiry=expiry, spread_type=side,
            long_leg=self.build_leg(long_row, side, "long"),
            short_leg=self.build_leg(short_row, side, "short"),
            debit=debit, width=abs(short_strike - long_strike),
            tp_pct=TRACK_DEBIT_TP_PCT, sl_pct=TRACK_DEBIT_SL_PCT,
            tp_value=tp_value, sl_value=sl_value,
        )


def _leg_record(short_leg, long_leg, credit):
    """single-wing record shape shared by IC/IBF's put_leg/call_leg --
    mirrors meic-alerts.py's _leg_record."""
    credit = round(credit, 2)
    tp_debit = round(credit * (1 - TRACK_TP_PCT / 100.0), 2)
    sl_debit = round(credit * (TRACK_SL_PCT / 100.0), 2)
    return {
        "short_strike": short_leg.strike, "long_strike": long_leg.strike,
        "short_symbol": short_leg.symbol, "long_symbol": long_leg.symbol,
        "short_delta": short_leg.delta,
        "entry_credit": credit, "tp_debit": tp_debit, "sl_debit": sl_debit,
        "status": "OPEN", "close_debit": None, "close_ts": None, "pnl": None, "close_reason": None,
    }


def open_tracked_position(ticker, family, side, spot_price, now_et):
    """family: 'CS'|'IC'|'IBF'|'DEBIT'; side: 'put'|'call' for CS/DEBIT
    (ignored for IC/IBF, which trade both sides). Raises if no valid trade
    is found -- caller catches and just skips tracking for this ticker/day."""
    suggester = TrackOptionSuggester()
    trading_day = now_et.strftime("%Y-%m-%d")

    if family == "CS":
        cs = suggester.suggest_cs(ticker, spot_price, side)
        return {
            "ticker": ticker, "family": family, "spread_type": side,
            "trading_day": trading_day, "entry_ts": now_et.isoformat(), "entry_price": spot_price,
            "expiry": cs.expiry,
            "short_strike": cs.short_leg.strike, "long_strike": cs.long_leg.strike,
            "short_symbol": cs.short_leg.symbol, "long_symbol": cs.long_leg.symbol,
            "short_delta": cs.short_leg.delta,
            "entry_credit": cs.credit, "width": cs.width,
            "tp_debit": cs.tp_debit, "sl_debit": cs.sl_debit,
            "status": "OPEN", "close_debit": None, "close_ts": None, "pnl": None, "close_reason": None,
        }

    if family == "DEBIT":
        db = suggester.suggest_debit(ticker, spot_price, side)
        return {
            "ticker": ticker, "family": family, "spread_type": side,
            "trading_day": trading_day, "entry_ts": now_et.isoformat(), "entry_price": spot_price,
            "expiry": db.expiry,
            "short_strike": db.short_leg.strike, "long_strike": db.long_leg.strike,
            "short_symbol": db.short_leg.symbol, "long_symbol": db.long_leg.symbol,
            "long_delta": db.long_leg.delta,
            "entry_debit": db.debit, "width": db.width,
            "tp_value": db.tp_value, "sl_value": db.sl_value,
            "status": "OPEN", "close_value": None, "close_ts": None, "pnl": None, "close_reason": None,
        }

    if family == "IC":
        ic = suggester.suggest_ic(ticker, spot_price)
        put_leg = _leg_record(ic.put_cs.short_leg, ic.put_cs.long_leg, ic.put_cs.credit)
        call_leg = _leg_record(ic.call_cs.short_leg, ic.call_cs.long_leg, ic.call_cs.credit)
        expiry = ic.expiry
    elif family == "IBF":
        ibf = suggester.suggest_ibf(ticker, spot_price)
        put_leg = _leg_record(ibf.put_short, ibf.put_long, ibf.put_credit)
        call_leg = _leg_record(ibf.call_short, ibf.call_long, ibf.call_credit)
        expiry = ibf.expiry
    else:
        raise ValueError(f"unknown tracked position family: {family}")

    return {
        "ticker": ticker, "family": family,
        "trading_day": trading_day, "entry_ts": now_et.isoformat(), "entry_price": spot_price,
        "expiry": expiry, "put_leg": put_leg, "call_leg": call_leg,
    }


def _track_reprice_leg(chain_df, strike, contract_symbol):
    if chain_df is None or chain_df.empty:
        return None
    if contract_symbol and "symbol" in chain_df.columns:
        match = chain_df[chain_df["symbol"] == contract_symbol]
        if not match.empty:
            row = match.iloc[0]
            return {"bid": float(row.get("bid", 0.0) or 0.0), "ask": float(row.get("ask", 0.0) or 0.0)}
    match = chain_df[chain_df["strike"] == strike]
    if not match.empty:
        row = match.iloc[0]
        return {"bid": float(row.get("bid", 0.0) or 0.0), "ask": float(row.get("ask", 0.0) or 0.0)}
    return None


def _force_expire(expiry_date, now_et):
    today_str = now_et.strftime("%Y-%m-%d")
    return today_str > expiry_date or (today_str == expiry_date and now_et.time() >= dt_time(16, 0))


def _manage_single_leg_position(position, underlying_price, now_et):
    """CS (credit) or DEBIT (debit) -- refetches the live chain, reprices
    the position's one spread, and returns a close-event dict if
    TP/SL/EXPIRED triggers this cycle, else None (still open). Mirrors
    meic-alerts.py's manage_open_position, trimmed to a single leg."""
    family = position.get("family", "CS")
    is_debit = family == "DEBIT"
    ticker = position["ticker"]
    is_call = position["spread_type"] == "call"
    expiry_date = (position.get("expiry") or "")[:10] or position["trading_day"]
    force_expire = _force_expire(expiry_date, now_et)
    dte = dte_debit if is_debit else dte_credit_spread

    if force_expire:
        if is_call:
            short_intrinsic = max(underlying_price - position["short_strike"], 0.0)
            long_intrinsic = max(underlying_price - position["long_strike"], 0.0)
        else:
            short_intrinsic = max(position["short_strike"] - underlying_price, 0.0)
            long_intrinsic = max(position["long_strike"] - underlying_price, 0.0)
        settlement_source = "expiration_intrinsic"
        value = round(long_intrinsic - short_intrinsic, 2) if is_debit else round(short_intrinsic - long_intrinsic, 2)
    else:
        try:
            puts_df, calls_df, _, _ = TrackChainProvider.fetch_chain(ticker, dte=dte)
        except Exception as e:
            logging.warning("track: chain refetch failed for %s, retrying next cycle: %s", ticker, e)
            return None
        chain_df = calls_df if is_call else puts_df
        short_q = _track_reprice_leg(chain_df, position["short_strike"], position.get("short_symbol", ""))
        long_q = _track_reprice_leg(chain_df, position["long_strike"], position.get("long_symbol", ""))
        if short_q is None or long_q is None:
            return None  # couldn't reprice this cycle (chain fetch failed/legs not found); retry next cycle
        settlement_source = "live_chain"
        value = round(long_q["bid"] - short_q["ask"], 2) if is_debit else round(short_q["ask"] - long_q["bid"], 2)

    close_reason = None
    if force_expire:
        close_reason = "EXPIRED"
    elif is_debit:
        if value >= position["tp_value"]:
            close_reason = "TP"
        elif value <= position["sl_value"]:
            close_reason = "SL"
    else:
        if value <= position["tp_debit"]:
            close_reason = "TP"
        elif value >= position["sl_debit"]:
            close_reason = "SL"
    if not close_reason:
        return None

    event = {
        "ticker": ticker, "family": family, "spread_type": position["spread_type"], "close_reason": close_reason,
        "short_strike": position["short_strike"], "long_strike": position["long_strike"],
        "settlement_source": settlement_source,
        "entry_underlying_price": position.get("entry_price"), "exit_underlying_price": underlying_price,
        "entry_ts": position.get("entry_ts"), "trading_day": position.get("trading_day"),
    }
    if is_debit:
        pnl = round(value - position["entry_debit"], 2)
        position.update(status=f"CLOSED_{close_reason}", close_value=value,
                         close_ts=now_et.isoformat(), pnl=pnl, close_reason=close_reason)
        event.update(entry_debit=position["entry_debit"], close_value=value, pnl=pnl)
    else:
        pnl = round(position["entry_credit"] - value, 2)
        position.update(status=f"CLOSED_{close_reason}", close_debit=value,
                         close_ts=now_et.isoformat(), pnl=pnl, close_reason=close_reason)
        event.update(entry_credit=position["entry_credit"], close_debit=value, pnl=pnl)
    return event


def _manage_wing(ticker, wing, is_call, expiry_date, underlying_price, now_et, dte):
    """reprices+decides TP/SL/EXPIRED for one CS-shaped wing dict (put_leg
    or call_leg of an IC/IBF position); mutates wing in place. Returns True
    if this call closed the wing this cycle."""
    if wing is None or wing["status"] != "OPEN":
        return False
    force_expire = _force_expire(expiry_date, now_et)

    if force_expire:
        if is_call:
            short_intrinsic = max(underlying_price - wing["short_strike"], 0.0)
            long_intrinsic = max(underlying_price - wing["long_strike"], 0.0)
        else:
            short_intrinsic = max(wing["short_strike"] - underlying_price, 0.0)
            long_intrinsic = max(wing["long_strike"] - underlying_price, 0.0)
        cost_to_close = round(short_intrinsic - long_intrinsic, 2)
        settlement_source = "expiration_intrinsic"
    else:
        try:
            puts_df, calls_df, _, _ = TrackChainProvider.fetch_chain(ticker, dte=dte)
        except Exception as e:
            logging.warning("track: chain refetch failed for %s, retrying next cycle: %s", ticker, e)
            return False
        chain_df = calls_df if is_call else puts_df
        short_q = _track_reprice_leg(chain_df, wing["short_strike"], wing.get("short_symbol", ""))
        long_q = _track_reprice_leg(chain_df, wing["long_strike"], wing.get("long_symbol", ""))
        if short_q is None or long_q is None:
            return False
        cost_to_close = round(short_q["ask"] - long_q["bid"], 2)
        settlement_source = "live_chain"

    close_reason = None
    if force_expire:
        close_reason = "EXPIRED"
    elif cost_to_close <= wing["tp_debit"]:
        close_reason = "TP"
    elif cost_to_close >= wing["sl_debit"]:
        close_reason = "SL"
    if not close_reason:
        return False

    pnl = round(wing["entry_credit"] - cost_to_close, 2)
    wing.update(status=f"CLOSED_{close_reason}", close_debit=cost_to_close, close_ts=now_et.isoformat(),
                pnl=pnl, close_reason=close_reason, settlement_source=settlement_source)
    return True


def _reprice_wing_cost(ticker, wing, is_call, expiry_date, underlying_price, now_et, dte):
    """quote-only reprice (no close decision) used by the companion-close
    check below."""
    force_expire = _force_expire(expiry_date, now_et)
    if force_expire:
        if is_call:
            short_intrinsic = max(underlying_price - wing["short_strike"], 0.0)
            long_intrinsic = max(underlying_price - wing["long_strike"], 0.0)
        else:
            short_intrinsic = max(wing["short_strike"] - underlying_price, 0.0)
            long_intrinsic = max(wing["long_strike"] - underlying_price, 0.0)
        return round(short_intrinsic - long_intrinsic, 2), "expiration_intrinsic", force_expire
    try:
        puts_df, calls_df, _, _ = TrackChainProvider.fetch_chain(ticker, dte=dte)
    except Exception:
        return None, None, force_expire
    chain_df = calls_df if is_call else puts_df
    short_q = _track_reprice_leg(chain_df, wing["short_strike"], wing.get("short_symbol", ""))
    long_q = _track_reprice_leg(chain_df, wing["long_strike"], wing.get("long_symbol", ""))
    if short_q is None or long_q is None:
        return None, None, force_expire
    return round(short_q["ask"] - long_q["bid"], 2), "live_chain", force_expire


def _manage_two_leg_position(position, underlying_price, now_et):
    """IC or IBF -- manages put_leg/call_leg independently through the same
    per-wing TP/SL/EXPIRED logic as a CS, then runs meic-alerts.py's proven
    companion-close pass (meic-alerts.py:1440-1466): if either wing just
    stopped out, close the other side too, but only if it's currently
    profitable; otherwise leave it to its own fate. Returns a close event
    only once BOTH wings are non-OPEN."""
    ticker = position["ticker"]
    family = position.get("family", "IC")
    expiry_date = (position.get("expiry") or "")[:10] or position["trading_day"]
    dte = dte_iron_wings
    put_leg, call_leg = position.get("put_leg"), position.get("call_leg")

    if put_leg:
        _manage_wing(ticker, put_leg, False, expiry_date, underlying_price, now_et, dte)
    if call_leg:
        _manage_wing(ticker, call_leg, True, expiry_date, underlying_price, now_et, dte)

    any_sl = (put_leg and put_leg.get("close_reason") == "SL") or (call_leg and call_leg.get("close_reason") == "SL")
    if any_sl:
        for wing, is_call in ((put_leg, False), (call_leg, True)):
            if not wing or wing["status"] != "OPEN":
                continue
            cost_to_close, settlement_source, force_expire = _reprice_wing_cost(
                ticker, wing, is_call, expiry_date, underlying_price, now_et, dte)
            if cost_to_close is None:
                continue
            if force_expire:
                reason = "EXPIRED"
            elif cost_to_close < wing["entry_credit"]:
                reason = "COMPANION"
            else:
                continue  # still underwater -- leave it to its own TP/SL
            pnl = round(wing["entry_credit"] - cost_to_close, 2)
            wing.update(status=f"CLOSED_{reason}", close_debit=cost_to_close, close_ts=now_et.isoformat(),
                        pnl=pnl, close_reason=reason, settlement_source=settlement_source)

    legs = [w for w in (put_leg, call_leg) if w is not None]
    if not legs or any(w["status"] == "OPEN" for w in legs):
        return None  # still (partially) open -- retry next cycle

    total_pnl = round(sum(w["pnl"] for w in legs), 2)
    return {
        "ticker": ticker, "family": family, "close_reason": "MULTI",
        "put_leg": put_leg, "call_leg": call_leg, "pnl": total_pnl,
        "entry_underlying_price": position.get("entry_price"), "exit_underlying_price": underlying_price,
        "entry_ts": position.get("entry_ts"), "trading_day": position.get("trading_day"),
    }


def manage_tracked_position(position, underlying_price, now_et):
    """Dispatches to the single-leg (CS/DEBIT) or two-leg (IC/IBF) manager
    based on position["family"] (defaults to "CS" for positions written
    before this field existed, so an in-flight production position file
    keeps working across the deploy)."""
    family = position.get("family", "CS")
    if family in ("CS", "DEBIT"):
        return _manage_single_leg_position(position, underlying_price, now_et)
    if family in ("IC", "IBF"):
        return _manage_two_leg_position(position, underlying_price, now_et)
    raise ValueError(f"unknown tracked position family: {family}")


# ─── futures long/short tracking (--track-trades, futures instance) ───────────
# No options chain involved at all (Schwab /chains doesn't cover futures) --
# reuses the regime direction pick_strategy() already computed (every
# bullish label starts with "bull", every bearish label with "bear") and
# tracks a simple long/short of the contract itself, with TP/SL as a fixed
# % of entry price. Continuous entry-when-flat rather than CS/IC/IBF/DEBIT's
# once-per-day catch, since futures have no clean single daily "open" the
# way is_open_bar() assumes (CME Globex runs ~23x5).

def open_tracked_future(ticker, position_side, spot_price, now_et):
    """position_side: 'long' or 'short'."""
    entry_price = round_to_tick(spot_price, ticker)
    if position_side == "long":
        tp_price = round_to_tick(entry_price * (1 + TRACK_FUT_TP_PCT / 100.0), ticker)
        sl_price = round_to_tick(entry_price * (1 - TRACK_FUT_SL_PCT / 100.0), ticker)
    else:
        tp_price = round_to_tick(entry_price * (1 - TRACK_FUT_TP_PCT / 100.0), ticker)
        sl_price = round_to_tick(entry_price * (1 + TRACK_FUT_SL_PCT / 100.0), ticker)
    return {
        "ticker": ticker, "family": "FUT", "position": position_side,
        "entry_price": entry_price, "entry_ts": now_et.isoformat(),
        "tp_price": tp_price, "sl_price": sl_price,
        "status": "OPEN", "close_price": None, "close_ts": None, "pnl": None, "close_reason": None,
    }


def manage_tracked_future(position, current_price, current_label, now_et):
    """returns a close-event dict if TP/SL/REVERSAL triggers this cycle,
    else None (still open). REVERSAL fires when the regime label flips to
    the opposite direction of the open position."""
    is_long = position["position"] == "long"
    close_reason = None
    if is_long:
        if current_price >= position["tp_price"]:
            close_reason = "TP"
        elif current_price <= position["sl_price"]:
            close_reason = "SL"
    else:
        if current_price <= position["tp_price"]:
            close_reason = "TP"
        elif current_price >= position["sl_price"]:
            close_reason = "SL"
    if close_reason is None:
        if (is_long and current_label.startswith("bear")) or ((not is_long) and current_label.startswith("bull")):
            close_reason = "REVERSAL"
    if close_reason is None:
        return None

    point_value = get_point_value(position["ticker"])
    if is_long:
        pnl = round((current_price - position["entry_price"]) * point_value, 2)
    else:
        pnl = round((position["entry_price"] - current_price) * point_value, 2)

    position.update(status=f"CLOSED_{close_reason}", close_price=current_price,
                     close_ts=now_et.isoformat(), pnl=pnl, close_reason=close_reason)
    return {
        "ticker": position["ticker"], "family": "FUT", "position": position["position"],
        "close_reason": close_reason, "entry_price": position["entry_price"],
        "close_price": current_price, "pnl": pnl, "entry_ts": position.get("entry_ts"),
    }


def _track_futures_entry_and_manage(symbol, label, strategy, price, now_et, args):
    """Manages any open position first (TP/SL/REVERSAL); if now flat and
    the regime is directional, opens a new long/short the same cycle."""
    position = _track_positions.get(symbol)
    if position is not None:
        close_event = manage_tracked_future(position, price, strategy, now_et)
        if close_event is not None:
            send_discord(build_track_close_embed(close_event, broker_name=args.broker))
            logging.info("track: closed %s %s reason=%s pnl=%.2f",
                         symbol, close_event["position"], close_event["close_reason"], close_event["pnl"])
            del _track_positions[symbol]
            save_track_positions(_track_positions)
            position = None

    if position is None:
        side = "long" if strategy.startswith("bull") else "short" if strategy.startswith("bear") else None
        if side is not None:
            position = open_tracked_future(symbol, side, price, now_et)
            _track_positions[symbol] = position
            send_discord(build_track_entry_embed(position, broker_name=args.broker))
            logging.info("track: opened %s %s @ %s tp=%s sl=%s",
                         symbol, side, position["entry_price"], position["tp_price"], position["sl_price"])
            save_track_positions(_track_positions)


def load_track_positions():
    if not os.path.exists(TRACK_POSITIONS_FILE):
        return {}
    try:
        with open(TRACK_POSITIONS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        print(f"Failed to load {TRACK_POSITIONS_FILE}: {e}", file=sys.stderr)
        return {}


def save_track_positions(positions):
    try:
        with open(TRACK_POSITIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(positions, fh, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save {TRACK_POSITIONS_FILE}: {e}", file=sys.stderr)


def load_track_daily_state():
    if not os.path.exists(TRACK_DAILY_STATE_FILE):
        return {"entry_attempted": {}}
    try:
        with open(TRACK_DAILY_STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        print(f"Failed to load {TRACK_DAILY_STATE_FILE}: {e}", file=sys.stderr)
        return {"entry_attempted": {}}


def save_track_daily_state(state):
    try:
        with open(TRACK_DAILY_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save {TRACK_DAILY_STATE_FILE}: {e}", file=sys.stderr)


_track_positions    = load_track_positions()
_track_daily_state  = load_track_daily_state()


def _track_embed_body(fields, broker_name, label_width=12):
    """Shared ansi-body builder for the two embeds below -- same broker-field
    + per-field-color-cycling convention as meic-alerts.py's build_close_embed."""
    palette = ["32", "36", "37", "35"]
    desc_lines = ["```ansi"]
    broker_field_name, broker_field_value = (
        DiscordNotifier.build_broker_field(broker_name) if DiscordNotifier else ("Broker", broker_name.capitalize())
    )
    desc_lines.append(_ansi(f"{broker_field_name:<{label_width}} {broker_field_value}", "33"))
    for i, (name, value) in enumerate(fields):
        desc_lines.append(_ansi(f"{name:<{label_width}} {value}", palette[i % len(palette)]))
    desc_lines.append("```")
    return "\n".join(desc_lines)[:4096]


def _track_footer():
    now = NOTIFIER.now_edt() if NOTIFIER else datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    tag = f" | 🏷 {NOTIFIER.state_tag}" if (NOTIFIER and NOTIFIER.state_tag) else ""
    return f"strategy-picker v{VERSION}{tag} | {now}"


def _build_cs_entry_embed(position, broker_name):
    strat_label = "CCS" if position["spread_type"] == "call" else "PCS"
    strat_name = "Bear Call Credit Spread (CCS)" if position["spread_type"] == "call" else "Bull Put Credit Spread (PCS)"
    headline = f"🎯 TRADE PICK | {position['ticker']} {strat_label}"
    fields = [
        ("Strategy", strat_name),
        ("Spread",   f"{position['short_strike']:.0f}/{position['long_strike']:.0f}"),
        ("Credit",   f"${position['entry_credit']:.2f}"),
        ("TP",       f"${position['tp_debit']:.2f}"),
        ("SL",       f"${position['sl_debit']:.2f}"),
        ("Expiry",   (position.get("expiry") or "")[:10] or "n/a"),
    ]
    color = COLOR_BULLISH if position["spread_type"] == "put" else COLOR_BEARISH
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=10),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def _build_debit_entry_embed(position, broker_name):
    is_call = position["spread_type"] == "call"
    strat_name = "Bull Call Debit Spread" if is_call else "Bear Put Debit Spread"
    headline = f"🎯 TRADE PICK | {position['ticker']} {'CALL' if is_call else 'PUT'} DEBIT"
    fields = [
        ("Strategy",   strat_name),
        ("Spread",     f"{position['short_strike']:.0f}/{position['long_strike']:.0f}"),
        ("Debit paid", f"${position['entry_debit']:.2f}"),
        ("TP",         f"${position['tp_value']:.2f}"),
        ("SL",         f"${position['sl_value']:.2f}"),
        ("Expiry",     (position.get("expiry") or "")[:10] or "n/a"),
    ]
    color = COLOR_BULLISH if is_call else COLOR_BEARISH
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=10),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def _build_multi_leg_entry_embed(position, broker_name):
    family = position.get("family", "IC")
    put_leg, call_leg = position["put_leg"], position["call_leg"]
    total_credit = round(put_leg["entry_credit"] + call_leg["entry_credit"], 2)
    headline = f"🎯 TRADE PICK | {position['ticker']} {family}"
    fields = [
        ("Put spread",   f"{put_leg['short_strike']:.0f}/{put_leg['long_strike']:.0f}  (${put_leg['entry_credit']:.2f})"),
        ("Call spread",  f"{call_leg['short_strike']:.0f}/{call_leg['long_strike']:.0f}  (${call_leg['entry_credit']:.2f})"),
        ("Total credit", f"${total_credit:.2f}"),
        ("Expiry",       (position.get("expiry") or "")[:10] or "n/a"),
    ]
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=12),
        "color": COLOR_NEUTRAL,
        "footer": {"text": _track_footer()},
    }]}


def _build_fut_entry_embed(position, broker_name):
    side = position["position"]
    headline = f"🎯 TRADE PICK | {position['ticker']} {side.upper()}"
    fields = [
        ("Entry", f"{position['entry_price']:.2f}"),
        ("TP",    f"{position['tp_price']:.2f}"),
        ("SL",    f"{position['sl_price']:.2f}"),
    ]
    color = COLOR_BULLISH if side == "long" else COLOR_BEARISH
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=8),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def build_track_entry_embed(position, broker_name="schwab"):
    family = position.get("family", "CS")
    if family == "FUT":
        return _build_fut_entry_embed(position, broker_name)
    if family in ("IC", "IBF"):
        return _build_multi_leg_entry_embed(position, broker_name)
    if family == "DEBIT":
        return _build_debit_entry_embed(position, broker_name)
    return _build_cs_entry_embed(position, broker_name)


def _build_cs_close_embed(event, broker_name):
    win = event["pnl"] >= 0
    emoji = "✅" if win else "\U0001F6D1"
    color = COLOR_BULLISH if win else COLOR_BEARISH
    strat_label = "CCS" if event["spread_type"] == "call" else "PCS"
    headline = f"{emoji} {strat_label}_{event['close_reason']} | {event['ticker']}"
    pnl_total = round(event["pnl"] * 100, 2)  # 1 contract; options multiplier 100
    fields = [
        ("Spread",       f"{event['short_strike']:.0f}/{event['long_strike']:.0f}"),
        ("Opened",       event.get("trading_day") or "n/a"),
        ("Entry credit", f"${event['entry_credit']:.2f}"),
        ("Close debit",  f"${event['close_debit']:.2f}"),
        ("PnL/contract", f"${event['pnl']:.2f}"),
        ("PnL total",    f"${pnl_total:.2f} (1x)"),
        ("Settlement",   event["settlement_source"]),
    ]
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=12),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def _build_debit_close_embed(event, broker_name):
    win = event["pnl"] >= 0
    emoji = "✅" if win else "\U0001F6D1"
    color = COLOR_BULLISH if win else COLOR_BEARISH
    side_label = "CALL" if event["spread_type"] == "call" else "PUT"
    headline = f"{emoji} {side_label}_DEBIT_{event['close_reason']} | {event['ticker']}"
    pnl_total = round(event["pnl"] * 100, 2)
    fields = [
        ("Spread",       f"{event['short_strike']:.0f}/{event['long_strike']:.0f}"),
        ("Opened",       event.get("trading_day") or "n/a"),
        ("Entry debit",  f"${event['entry_debit']:.2f}"),
        ("Close value",  f"${event['close_value']:.2f}"),
        ("PnL/contract", f"${event['pnl']:.2f}"),
        ("PnL total",    f"${pnl_total:.2f} (1x)"),
        ("Settlement",   event["settlement_source"]),
    ]
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=12),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def _build_multi_leg_close_embed(event, broker_name):
    family = event.get("family", "IC")
    win = event["pnl"] >= 0
    emoji = "✅" if win else "\U0001F6D1"
    color = COLOR_BULLISH if win else COLOR_BEARISH
    put_leg, call_leg = event["put_leg"], event["call_leg"]
    headline = f"{emoji} {family}_CLOSED | {event['ticker']}"
    pnl_total = round(event["pnl"] * 100, 2)
    fields = [
        ("Put",       f"{put_leg['short_strike']:.0f}/{put_leg['long_strike']:.0f}  "
                      f"{put_leg['close_reason']}  ${put_leg['pnl']:.2f}"),
        ("Call",      f"{call_leg['short_strike']:.0f}/{call_leg['long_strike']:.0f}  "
                      f"{call_leg['close_reason']}  ${call_leg['pnl']:.2f}"),
        ("Opened",    event.get("trading_day") or "n/a"),
        ("PnL total", f"${pnl_total:.2f} (1x)"),
    ]
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=8),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def _build_fut_close_embed(event, broker_name):
    win = event["pnl"] >= 0
    emoji = "✅" if win else "\U0001F6D1"
    color = COLOR_BULLISH if win else COLOR_BEARISH
    headline = f"{emoji} {event['position'].upper()}_{event['close_reason']} | {event['ticker']}"
    fields = [
        ("Entry",  f"{event['entry_price']:.2f}"),
        ("Exit",   f"{event['close_price']:.2f}"),
        ("Reason", event["close_reason"]),
        ("PnL",    f"${event['pnl']:.2f}"),
    ]
    return {"embeds": [{
        "title": headline,
        "description": _track_embed_body(fields, broker_name, label_width=8),
        "color": color,
        "footer": {"text": _track_footer()},
    }]}


def build_track_close_embed(event, broker_name="schwab"):
    family = event.get("family", "CS")
    if family == "FUT":
        return _build_fut_close_embed(event, broker_name)
    if family in ("IC", "IBF"):
        return _build_multi_leg_close_embed(event, broker_name)
    if family == "DEBIT":
        return _build_debit_close_embed(event, broker_name)
    return _build_cs_close_embed(event, broker_name)


# ─── main ─────────────────────────────────────────────────────────────────────

def run(router, args, symbol="SPX", label=None, send_alert=True, note=None):
    display = label or symbol
    mid_tf  = args.timeframe

    df5    = fetch(router, symbol, "5m")
    dfmid  = fetch(router, symbol, mid_tf)
    df60   = fetch(router, symbol, "60m", bars=500)
    vix_df = fetch(router, "VIX", mid_tf)
    if df5 is None or dfmid is None or df60 is None or vix_df is None:
        raise RuntimeError(
            f"{symbol}: insufficient data (5m={'ok' if df5 is not None else 'MISSING'} "
            f"{mid_tf}={'ok' if dfmid is not None else 'MISSING'} "
            f"60m={'ok' if df60 is not None else 'MISSING'} "
            f"vix={'ok' if vix_df is not None else 'MISSING'})"
        )

    frames = {}
    for tf, df in [("5m", df5), (mid_tf, dfmid), ("60m", df60)]:
        macd, signal  = calc_macd(df["close"])
        ema           = calc_ema(df["close"])
        adx, dip, dim = calc_adx(df["high"], df["low"], df["close"])
        frames[tf] = dict(macd=macd, signal=signal, close=df["close"],
                          ema=ema, dip=dip, dim=dim, adx=adx)

    def last(d, key):
        return float(d[key].dropna().iloc[-1])

    s5   = tf_score(last(frames["5m"],  "macd"), last(frames["5m"],  "signal"),
                    last(frames["5m"],  "close"), last(frames["5m"],  "ema"),
                    last(frames["5m"],  "dip"),   last(frames["5m"],  "dim"))
    smid = tf_score(last(frames[mid_tf], "macd"), last(frames[mid_tf], "signal"),
                    last(frames[mid_tf], "close"), last(frames[mid_tf], "ema"),
                    last(frames[mid_tf], "dip"),   last(frames[mid_tf], "dim"))
    s60  = tf_score(last(frames["60m"], "macd"), last(frames["60m"], "signal"),
                    last(frames["60m"], "close"), last(frames["60m"], "ema"),
                    last(frames["60m"], "dip"),   last(frames["60m"], "dim"))
    logging.debug("%s: raw scores 5m=%d %s=%d 60m=%d", display, s5, mid_tf, smid, s60)

    def score_bar(tf, idx):
        d = frames[tf]
        return tf_score(float(d["macd"].dropna().iloc[idx]),
                        float(d["signal"].dropna().iloc[idx]),
                        float(d["close"].dropna().iloc[idx]),
                        float(d["ema"].dropna().iloc[idx]),
                        float(d["dip"].dropna().iloc[idx]),
                        float(d["dim"].dropna().iloc[idx]))

    def raw_state(r60, rmid):
        if r60 == 1  and rmid == 1:  return  1
        if r60 == -1 and rmid == -1: return -1
        if use_60m_bias and r60 != 0: return r60
        return 0

    raw_series  = [raw_state(score_bar("60m", i), score_bar(mid_tf, i)) for i in range(-3, 0)]
    trend_state = confirm_trend(raw_series, window=2)[-1]
    logging.debug("%s: raw_series=%s confirmed=%s", display, raw_series, trend_state)

    adx_val      = last(frames[mid_tf], "adx")
    dip_val      = last(frames[mid_tf], "dip")
    dim_val      = last(frames[mid_tf], "dim")
    adx_trending = adx_val >= adx_trend_th
    adx_bias     = 1 if dip_val > dim_val else (-1 if dim_val > dip_val else 0)

    vix = float(vix_df["close"].dropna().iloc[-1])

    ep5   = ema_pos(last(frames["5m"],  "close"), last(frames["5m"],  "ema"))
    epmid = ema_pos(last(frames[mid_tf], "close"), last(frames[mid_tf], "ema"))
    ep60  = ema_pos(last(frames["60m"], "close"), last(frames["60m"], "ema"))

    strategy = pick_strategy(trend_state, adx_trending, adx_bias, vix)

    tf5_txt   = "bull ▲" if s5   == 1 else "bear ▼" if s5   == -1 else "neutral"
    tfmid_txt = "bull ▲" if smid == 1 else "bear ▼" if smid == -1 else "neutral"
    tf60_txt  = "bull ▲" if s60  == 1 else "bear ▼" if s60  == -1 else "neutral"

    adx_flag = "🔥" if adx_trending else "–"
    logging.info("sp | %-6s | trend:%-8s | %-32s | adx %.1f%s | vix %.2f",
                 display, trend_text(trend_state), strategy[:32],
                 adx_val, adx_flag, vix)
    logging.debug("     5m:%-8s %s:%-8s 60m:%s  di+:%.1f di-:%.1f",
                  tf5_txt, mid_tf, tfmid_txt, tf60_txt, dip_val, dim_val)

    if send_alert:
        tag = f"MANUAL — {note}" if note else f"📍 {mid_tf} BAR"
        fields = [
            ("5m",       f"{tf5_txt}  {ema_pos_text(ep5)}"),
            (mid_tf,     f"{tfmid_txt}  {ema_pos_text(epmid)}"),
            ("60m",      f"{tf60_txt}  {ema_pos_text(ep60)}"),
            ("Trend",    f"{trend_text(trend_state)}  |  adx {adx_val:.1f}{adx_flag}  "
                         f"di+:{dip_val:.1f}  di-:{dim_val:.1f}"),
            ("Strategy", strategy),
        ]
        if note:
            fields.append(("Note", note))
        send_discord(build_embed(tag, display, trend_state, fields, broker_name=args.broker))
        logging.info("  → discord sent (%s)", display)
    else:
        logging.debug("%s: send_alert=False — discord skipped", display)

    # return both so callers can track trend changes independently
    return strategy, trend_state, {
        "tf5": tf5_txt, "tfmid": tfmid_txt, "tf60": tf60_txt,
        "ep5": ep5, "epmid": epmid, "ep60": ep60,
        "adx": adx_val, "adx_flag": adx_flag,
        "dip": dip_val, "dim": dim_val, "vix": vix,
        "price": last(frames[mid_tf], "close"),  # current spot -- needed by --track-trades
    }

# ─── continuous run helpers ───────────────────────────────────────────────────

def secs_to_next_bar(timeframe):
    """seconds until the next bar close for the given timeframe, plus 5s for
    data propagation."""
    bar_secs = TIMEFRAME_SECS.get(timeframe, 900)
    now  = time.time()
    next_bar = math.ceil(now / bar_secs) * bar_secs
    return max(5, next_bar - now + 5)


def is_open_bar(mid_tf):
    """true during the 9:30–9:45 am et bar on weekdays (rth open) -- widened
    to the first mid_tf-sized bar so 30m/1h cadences still catch the open."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_secs  = RTH_OPEN.hour * 3600 + RTH_OPEN.minute * 60
    bar_secs   = TIMEFRAME_SECS.get(mid_tf, 900)
    now_secs   = now.hour * 3600 + now.minute * 60
    return open_secs <= now_secs < open_secs + bar_secs


def _discord_full(label, tag, strat, prev_strat, d, trend_state, args, color=None):
    """send a full color-coded strategy embed to discord."""
    mid_tf = args.timeframe
    fields = [
        ("5m",    f"{d['tf5']}  {ema_pos_text(d['ep5'])}"),
        (mid_tf,  f"{d['tfmid']}  {ema_pos_text(d['epmid'])}"),
        ("60m",   f"{d['tf60']}  {ema_pos_text(d['ep60'])}"),
        ("Trend", f"{d['trend_txt']}  |  adx {d['adx']:.1f}{d['adx_flag']}  "
                  f"di+:{d['dip']:.1f}  di-:{d['dim']:.1f}"),
        ("Strategy", strat),
    ]
    if prev_strat:
        fields.append(("Previous", prev_strat))
    send_discord(build_embed(tag, label, trend_state, fields, color=color, broker_name=args.broker))


def _track_classify(strategy):
    """maps a pick_strategy() label to (family, side) for --track-trades:
    family in {"CS", "IC", "IBF", "DEBIT"}, side in {"put", "call", None}
    (side is None for IC/IBF, which trade both sides). Returns None for
    labels with no tracked trade (wait-for-direction, pause)."""
    if strategy.startswith("bull put credit spread (pcs)"):
        return "CS", "put"
    if strategy.startswith("bear call credit spread (ccs)"):
        return "CS", "call"
    if strategy.startswith("neutral — iron condor"):
        return "IC", None
    if strategy.startswith("neutral — iron butterfly"):
        return "IBF", None
    if strategy.startswith("bull call debit spread"):
        return "DEBIT", "call"
    if strategy.startswith("bear put debit spread"):
        return "DEBIT", "put"
    return None


def _track_entry_and_manage(symbol, label, strategy, price, open_bar, now, args):
    """--track-trades: for futures, hand off to the continuous long/short
    tracker (no options chain, no daily catch-window). For equity/index
    tickers: catch one directional/neutral regime near the open per ticker
    per day across every family (CS/IC/IBF/DEBIT) -- real strikes/TP/SL,
    JSON+Discord only -- then manage any already-open tracked position
    every cycle."""
    if is_futures_ticker(symbol):
        _track_futures_entry_and_manage(symbol, label, strategy, price, now, args)
        return
    today = now.strftime("%Y-%m-%d")
    entry_attempted = _track_daily_state.setdefault("entry_attempted", {})
    already_open = symbol in _track_positions

    # The date-guard (not open_bar) is the real gate: open_bar is the
    # *normal* moment this fires (the first bar of the session), but if a
    # restart causes the bot to miss that exact bar, the first cycle that
    # runs afterward still gets one catch attempt for the day rather than
    # silently losing the whole day's pick.
    if not already_open and entry_attempted.get(symbol, {}).get("day") != today:
        classification = _track_classify(strategy)
        opened = False
        if classification is not None:
            family, side = classification
            try:
                position = open_tracked_position(symbol, family, side, price, now)
                _track_positions[symbol] = position
                opened = True
                send_discord(build_track_entry_embed(position, broker_name=args.broker))
                side_txt = f" {side}" if side else ""
                logging.info("track: opened %s %s%s%s", symbol, family, side_txt,
                             "" if open_bar else " (catch-up, missed open_bar)")
            except Exception as e:
                logging.warning("track: no valid %s trade for %s near open: %s", family, symbol, e)
        entry_attempted[symbol] = {"day": today, "fired": opened}
        save_track_daily_state(_track_daily_state)
        if opened:
            save_track_positions(_track_positions)

    if symbol in _track_positions:
        close_event = manage_tracked_position(_track_positions[symbol], price, now)
        if close_event is not None:
            send_discord(build_track_close_embed(close_event, broker_name=args.broker))
            logging.info("track: closed %s %s reason=%s pnl=%.2f",
                         symbol, close_event.get("family", "CS"), close_event["close_reason"], close_event["pnl"])
            del _track_positions[symbol]
            save_track_positions(_track_positions)


def run_all(router, args, symbol_labels, send_alert, note, prev_strategies, prev_trends, prev_adx):
    """scan all symbols; fire discord on open bar, trend change, adx flip, or
    strategy change. tickers outside their own session window (rth for
    equities/indices, the ~23hr futures session for futures) are skipped
    entirely for this cycle -- no fetch, no alert."""
    mid_tf = args.timeframe
    open_bar = is_open_bar(mid_tf)
    now = datetime.now(ET)
    for symbol, label in symbol_labels:
        if not symbol_session_ok(symbol, now):
            logging.debug("%s: outside session window (%s) — skipping this cycle",
                          label, "futures" if is_futures_ticker(symbol) else "rth")
            continue
        try:
            strategy, trend_state, d = run(router, args, symbol, label=label,
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
                    _discord_full(label, tag, strategy, None, d, trend_state, args)

                else:
                    # trend change — highest priority non-open alert
                    if trend_changed:
                        old_t = trend_text(prev_t)
                        new_t = trend_text(trend_state)
                        tag   = f"📊 TREND: {old_t} → {new_t}"
                        logging.info("trend change %s: %s → %s  strategy: %s",
                                     label, old_t, new_t, strategy)
                        _discord_full(label, tag, strategy, prev_s, d, trend_state, args)

                    # adx momentum flip — fires independently of trend change
                    if adx_flipped:
                        implication = ("credit spreads unlocked" if adx_trending
                                       else "credit spreads locked — prefer debits")
                        adx_tag = (f"⚡ ADX TRENDING ({d['adx']:.1f} ≥ {adx_trend_th:.0f})"
                                   if adx_trending else
                                   f"⚡ ADX FLAT ({d['adx']:.1f} < {adx_trend_th:.0f})")
                        fields = [
                            ("Trend",    f"{d['trend_txt']}  |  adx {d['adx']:.1f}{d['adx_flag']}  "
                                        f"di+:{d['dip']:.1f}  di-:{d['dim']:.1f}"),
                            ("Momentum", implication),
                            ("Strategy", strategy),
                        ]
                        send_discord(build_embed(adx_tag, label, trend_state, fields, broker_name=args.broker))
                        logging.info("adx flip %s: %s → %s  adx=%.1f  %s",
                                     label,
                                     "trending" if prev_a else "flat",
                                     "trending" if adx_trending else "flat",
                                     d["adx"], implication)

                    # strategy changed without a trend or adx flip
                    elif strat_changed and not trend_changed:
                        tag = "🔄 STRATEGY CHANGED"
                        logging.info("strategy change %s: %s → %s", label, prev_s, strategy)
                        _discord_full(label, tag, strategy, prev_s, d, trend_state, args)
            else:
                logging.debug("%s: send_alert=False — discord skipped", label)

            prev_strategies[label] = strategy
            prev_trends[label]     = trend_state
            prev_adx[label]        = adx_trending

            if args.track_trades:
                _track_entry_and_manage(symbol, label, strategy, d["price"], open_bar, now, args)

        except Exception as e:
            logging.error("✗ %s: %s", label, e)

    return prev_strategies, prev_trends, prev_adx


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"strategy-picker v{VERSION} — options strategy router")
    parser.add_argument("--tickers", "-t",  nargs="+", type=str, default=None,
                        help="symbols to run (e.g. SPX QQQ ES NQ); defaults to SPX + QQQ")
    parser.add_argument("--run", "-r",      choices=["once", "continuous"], default="once",
                        help="once=run now (default); continuous=loop every bar")
    parser.add_argument("--timeframe", "--interval", dest="timeframe", metavar="TF",
                        choices=["5m", "15m", "30m", "60m", "1h"], default="15m",
                        help="mid timeframe + continuous-loop bar cadence (default 15m)")
    parser.add_argument("--broker", choices=["schwab", "yfinance"], default="schwab",
                        help="primary data broker (default schwab)")
    parser.add_argument("--broker-fallback", choices=["yfinance", "none"], default="yfinance",
                        help="fallback data broker if primary fails (default yfinance)")
    parser.add_argument("--note", "-n",     type=str, default=None,
                        help="context label for discord post (manual runs)")
    parser.add_argument("--discord",        metavar="TARGET", default="default",
                        help="discord webhook target: 'default' for DISCORD_WEBHOOK_STRATEGY "
                             "(from ingestion.env), a full webhook URL, or an env var name "
                             "(default: default)")
    parser.add_argument("--no-discord",     action="store_true",
                        help="disable discord entirely, overriding --discord")
    parser.add_argument("--debug",          action="store_true",
                        help="print debug to console (always written to log file)")
    parser.add_argument("--state-tag",      default=None,
                        help="Arbitrary run label (e.g. 'stocks', 'indices', 'futures') shown in the "
                             "Discord footer so messages from different runs are distinguishable.")
    parser.add_argument("--no-track-trades", action="store_true",
                        help="Disable the directional CS (PCS/CCS) trade-tracking feature (real "
                             "strikes/TP/SL off a live Schwab chain, tracked locally in JSON, no DB, "
                             "reported to Discord). On by default; overriding --track-trades. "
                             "Equity/index tickers only -- futures are skipped regardless (Schwab "
                             "/chains has no futures options).")
    args = parser.parse_args()
    args.track_trades = not args.no_track_trades

    setup_logging(args.debug)
    logging.debug("args: %s", vars(args))

    if NOTIFIER is not None:
        NOTIFIER.state_tag = args.state_tag

    if args.no_discord:
        discord_url = ""
    elif NOTIFIER is not None:
        discord_url = NOTIFIER.resolve_webhook(args.discord, logger=logging.getLogger())
    else:
        discord_url = os.getenv("DISCORD_WEBHOOK_STRATEGY", "") if args.discord == "default" else os.getenv(args.discord, "")
    logging.debug("discord target=%s -> %s", args.discord, "resolved" if discord_url else "unresolved/empty")

    if args.tickers:
        symbol_labels = [(t.upper(), label_map.get(resolve(t.upper()), t.upper())) for t in args.tickers]
    else:
        symbol_labels = [("SPX", "SPX"), ("QQQ", "QQQ")]

    any_futures = any(is_futures_ticker(s) for s, _ in symbol_labels)
    logging.info("symbols: %s | mode: %s | timeframe: %s | session: %s | broker: %s (fallback: %s)",
                 [lbl for _, lbl in symbol_labels], args.run, args.timeframe,
                 "23hr futures" if any_futures else "rth", args.broker, args.broker_fallback)

    router = build_broker_router(args)
    send_alert = not args.no_discord

    if send_alert:
        send_discord(build_session_start_embed(symbol_labels, args, any_futures))
        logging.info("session start: discord sent")
    post_signal_event(build_session_start_event(symbol_labels, args, any_futures))

    if args.run == "once":
        for symbol, label in symbol_labels:
            now = datetime.now(ET)
            ok = symbol_session_ok(symbol, now)
            if not ok:
                logging.info("%s: outside its session window (%s) — will compute but skip discord",
                             label, "futures" if is_futures_ticker(symbol) else "rth")
            try:
                run(router, args, symbol, label=label, note=args.note, send_alert=send_alert and ok)
            except Exception as e:
                logging.error("✗ %s: %s", label, e)

    else:  # continuous
        logging.info("continuous mode — %s bars — symbols: %s",
                     args.timeframe, [lbl for _, lbl in symbol_labels])
        logging.info("alerts: open bar (9:30 ET) + trend change + strategy change; "
                     "equities/indices only alert during RTH, futures across the 23hr session")
        prev_s, prev_t, prev_a = {}, {}, {}
        while True:
            now = datetime.now(ET)
            if not any_futures and not is_rth(now):
                secs = seconds_until_rth_open(now)
                logging.info("no futures tickers, market closed — sleeping %.0fs until next RTH open (%s ET)",
                             secs, datetime.fromtimestamp(time.time() + secs, tz=ET).strftime("%a %H:%M"))
                time.sleep(secs)
                continue
            if any_futures and not is_futures_session(now):
                secs = seconds_until_futures_session(now)
                logging.info("outside futures session — sleeping %.0fs until session reopen (%s ET)",
                             secs, datetime.fromtimestamp(time.time() + secs, tz=ET).strftime("%a %H:%M"))
                time.sleep(secs)
                continue

            prev_s, prev_t, prev_a = run_all(router, args, symbol_labels, send_alert,
                                             args.note, prev_s, prev_t, prev_a)
            secs = secs_to_next_bar(args.timeframe)
            logging.info("next bar in %.0fs  (%s ET)",
                         secs, datetime.fromtimestamp(time.time() + secs,
                         tz=ET).strftime("%H:%M:%S"))
            time.sleep(secs)
