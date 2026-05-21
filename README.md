# Strategy Picker v2.1 (Options Strategy Guide)

**Strategy Picker** is a TA-driven "strategy router" for intraday options trading.  
It reads market trend, momentum, volatility, and EMA 8 position across three timeframes, then suggests which options structure to favor right now:

- **Bull → PCS** — Bullish trend confirmed; favor **Put Credit Spreads**
- **Bear → CCS** — Bearish trend confirmed; favor **Call Credit Spreads**
- **Neutral → Iron Condor** — Range-bound; favor **Iron Condors** (optional)
- **Neutral → Iron Butterfly** — Tight range, elevated VIX; favor **Iron Butterfly** (optional)
- **Debit Trade** — Trend unclear or vol low; favor **directional debits** (calls/puts/spreads)
- **Pause for reckoning and coffee** — Conditions messy; avoid new risk and gather more data

The indicator is designed to be stand-alone and works on SPX, NDX, QQQ, SPY, futures, and most liquid stocks.

---

## How it works

Strategy Picker combines four components:

### 1. Triple-Screen Trend Tracking (5m + 15m + 60m)

The indicator now uses **three timeframes** in a classic Elder Triple Screen structure:

| Timeframe | Role | Weight |
|-----------|------|--------|
| **60m** | Primary trend anchor — "the tide" | Highest |
| **15m** | Intermediate confirmation — "the wave" | Medium |
| **5m** | Entry timing / early signal — "the ripple" | Lowest |

Each timeframe is evaluated independently using **MACD (12/26/9)** and **EMA 8**:
- A timeframe is **Bull** when: MACD > 0, MACD > Signal, and Close > EMA 8
- A timeframe is **Bear** when: MACD < 0, MACD < Signal, and Close < EMA 8
- Otherwise it is **Neutral**

The **confirmed TrendState** is derived from 60m + 15m agreement:
- Both Bull → **Bull**
- Both Bear → **Bear**
- Disagreement → if `Use 60m as primary trend` is enabled, 60m wins; otherwise **Neutral**

A **2-bar confirmation** smooths noise: the raw trend must persist for 2 consecutive bars before TrendState flips.

The 5m timeframe is displayed in the table as an early-warning signal but does **not** drive the confirmed TrendState. Think of it as a heads-up that momentum may be shifting before the 15m and 60m catch up.

---

### 2. EMA 8 — Price Position as a Directional Filter

**EMA 8** plays a dual role: it is part of the trend state calculation *and* displayed separately per timeframe as a price position indicator.

Each timeframe shows one of three states:

| Symbol | Meaning |
|--------|---------|
| **↑ EMA 8** | Price is **above** EMA 8 — bullish momentum, trend is supported |
| **@ EMA 8** | Price is **at** EMA 8 (within 0.05%) — decision zone, potential bounce or break |
| **↓ EMA 8** | Price is **below** EMA 8 — bearish momentum, trend is under pressure |

**Why EMA 8 matters:**  
Many traders — including several prominent YouTubers and short-term traders — use EMA 8 exclusively as a trade filter. Price tends to bounce off EMA 8 as dynamic support in a bull trend, or reject it as resistance in a bear trend. When price crosses through EMA 8 on the 60m, it often precedes a meaningful move.

**Reading the EMA 8 column in context:**
- All three timeframes showing **↑ EMA 8** with Bull trend = high-conviction bull setup
- 5m showing **↓ EMA 8** while 15m/60m show **↑ EMA 8** = 5m pullback inside an uptrend, potential PCS entry
- 60m showing **↓ EMA 8** while 5m/15m are neutral = caution, larger trend may be turning
- All three showing **@ EMA 8** = coiling, breakout or breakdown imminent

---

### 3. Trend Strength via ADX

**ADX(14)** measures how strong (or weak) the current trend is, regardless of direction.

- The **orange line** in the Strategy Picker pane is ADX (trend strength).
- The **faint horizontal orange line** is the ADX Threshold (default 20).
- **Below threshold** → weak / range-bound; trend strategies carry more risk.
- **Above threshold** → trend is strong enough to trust directional strategies.

ADX is used as a gate for credit spreads:
- Credit spreads require both a directional trend **and** ADX ≥ threshold.
- When VIX is in the mid-range (18–19), ADX trending is required before full credit spread size is allowed; below that level, size is reduced.

The **DI+ and DI-** lines (displayed in the Trend row) show which direction the ADX energy is pointing:
- DI+ > DI- = bullish pressure
- DI- > DI+ = bearish pressure

---

### 4. Volatility / Options Regime via VIX

VIX is pulled on 15-minute bars from `TVC:VIX` and used to determine which options structure is appropriate given the current premium environment:

| VIX Level | Regime | Strategy |
|-----------|--------|----------|
| < 16 | Low vol, cheap premiums | Debit spreads (directional) |
| 16–19, ADX flat | Sideways, vol mild | Iron Condor (if enabled) |
| 19–23, ADX flat | Tight range, vol elevated | Iron Butterfly (if enabled) |
| ≥ 18 with ADX trending | Trend + vol confirmed | Credit Spreads (reduced size) |
| ≥ 19 with ADX trending | Full credit spread regime | Credit Spreads (full size) |
| > 23, no trend | High vol, no direction | Pause |

VIX thresholds are all user-configurable in the Inputs panel.

---

### Putting it all together

The strategy decision flows as follows:

```
TrendState (60m+15m confirmed)
    └── Bull or Bear?
            └── ADX trending? → YES → VIX ≥ CS threshold? → PCS / CCS
                               → NO  → VIX low? → Debit Trade
    └── Neutral?
            └── VIX < 16 → Debit Trade (with ADX bias direction)
            └── VIX 19–23, ADX flat → Iron Butterfly
            └── VIX 16–19, ADX flat → Iron Condor
            └── VIX > 23, ADX flat → Pause
```

The **5m EMA 8 position** and the **ADX DI+/DI- readings** act as secondary confirmation — they don't change the strategy output but help you judge the quality of the signal before entering.

---

## Panel and Visuals

When added to any chart with `overlay=false`, Strategy Picker shows:

**Plots:**
- **Blue TrendState line** — 1 = Bull, −1 = Bear, 0 = Neutral (hideable in Style tab)
- **Orange ADX line** — higher = stronger trend; below threshold = chop
- **Faint orange horizontal line** — ADX Threshold (default 20)

**Top-right table (6 rows):**

| Row | Label col (black) | Direction col | Detail col |
|-----|-------------------|---------------|------------|
| 0 | *(spacer)* | | |
| 1 | **5m** | Bull ▲ / Bear ▼ / Neutral | ↑ ↓ @ EMA 8 |
| 2 | **15m** | Bull ▲ / Bear ▼ / Neutral | ↑ ↓ @ EMA 8 |
| 3 | **60m** | Bull ▲ / Bear ▼ / Neutral | ↑ ↓ @ EMA 8 |
| 4 | **Trend** | Bull / Bear / Neutral | ADX value + DI+/DI- |
| 5 | **Strategy** | Full strategy name + DTE | VIX value |

Color coding:
- **Green** = Bullish (semi-transparent on TF rows, more solid on Trend/Strategy)
- **Red** = Bearish (same hierarchy)
- **Grey** = Neutral
- **Black** = Label column always

---

## Inputs

| Group | Input | Default | Notes |
|-------|-------|---------|-------|
| MACD / EMA | Fast / Slow / Signal | 12 / 26 / 9 | Standard MACD |
| MACD / EMA | EMA Length | 8 | EMA 8 — classic short-term trend filter |
| ADX | ADX Length | 14 | Standard DMI period |
| ADX | ADX Trend Threshold | 20 | Below = range; above = trend |
| VIX Thresholds | VIX Low (Debit only) | 16 | Below this → only debit trades |
| VIX Thresholds | VIX IC Low / High | 16 / 19 | Iron Condor window |
| VIX Thresholds | VIX IBF Low / High | 19 / 23 | Iron Butterfly window |
| VIX Thresholds | VIX Mid (CS reduced) | 18 | CS allowed with ADX, reduced size |
| VIX Thresholds | VIX High (CS full) | 19 | CS allowed at full size |
| DTE Guidance | Credit Spreads DTE | 0 | Same-day; shown in strategy label |
| DTE Guidance | IC / IBF DTE | 5 | 3–7 DTE range |
| DTE Guidance | Debit Spreads DTE | 14 | 14–21 DTE range |
| Options | Allow Iron Condor | true | Toggle IC suggestions |
| Options | Allow Iron Butterfly | true | Toggle IBF suggestions |
| Options | Skip wait at open | false | If false, waits N bars after RTH open |
| Options | Bars to wait at open | 2 | Default ≈ 10–30 min depending on TF |
| Options | Use 60m as primary trend | true | 60m wins when TFs disagree |

---

## Alerts

Two alert conditions are defined:

- **Strategy Changed** — fires when the strategy text changes after bar confirmation.
- **Trend Changed** — fires when TrendState flips after 2-bar confirmation.

Alert message format:
```
Strategy Picker | SPX | Prev: Bear Call Credit Spread (CCS) [0 DTE] → Now: Pause for reckoning and coffee
Strategy Picker | SPX | Trend: Bear → Neutral
```

> **Note:** Pine Script v6 requires `alertcondition()` message strings to be constant. Old/new strategy names are injected via `alert()` calls, not `alertcondition()`. For webhook/Discord bot integration, parse the `Prev:` and `Now:` fields from the alert message.

---

## Recommended Usage

- Works on any timeframe but optimized for **15-minute charts** on SPX, MES, NQ, QQQ, SPY, or large-cap stocks.
- Wait ~10–30 minutes after RTH open before acting on suggestions (or enable `Skip wait at open`).
- Use as a **guide, not an execution system**:
  - `Bull → PCS` → consider selling a put credit spread below current price
  - `Bear → CCS` → consider selling a call credit spread above current price
  - `Neutral → Iron Condor` → consider a defined-risk range trade
  - `Debit Trade` → consider directional calls/puts or a debit spread
  - `Pause for reckoning and coffee` → reduce risk, wait for clearer signal

**Reading the table quickly:**
1. Glance at **5m / 15m / 60m** rows — are they aligned? All green = high confidence bull. All red = high confidence bear. Mixed = caution.
2. Check the **EMA 8 column** — is price above or below on the 60m? That tilts the bias.
3. Check **Trend row** — is ADX 🔥 (trending)? That unlocks credit spreads.
4. Read **Strategy row** — the final output, with DTE guidance and VIX context.

---

## License

This project is licensed under the terms of the MIT License.
