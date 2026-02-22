# Strategy 6: Intelligence Legacy (v1.0)

Strategy 6 is a comprehensive technical analysis engine based on the original v1.0 Intelligence strategy. It uses an exhaustive suite of indicators across four analytical blocks with weighted scoring to determine market sentiment.

## 1. Indicator Suite

### A) Trend Block (Weight: 3)
*   **EMA 50 & 200:** Moving average alignment and price positioning.
*   **SMA 20:** Short-term trend filter.
*   **ADX (Average Directional Index):** Trend strength filter (> 25).
*   **Ichimoku Cloud:** Price relation to Span A and Span B.
*   **MACD:** Signal line crossover.

### B) Momentum Block (Weight: 2)
*   **RSI:** Relative Strength Index (50 threshold).
*   **Stoch RSI:** Stochastic RSI (0.5 threshold).
*   **Williams %R:** Momentum velocity (-50 threshold).
*   **ROC (Rate of Change):** Speed of price movement (0 threshold).
*   **CCI (Commodity Channel Index):** Cyclical trend detector (0 threshold).

### C) Volatility Block (Weight: 1)
*   **ATR:** Average True Range expansion vs. previous period.
*   **Bollinger Bands:** Width expansion check.
*   **Donchian Channel:** Price relation to the 20-period midline.
*   **Keltner Channel:** Price relation to the ATR-based midline.

### D) Structure / Mean Reversion Block (Weight: 2)
*   **Price Distance from EMA:** Measures overextension.
*   **Z-Score:** Statistical deviation from the 20-period mean.
*   **BB Band Touch:** Rejection from outer bands.
*   **Pivot Points:** Support and Resistance levels (Pivot, R1, S1).
*   **Daily High/Low Proximity:** Monitors proximity to daily boundaries.
*   **HTF SNR Alignment:** Alignment with identified 1-Hour Support and Resistance zones.

---

## 2. Scoring & Execution

The bot calculates a normalized **Confidence Score** from -100% to +100% based on the cumulative weighted scores of all indicators.

*   **Confidence Threshold:** >= 60% for trade execution.
*   **Modes:** Supports both **Rise & Fall** and **Multiplier** contracts.
*   **Timeframes:**
    *   **Core Analysis:** 1-Hour (HTF).
    *   **Timing/Execution:** 1-Minute (LTF).
    *   **Bias Filter:** 4-Hour (Bias).
*   **Exit Logic:** Primarily managed via Take Profit and Stop Loss (calculated based on ATR).
