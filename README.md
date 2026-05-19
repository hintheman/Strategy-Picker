***

# Strategy Picker (Options Strategy Guide)

**Strategy Picker** is a TA‑driven “strategy router” for intraday trading.  
It reads market trend, volatility, and regime, then suggests which options structure to favor right now:

- Bull with PCS → Bullish; favor **Put Credit Spreads**  
- Bear with CCS → Bearish; favor **Call Credit Spreads**  
- Neutral with Iron Condor → Range; favor **Iron Condors** (optional)  
- Debit Trade → Trend unclear or vol low; favor **directional debits** (calls/puts/spreads)  
- Pause for reckoning and coffee → Conditions messy; avoid new risk and collect more info  

The indicator is designed to be stand‑alone, and works on SPX, NDX, QQQ, SPY, futures, and most liquid stocks.

***

## How it works

Strategy Picker combines three components:

1. **Triple‑screen trend tracking (15m + 60m)**  
   - Uses MACD (12/26/9) and EMA 8 on 15‑minute and 60‑minute timeframes.  
   - 60m and 15m must both be bullish to call a **Bull trend**, both bearish for a **Bear trend**; otherwise **Neutral**.  
   - A 2‑bar confirmation smooths noise: the raw trend must persist for 2 bars before TrendState flips.

2. **Trend strength via ADX**  
   - ADX(14) on the chart timeframe measures how strong the trend is.  
   - The orange line in the Strategy Picker pane is **ADX (trend strength)**.  
   - The faint horizontal orange line is **“ADX Threshold (trend vs range)”** (default 20).  
   - Below the threshold: weak / range‑bound; above: strong enough to trust trend strategies. [tradingview](https://www.tradingview.com/pine-script-docs/primer/first-indicator/)

3. **Volatility / options regime via VIX**  
   - VIX is pulled on 15‑minute bars.  
   - Approximate regimes:
     - VIX < 16 → low vol; premiums cheap → better for **Debit Trade**.  
     - 16–19 with low ADX → **Neutral with Iron Condor** (if enabled).  
     - VIX ≥ 19 with strong Bull/Bear trend → **PCS/CCS** in the direction of the trend.  
     - VIX ≥ 19 with Neutral/unclear trend → **Pause**.

Putting it together, the script picks one strategy at all times after the initial wait period.

***

## Panel and visuals

When added to a 15‑minute chart with `overlay=false`, Strategy Picker shows:

- **Blue TrendState plot**  
  - 1 = Bull, −1 = Bear, 0 = Neutral (you can hide this in Style if you only want the table).

- **Orange ADX line**  
  - `ADX (trend strength)` in the legend.  
  - Higher values = stronger trend; weak values = chop / range. [avatrade](https://www.avatrade.com/education/technical-analysis-indicators-strategies/adx-indicator-trading-strategies)

- **Orange horizontal line**  
  - Labeled `ADX Threshold (trend vs range)`.  
  - Default 20; can be adjusted in Inputs.

- **Top‑left table**  
  - Row 1:  
    - Trend: `Bull / Bear / Neutral`  
    - ADX: current ADX value  
  - Row 2:  
    - Strategy: one of `Bull with PCS / Bear with CCS / Neutral with Iron Condor / Debit Trade / Pause for reckoning and coffee`  
    - VIX: current VIX value  

This makes it easy to see, at a glance, *why* a given strategy is being suggested.

***

## Inputs

Key user‑configurable inputs:

- **MACD Fast / Slow / Signal lengths** (defaults 12/26/9)  
- **EMA Length** for trend filter (default 8)  
- **ADX Length** (default 14) and **ADX Trend Threshold** (default 20)  
- **VIX thresholds**:
  - `VIX Low (Debit)`  
  - `VIX IC Low` / `VIX IC High` (IC window)  
  - `VIX CS Threshold` (credit‑spread regime)  
- **Allow Iron Condor when Neutral** (on/off)  
- **Skip wait period at open?** (default false)  
- **Bars to wait after RTH open** (default 2 ≈ 30 minutes on 15m)

***

## Alerts and old/new strategy

The script defines two alert conditions:

- **Strategy Changed** – fires whenever the picked strategy text changes after confirmation.  
- **Trend Change** – fires when TrendState flips after 2‑bar confirmation.

Inside the code:

- `strategyPrev` (via `oldStrategy`) holds the **previous strategy name**.  
- `strategy` holds the **new strategy name**.

Pine v6 requires `alertcondition()` messages to be constant strings, so old/new names are not injected directly into the alert text. Instead:

- Use `Strategy Changed` as a generic alert condition.  
- Let your webhook/Discord bot keep track of the last strategy it saw per symbol, and build messages like  
  `"SPX | Strategy: Pause -> Bull with PCS"`  
  using `strategyPrev` vs `strategy` in your external logic.

***

## Recommended usage

- Run on **15‑minute charts** for SPX, MES, NQ, QQQ, SPY, or large‑cap stocks.  
- Wait until ~30 minutes after the RTH open (or enable `skipWait`) before acting on suggestions.  
- Use Strategy Picker as a **guide**, not an execution system:
  - If it says `Bull with PCS`, think in terms of selling bullish put credit spreads.  
  - If it says `Debit Trade`, think in terms of directional calls/puts or debit spreads.  
  - If it says `Pause for reckoning and coffee`, respect the chop and reduce risk.

License
This project is licensed under the terms of the MIT License.

***
