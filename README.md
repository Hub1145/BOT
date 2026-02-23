# Deriv Trading Bot Dashboard (Multi-Strategy v4.0)

A high-performance, multi-strategy trading bot designed for Deriv Volatility Indices. This bot features a real-time web dashboard for monitoring statistics, logs, and active positions with a focus on precision execution and algorithmic market context.

## 🚀 Key Features

### 🎯 Intelligent Dashboard
- **Amount Tab**: Comprehensive real-time statistics including Balance, PNL, Total Trades, Win Rate, and Average Trade PNL.
- **Position Tab**: Live monitoring of all open contracts with real-time PNL tracking, entry spot prices, and automated expiry countdowns.
- **Log Tab**: Real-time console output streaming system events, signal generation, and trade executions.
- **Dynamic Screener**: Real-time technical analysis for advanced strategies (5, 6, and 7) with adaptive columns based on the active strategy.

### ⚙️ Professional Bot Controls
- **Start/Stop**: One-click control for bot execution. The bot continues to monitor and close existing positions even when trading is paused.
- **Multi-Symbol Management**: Add and trade multiple symbols simultaneously. The bot handles concurrent analysis for all symbols without delays.
- **Risk Management**: Toggle between fixed USD or percentage-based balance usage. Configure Max Daily Loss %, Take Profit, Stop Loss, and Force Close durations.

---

## 📊 Trading Strategies

The bot supports seven distinct trading strategies, ranging from simple breakouts to complex intelligent screeners.

### 🔹 Strategy 1: Slow Breakout (v4.0 Enhanced)
*   **Timeframes**: Daily (HTF) / 15-Minute (LTF).
*   **Macro Filter**: Only takes breakouts aligning with the 4H 100 EMA trend.
*   **Whipsaw Protection**: Automatically disables for the day if the Daily Open is crossed more than 3 times (ranging market).
*   **Logic**: Triggers on the 15m candle where a breakout across the Daily Open occurs.
*   **Dynamic Exit**: Exits at +2 Daily ATR target or if a 15m candle closes back across the Daily Open.

### 🔹 Strategy 2: Moderate (v4.0 Enhanced)
*   **Timeframes**: 1-Hour (HTF) / 3-Minute (LTF).
*   **Momentum Qualifier**: Requires 3m RSI(14) > 55 (Buy) or < 45 (Sell) to filter weak breakouts.
*   **HTF Bias Gate**: Entries allowed only if 4H EMA 21 > 50 (Bullish) or 21 < 50 (Bearish).
*   **Logic**: Breakout crossover logic applied to 1-hour and 3-minute intervals.
*   **Distance-Based Expiry**: Reduces expiry to 30m if the signal fires when price is already >1 ATR from the 1H open (exhaustion guard).

### 🔹 Strategy 3: Fast (v4.0 Enhanced)
*   **Timeframes**: 15-Minute (HTF) / 1-Minute (LTF).
*   **Candle Sequence Filter**: Requires 2 consecutive 1m closes beyond the 15m open to confirm momentum.
*   **Volatility Regime**: Skips trades if 1m ATR is in the bottom 20th percentile (low-volatility breakout failure guard).
*   **Overtrading Protection**: Hard cap of 4 entries per symbol per hour.
*   **Dynamic Expiry**: Sets expiry to remaining 15m candle time + 2 minutes for better temporal alignment.

### 🔹 Strategy 4: SNR Price Action (v4.0 Enhanced)
*   **Logic**: Pure Price Action strategy based on Support, Resistance, and Flip zones.
*   **Zone Freshness**: Tracks touch counts; reduces position size after 3 touches and retires zones after 5.
*   **Pattern Scoring**: Rates 1m reversal patterns (Pin Bars, Engulfing) based on wick ratio (>2:1) and close position.
*   **Momentum Exhaustion**: Uses 5m RSI filter to avoid fading aggressive moves (e.g., skips bearish pins if 5m RSI > 80).
*   **Hard Invalidation**: Immediately marks zones as broken if a 1m candle closes through the level.

### 🔹 Strategy 5: Synthetic Intelligence (v4.0)
Advanced engine with market regime switching and tiered structural mapping.
*   **Market Regime Switch**: Automatically toggles weights based on ADX:
    *   **Trending (ADX > 25)**: 80% Weight to Trend/Volatility; disables oscillators; buys pullbacks.
    *   **Ranging (ADX < 20)**: 80% Weight to Momentum/Structure; fades extremes.
*   **Architecture**:
    *   **Tiered Structure**: Uses overlapping Order Blocks and Fair Value Gaps (FVG) for high-conviction entries.
    *   **Execution**: Rise & Fall (Scalp) requires Stoch RSI extremes at Fractal touches. Multipliers cap at 10x during "Dead Hours" (22:00-06:00 UTC).
*   **Adaptive Reset**: Requires 2 consecutive wins or 1 win + ADX > 20 to return to baseline safety thresholds.

### 🔹 Strategy 6: Intelligence Legacy (v4.0)
The exhaustive indicator suite refactored for dimensionality and smoothing.
*   **Dimensionality Reduction**: Groups 20+ indicators and requires cross-category agreement (Trend + Momentum) to prevent multicollinearity lag.
*   **Timeframe Smoothing**: Bridges intervals linearly: 1m (Entry) -> 15m (Trend) -> 1H (Macro Bias).

### 🔹 Strategy 7: Pullback Alignment (v4.0)
High-precision model that enters main trends at the end of micro-pullbacks.
*   **Pullback Model**: 1H (Macro) and 15m (Intraday) must be aligned, while 1m must show a temporary pullback/oversold state.
*   **Execution**: Triggers the moment the 1m Small TF flips back to align with the HTF/Mid trend.
*   **ADR Guard**: Prevents entries if the asset has already moved its Average Daily Range (ADR), avoiding "buying the top."

---

## 🛠 Advanced Position Management

- **One Trade Per Symbol**: The bot ensures only one active position exists per symbol.
- **Opposite Cancellation**: Receiving a new signal in the opposite direction automatically closes the existing trade before entering the new one.
- **Free Ride Protocol**: Moves SL to a structural safety zone (recent 1m Fractal or ATR buffer) once profit reaches 1.5 ATR, protecting against liquidity grab wicks.
- **Dynamic Trailing**: Uses SuperTrend (15m) to trail profits once in "Free Ride" mode.
- **MACD Divergence Exit**: Immediate hard exit if a macro-timeframe MACD divergence prints against the position.
- **Ghost Cleanup**: Automatically purges expired contracts from internal state if API updates are missed.

---

## ⚙️ Setup & Deployment

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **Run the Application**:
    ```bash
    python app.py
    ```
3.  **Access Dashboard**: Open `http://localhost:3000` (or your configured PORT).
4.  **Configure API**: Click "Config" and enter your **Deriv API Token** and **App ID**.

---

## ⚠️ Important Notes

- **Demo First**: Always test strategies with a Deriv Demo account (VRTC) before going live.
- **UTC Time**: Strategy 1 and breakout logic use UTC time for Daily candle calculations.
- **Rate Limits**: The bot includes built-in gaps and throttles to respect Deriv API rate limits while maintaining concurrent symbol analysis.

---

## 🛡 License & Disclaimer

This software is for educational purposes. Trading financial instruments involves significant risk of loss. The authors are not responsible for any financial losses incurred.
