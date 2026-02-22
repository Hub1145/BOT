# Strategy 5: Synthetic Intelligence Screener (v2.0)

Strategy 5 v2.0 is a highly optimized trading engine specifically designed for Deriv Volatility Indices. It recognizes that synthetic markets are mathematically generated and focuses on pure price action algorithms by stripping away indicator noise and lag.

The engine splits into two distinct profiles: **The Scalper (Rise/Fall)** and **The Day Trader (Multiplier)**.

## 1. Upgraded Multi-Timeframe Architecture

The timeframe hierarchy is separated based on the trading mode:

### Mode A: Rise & Fall (Scalping)
*   **Daily:** Ignored.
*   **1-Hour (Macro Bias):** Determines the "Do Not Trade Against" direction and maps major Support/Resistance.
*   **15-Minute (Momentum Bias):** Confirms intraday wave expansion or exhaustion.
*   **5-Minute (Core Setup):** Primary timeframe for indicator suite and block scoring.
*   **1-Minute (Trigger & Entry):** Precise execution (pullbacks, pin bars, engulfing).

### Mode B: Multiplier (Day Trading)
*   **Daily (Macro Trend):** Sets the global regime and used for Pivot calculations.
*   **1-Hour (Core Setup):** Primary timeframe for indicator scoring and trend validation.
*   **15-Minute (Structure):** Used to find optimal pullback zones.
*   **5-Minute (Trigger):** Timed entry into the 1H/Daily trend.
*   **1-Minute:** Ignored.

---

## 2. Optimized Intelligence Engine

The engine focuses on the sharpest tools to eliminate redundancy and lag.

### A) Trend Block (Regime Filter)
*   **EMA 50 & EMA 200:** Golden standard for trend bias.
*   **SuperTrend Indicator:** Algorithmic indicator that responds perfectly to synthetic markets.
*   **ADX (Average Directional Index):** Trend strength filter. Multiplier trades are disabled if ADX < 20.

### B) Momentum Block (Velocity & Exhaustion)
*   **RSI (14):** Base momentum.
*   **Stoch RSI:** Highly sensitive for trigger timing.
*   **MACD (12, 26, 9):** Strictly used for **Divergence Detection**, not crossovers.

### C) Volatility Block (Expansion)
*   **ATR (Average True Range):** Used for dynamic Stop Loss and Multiplier sizing.
*   **Bollinger Bands (20, 2):** Used for mean-reversion (Rise/Fall) and volatility breakouts (Multipliers).

### D) Structure Block (Geometry)
*   **Auto Support & Resistance (HTF SNR):** Maps 1H and 15m order blocks and rejection zones.
*   **Price Distance from EMA 50:** Measures overextension.

---

## 3. Dynamic Scoring & Execution Logic

### Mode A: RISE & FALL LOGIC (Scalping)
*   **Goal:** Quick strikes exploiting mean reversion and momentum exhaustion.
*   **Weighting:** Structure (40%), Momentum (40%), Volatility (20%), Trend (0%).
*   **Signal Trigger (>= 65% Confidence):**
    *   Price touches 1H SNR or 15m outer Bollinger Band.
    *   5m Stoch RSI is crossing back.
    *   1m chart prints a reversal candle.
*   **Dynamic Expiry:**
    *   Triggered on 1m Reversal: 3 to 5 Minutes.
    *   Triggered on 5m Reversal: 10 to 15 Minutes.

### Mode B: MULTIPLIER LOGIC (Day Trading)
*   **Goal:** Catching large, sustained moves.
*   **Weighting:** Trend (50%), Volatility (30%), Structure (20%), Momentum (Filter).
*   **Signal Trigger (>= 75% Confidence):**
    *   1H EMA 50 > EMA 200 and ADX > 25.
    *   Price pulls back to 15m EMA 50 or SuperTrend line.
    *   5m chart shows momentum resuming.
*   **ATR-Based Multiplier Selection:**
    *   High Volatility (High ATR): 10x - 20x Multiplier.
    *   Low Volatility (Low ATR): 50x Multiplier.
*   **Target Levels:**
    *   Stop Loss: 1.5x 1H ATR.
    *   Take Profit: 3.0x 1H ATR (1:2 RR minimum).

---

## 4. Advanced Position Management

### For Multiplier (Day Trading)
*   **Free Ride Protocol:** SL moved to Entry Price + margin once profit reaches **1.5 ATR**.
*   **SuperTrend Trailing:** Profit is allowed to run as long as the **15-Minute SuperTrend** holds.
*   **Divergence Hard Exit:** Position closed immediately if **1H MACD** prints a valid divergence against the trade.

### For Rise & Fall (Scalping)
*   **Late Entry Penalty:** Trade cancelled if the 1m candle has already moved more than 30% of its average ATR.
*   **Volatility Freeze:** Execution paused if 1m ATR drops below baseline threshold.
