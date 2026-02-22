# Strategy 5: Synthetic Intelligence Screener (v2.1)

Strategy 5 v2.1 is an advanced trading engine specifically optimized for Deriv Volatility Indices. This version focuses on eliminating noise, correcting leverage logic, and introducing structural geometry (Fractals & Order Blocks) to achieve high-precision entries.

## 1. Multi-Timeframe Architecture (v2.1 Refined)

### Mode A: Rise & Fall (Scalping)
*   **1-Hour (Macro Bias):** Determines "Do Not Trade Against" direction and maps major SNR.
*   **15-Minute (Momentum Bias):** Confirms intraday wave expansion.
*   **5-Minute (Core Setup):** Primary timeframe for indicator suite and **Fractal Detection**.
*   **1-Minute (Trigger & Entry):** Precise execution via reversal candle confirmation.

### Mode B: Multiplier (Day Trading)
*   **Daily (Macro Trend):** Sets global regime and Pivot calculations.
*   **1-Hour (Core Setup):** Primary timeframe for indicator scoring and **Order Block Detection**.
*   **15-Minute (Structure):** Used for pullback identification (EMA 50 / SuperTrend).
*   **5-Minute (Momentum):** Confirms resumption of trend after a pullback.
*   **1-Minute (Execution):** Final entry confirmation candle (Precision Leg).

---

## 2. The v2.1 Intelligence Engine

### A) Trend Block (Regime Filter)
*   **EMA 50 & EMA 200:** Trend bias golden standard.
*   **SuperTrend:** Algorithmic trend following.
*   **ADX:** Trend strength filter. Higher ADX (>30) unlocks higher leverage.

### B) Momentum Block
*   **RSI (14) & Stoch RSI:** Velocity and trigger timing.
*   **MACD Divergence:** Identifies structural exhaustion (Divergence only, no crossovers).

### C) Volatility Block
*   **ATR (Average True Range):** Measures volatility expansion. Used for dynamic multiplier selection.
*   **Bollinger Bands:** Identifies band-walks (trend) and rejections (range).

### D) Structure Block (v2.1 Geometry)
*   **5m Fractals (Scalping):** Detects recent swing highs/lows for precise retest entries.
*   **1H Order Blocks (Multiplier):** Identifies institutional "Order Flow" zones (last opposite candle before an impulse) for high-probability pullback entries.
*   **Price Distance from EMA 50:** Measures overextension.

---

## 3. Dynamic Scoring & Thresholds

### Mode A: RISE & FALL (Scalping)
*   **Confidence Threshold:** **>= 72%** (Strict filter for binary outcomes).
*   **Weighting:** Structure (40%), Momentum (40%), Volatility (20%).
*   **Logic:** Requires a 5m Fractal retest or 1H SNR touch + 1m reversal candle.

### Mode B: MULTIPLIER (Day Trading)
*   **Confidence Threshold:** **>= 68%** (Intervention possible via position management).
*   **Weighting:** Trend (50%), Volatility (30%), Structure (20%).
*   **Precision Entry:** Once 1H/15m/5m conditions align, requires a **1m confirmation candle** in the signal direction.

### v2.1 Multiplier Selection (Corrected)
Leverage is tied to Volatility and Trend Strength:
*   **High Volatility (High ATR) + Strong Trend (ADX > 30):** 20x - 50x Multiplier.
*   **Medium Volatility (ADX > 20):** 10x - 20x Multiplier.
*   **Low Volatility (Compression):** 5x - 10x Multiplier (Wait for breakout).

---

## 4. Advanced Position & Risk Management

### Adaptive Sensitivity (Streak Tracking)
The engine learns from recent performance. If **3+ consecutive losses** occur on a symbol:
1.  The confidence threshold is automatically increased by **10%**.
2.  Filters become stricter until a winning trade resets the streak.

### Multiplier Decision Engine
*   **Free Ride Protocol:** SL moved to Entry + small margin once profit reaches **1.5 ATR**.
*   **SuperTrend Trailing:** Position trails the **15-Minute SuperTrend** line once in "Free Ride".
*   **Divergence Hard Exit:** Immediate exit if a **1H MACD Divergence** prints against the position.

### Scalping Protections
*   **Late Entry Penalty:** Trade cancelled if 1m candle body exceeds 30% of its average ATR.
*   **Volatility Freeze:** Execution paused if 1m ATR drops below baseline (market consolidation).
