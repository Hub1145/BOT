# Strategy 7: Intelligent Multi-TF Alignment

Strategy 7 is a sophisticated multi-timeframe analysis engine that identifies high-probability trading opportunities by seeking alignment across three user-defined timeframes: **Small**, **Mid**, and **High**.

## 1. Core Logic

The strategy utilizes the `DerivTA` engine to perform a comprehensive technical analysis on each selected timeframe. To ensure maximum performance and responsiveness, analysis is performed in a **dedicated background thread**, avoiding any lag in the main tick-processing loop.

### Signal Alignment
*   **Buy Signal:** Triggered when all three timeframes (Small, Mid, and High) report a "BUY" or "STRONG_BUY" recommendation.
*   **Sell Signal:** Triggered when all three timeframes report a "SELL" or "STRONG_SELL" recommendation.
*   **Neutral State:** If any timeframe reports "NEUTRAL" or if directions conflict, no trade is executed.

### Confidence Labels
Strategy 7 reports explicit sentiment labels in the Screener:
*   **STRONG_BUY / STRONG_SELL:** Perfect alignment with high-conviction oscillators and MAs on the core timeframe.
*   **BUY / SELL:** General directional alignment across indicators.
*   **NEUTRAL:** Indecision or conflicting signals.

### Extreme Market Conditions
*   When all three timeframes report **STRONG_BUY** or **STRONG_SELL**, the intelligence engine identifies an aggressive trend.
*   The bot continues to trade in the direction of the strong signal but monitors for exhaustion patterns via the built-in dynamic position management.

---

## 2. Multi-Timeframe Architecture

Users can customize the hierarchy to suit their trading style:
*   **Small TF:** Typically 1m to 5m. Used for local momentum and entry precision.
*   **Mid TF:** Typically 5m to 1h. Represents the intraday trend.
*   **High TF:** Typically 1h to 1d. Provides the macro bias and structural context.

---

## 3. Intelligence-Driven Management

Strategy 7 integrates with the bot's **Decision Engine v2.0** for autonomous risk control:

*   **ATR-Based TP/SL:** Uses the Average True Range of the **Mid TF** to set targets that respect current market volatility.
*   **Free Ride Protocol:** Moves Stop Loss to entry price plus a small margin once the position reaches **1.5 ATR** in profit.
*   **SuperTrend Trailing:** In "Free Ride" mode, the position is trailed based on the local trend to maximize profits during extended runs.
*   **Hard Exit:** The bot will exit the trade if a significant trend reversal or MACD divergence is detected on the Core timeframe.

---

## 4. Configuration

To use Strategy 7:
1.  Select **Intelligent Multi-TF Alignment** from the Trading Strategy dropdown.
2.  Choose your desired **Small**, **Mid**, and **High** timeframes.
3.  Configure your **Contract Type** (Rise & Fall or Multiplier).
4.  Ensure your **Trade Amount** and **Risk Management** settings are correctly set in the dashboard.
