# Deriv Trading Bot Dashboard (Multi-Strategy)

A high-performance, multi-strategy trading bot designed for Deriv Volatility Indices. This bot features a real-time web dashboard for monitoring statistics, logs, and active positions with a focus on precision execution and advanced technical analysis.

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

### 🔹 Strategy 1: Slow Breakout (Daily / 15m)
*   **Timeframes**: Daily (HTF) / 15-Minute (LTF).
*   **Logic**: Triggers on the 15m candle where a breakout across the Daily Open occurs.
    *   **Buy**: 15m candle open <= Daily Open AND 15m candle close > Daily Open.
    *   **Sell**: 15m candle open >= Daily Open AND 15m candle close < Daily Open.
*   **Expiry**: Hardcoded to expire at the close of the current Daily candle (EOD).

### 🔹 Strategy 2: Moderate (1h / 3m)
*   **Timeframes**: 1-Hour (HTF) / 3-Minute (LTF).
*   **Logic**: Breakout crossover logic applied to 1-hour and 3-minute intervals.
*   **Expiry**: Fixed duration (default 1 hour) or time until next 1H candle close.

### 🔹 Strategy 3: Fast (15m / 1m)
*   **Timeframes**: 15-Minute (HTF) / 1-Minute (LTF).
*   **Logic**: Scalping breakout strategy for rapid volatility expansion.
*   **Expiry**: Fixed duration (default 15 minutes).

### 🔹 Strategy 4: SNR Price Action
*   **Logic**: Pure Price Action strategy based on Support, Resistance, and Flip zones.
*   **Analysis**: Identifies high-conviction zones on 5m/1h timeframes and waits for 1m reversal patterns (Pin Bar, Engulfing, Tweezer) at those zones.

### 🔹 Strategy 5: Synthetic Intelligence Screener (v2.1)
An advanced engine optimized for Volatility Indices using weighted indicator blocks.
*   **Architecture**:
    *   **Trend Block**: EMA 50/200, SuperTrend, ADX.
    *   **Momentum Block**: RSI, Stoch RSI, MACD Divergence.
    *   **Volatility Block**: ATR, Bollinger Bands.
    *   **Structure Block**: 5m Fractals (Scalp) or 1H Order Blocks (Multiplier).
*   **Execution Modes**: Supports **Rise & Fall** (Scalping) with >=72% confidence and **Multiplier** (Day Trading) with >=68% confidence.
*   **Adaptive Sensitivity**: Automatically increases confidence thresholds following 3+ consecutive losses on a symbol.

### 🔹 Strategy 6: Intelligence Legacy (v1.0)
The exhaustive indicator suite from v1.0, featuring over 20 technical indicators.
*   **Indicator Blocks**: Trend (Weight 3), Momentum (Weight 2), Volatility (Weight 1), Structure (Weight 2).
*   **Execution**: Normalized confidence score >= 60% across Core (1H), Timing (1m), and Bias (4H) timeframes.

### 🔹 Strategy 7: Intelligent Multi-TF Alignment
Seek high-conviction entries by aligning signals across three custom timeframes.
*   **Logic**: Triggers only when the Small, Mid, and High timeframes all report a consistent BUY or SELL recommendation.
*   **Customization**: Users select any three timeframes (e.g., 1m, 5m, 1h) from the dashboard.
*   **Intelligence**: Integrates with the bot's autonomous decision engine for ATR-based TP/SL and trailing stops.

---

## 🛠 Advanced Position Management

- **One Trade Per Symbol**: The bot ensures only one active position exists per symbol.
- **Opposite Cancellation**: Receiving a new signal in the opposite direction automatically closes the existing trade before entering the new one.
- **Free Ride Protocol**: In intelligent strategies, moves SL to entry + margin once profit reaches 1.5 ATR.
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
