# Strategy 5: Unified Market Intelligence Screener

Strategy 5 is the most advanced trading engine in the bot, designed to act as a comprehensive "Intelligence Screener." It utilizes a multi-timeframe architecture and a weighted scoring system across four distinct analytical blocks to determine high-probability trade setups.

## 1. Multi-Timeframe Architecture

The strategy concurrently monitors five different timeframes to ensure global, intermediate, and local alignment:

*   **Global Trend (Daily):** Used for pivot point calculations and long-term trend alignment.
*   **Intermediate Bias (4-Hour):** Filters the overall market regime.
*   **Core Intelligence (1-Hour):** The primary timeframe where the exhaustive indicator suite is calculated.
*   **Confirmation Bias (15-Minute):** Ensures the immediate preceding momentum aligns with the core signal.
*   **Local Timing (1-Minute):** Used for precise entry execution and price action pattern confirmation.

---

## 2. The Intelligence Engine (4-Block Scoring)

The bot calculates a **Sentiment Score** for four blocks, which are then weighted into a final **Confidence Score**.

### A) Trend Block (Weight: 3)
Determines the dominant market direction and strength.
*   **EMA 50 & EMA 200:** Checks for price positioning and moving average crossovers.
*   **SMA 20:** Short-term trend tracking.
*   **ADX (Average Directional Index):** Measures trend strength (requires > 25 for strong trend bonus).
*   **Ichimoku Cloud:** Checks if price is above/below Span A and Span B.
*   **MACD:** Alignment of MACD line vs. Signal line.
*   **Aroon Indicator:** Identifies when a new trend is starting.
*   **DPO (Detrended Price Oscillator):** Removes trend to identify cycles.
*   **KST (Know Sure Thing):** Smoothed rate-of-change trend indicator.
*   **TRIX:** Triple exponentially smoothed moving average.
*   **Vortex Indicator:** Identifies the start of a new trend.
*   **WMA (Weighted Moving Average):** Faster reaction to price changes.

### B) Momentum Block (Weight: 2)
Measures the velocity of price movements to identify breakouts and exhaustions.
*   **RSI (Relative Strength Index):** Thresholds at 50 for bullish/bearish bias.
*   **Stoch RSI:** Measures RSI relative to its range (0.5 threshold).
*   **Williams %R:** Identifies overbought/oversold levels and momentum shifts.
*   **ROC (Rate of Change):** Validates the speed of the current trend.
*   **CCI (Commodity Channel Index):** Detects cyclical trends and extreme momentum.
*   **TSI (True Strength Index):** Double-smoothed momentum indicator.
*   **Ultimate Oscillator:** Combined momentum across three timeframes.
*   **PPO (Percentage Price Oscillator):** Percentage-based MACD.

### C) Volatility Block (Weight: 1)
Analyzes market expansion and contraction.
*   **ATR (Average True Range):** Used to gauge volatility expansion vs. contraction.
*   **Bollinger Bands:** Analyzes band width expansion in the direction of the trend.
*   **Donchian Channels:** Checks if price is trading in the upper or lower half of the recent range.
*   **Keltner Channels:** Price positioning relative to the ATR-based midline.
*   **Ulcer Index:** Measures downside risk/volatility.
*   **Mass Index:** Identifies trend reversals by measuring range expansion.

### D) Structure / Mean Reversion Block (Weight: 2)
Analyzes key levels, proximity to major averages, and potential reversals.
*   **Price Distance from EMA:** Penalizes setups that are overextended (too far from EMA 50).
*   **Z-Score:** Statistically measures price deviation from the 20-period mean.
*   **BB Band Touches:** Identifies potential resistance/support rejections.
*   **Pivot Points (Standard):** Uses Daily High/Low/Close to identify R1/S1 levels for bounces or breakouts.
*   **Daily High/Low Proximity:** Monitors if price is nearing extreme daily boundaries.
*   **HTF SNR Alignment:** Matches price against identified Support and Resistance zones from the 100-candle history.

---

## 3. Scoring & Execution Logic

### Normalized Confidence
The final score is normalized into a **Confidence Percentage** ranging from **-100% (Strong Sell)** to **+100% (Strong Buy)**.
*   **Trade Trigger:** Requires absolute confidence of **>= 60%**.
*   **MTF Alignment:** In addition to the score, the 15m bias and 1m price action must align with the signal.

### Rise & Fall (Scalping)
*   **Expiry Duration:** Dynamically adjusted based on confidence.
    *   70%+ Confidence: 15 Minute Expiry
    *   55%+ Confidence: 10 Minute Expiry
    *   40%+ Confidence: 5 Minute Expiry

### Multiplier (Position Trading)
*   **Automated Multiplier Selection:**
    *   80%+ Confidence: 50x Multiplier
    *   65%+ Confidence: 20x Multiplier
    *   50%+ Confidence: 5x Multiplier
*   **Target Levels (TP/SL):** Based on 1H ATR and a Risk Factor (0.5 to 1.5) determined by confidence.
    *   Take Profit is set at 1.5x the Stop Loss (1:1.5 Risk/Reward).

---

## 4. Dynamic Position Management (The "Decision Engine")

Once a trade is open, the Intelligence Engine monitors it on every tick:

1.  **Intelligence Decay Exit:** If the confidence score drops by **25%** relative to the entry score, the trade is closed immediately.
2.  **Trend Flip Exit:** If the Trend Score flips sign (indicating a crossover or structural shift), the position is exited.
3.  **Momentum Reversal:** If the Momentum Score heavily contradicts the entry (e.g., strong bearish momentum in a long trade), the bot triggers a hard exit.
4.  **ATR-Based Trailing (Multiplier Only):**
    *   **Breakeven:** Moves SL to entry price once profit reachs **1 ATR**.
    *   **Trailing:** Trails the position using the **EMA 20** once profit exceeds **2 ATR**.
