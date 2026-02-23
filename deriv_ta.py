
import asyncio
import json
import time
import numpy as np
import pandas as pd
import websockets
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────

DERIV_WS_URL = "wss://ws.binaryws.com/websockets/v3?app_id=1089"

# All available Deriv synthetic volatility symbols
class Symbol:
    # Volatility Indices
    V10  = "R_10"
    V25  = "R_25"
    V50  = "R_50"
    V75  = "R_75"
    V100 = "R_100"

    # Volatility (1s) Indices
    V10_1S  = "1HZ10V"
    V25_1S  = "1HZ25V"
    V50_1S  = "1HZ50V"
    V75_1S  = "1HZ75V"
    V100_1S = "1HZ100V"

    # Boom & Crash
    BOOM_300  = "BOOM300N"
    BOOM_500  = "BOOM500"
    BOOM_1000 = "BOOM1000"
    CRASH_300  = "CRASH300N"
    CRASH_500  = "CRASH500"
    CRASH_1000 = "CRASH1000"

    # Step Indices
    STEP_100 = "STPRNG"

    # Jump Indices
    JUMP_10  = "JD10"
    JUMP_25  = "JD25"
    JUMP_50  = "JD50"
    JUMP_75  = "JD75"
    JUMP_100 = "JD100"


class Interval(Enum):
    """Intervals in seconds (Deriv candle granularity)."""
    INTERVAL_1_MINUTE   = 60
    INTERVAL_2_MINUTES  = 120
    INTERVAL_3_MINUTES  = 180
    INTERVAL_5_MINUTES  = 300
    INTERVAL_10_MINUTES = 600
    INTERVAL_15_MINUTES = 900
    INTERVAL_30_MINUTES = 1800
    INTERVAL_1_HOUR     = 3600
    INTERVAL_2_HOURS    = 7200
    INTERVAL_4_HOURS    = 14400
    INTERVAL_8_HOURS    = 28800
    INTERVAL_1_DAY      = 86400


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class Analysis:
    """Mirrors tradingview_ta Analysis object."""
    symbol: str
    interval: str
    summary: dict = field(default_factory=dict)
    moving_averages: dict = field(default_factory=dict)
    oscillators: dict = field(default_factory=dict)
    indicators: dict = field(default_factory=dict)  # raw indicator values

    def __repr__(self):
        return (
            f"Analysis(symbol={self.symbol}, interval={self.interval}, "
            f"recommendation={self.summary.get('RECOMMENDATION', 'N/A')})"
        )


# ─────────────────────────────────────────────
#  INDICATOR COMPUTATIONS
# ─────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def _stoch(high: pd.Series, low: pd.Series, close: pd.Series,
           k_period: int = 14, d_period: int = 3, smooth_k: int = 3):
    lowest_low   = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    raw_k = 100 * (close - lowest_low) / (highest_high - lowest_low + 1e-10)
    k = raw_k.rolling(smooth_k).mean()
    d = k.rolling(d_period).mean()
    return k, d

def _cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    tp = (high + low + close) / 3
    ma = tp.rolling(period).mean()
    md = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - ma) / (0.015 * md)

def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    plus_dm  = np.where((high - prev_high) > (prev_low - low), np.maximum(high - prev_high, 0), 0)
    minus_dm = np.where((prev_low - low) > (high - prev_high), np.maximum(prev_low - low, 0), 0)

    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    tr  = pd.Series(np.maximum(tr1, np.maximum(tr2, tr3)), index=close.index)

    atr      = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * pd.Series(plus_dm, index=close.index).ewm(span=period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=close.index).ewm(span=period, adjust=False).mean() / atr
    dx       = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx      = dx.ewm(span=period, adjust=False).mean()
    return adx, plus_di, minus_di

def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line   = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram

def _ao(high: pd.Series, low: pd.Series) -> pd.Series:
    midpoint = (high + low) / 2
    return _sma(midpoint, 5) - _sma(midpoint, 34)

def _momentum(close: pd.Series, period: int = 10) -> pd.Series:
    return close - close.shift(period)

def _williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    highest_high = high.rolling(period).max()
    lowest_low   = low.rolling(period).min()
    return -100 * (highest_high - close) / (highest_high - lowest_low + 1e-10)

def _stoch_rsi(close: pd.Series, rsi_period: int = 14, stoch_period: int = 14,
               k_period: int = 3, d_period: int = 3):
    rsi = _rsi(close, rsi_period)
    lowest  = rsi.rolling(stoch_period).min()
    highest = rsi.rolling(stoch_period).max()
    stoch   = 100 * (rsi - lowest) / (highest - lowest + 1e-10)
    k = stoch.rolling(k_period).mean()
    d = k.rolling(d_period).mean()
    return k, d

def _bull_bear_power(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 13):
    ema13    = _ema(close, period)
    bull_pwr = high - ema13
    bear_pwr = low - ema13
    return bull_pwr, bear_pwr

def _ultimate_oscillator(high: pd.Series, low: pd.Series, close: pd.Series,
                          p1: int = 7, p2: int = 14, p3: int = 28) -> pd.Series:
    prev_close = close.shift(1)
    bp  = close - np.minimum(low, prev_close)
    tr  = np.maximum(high, prev_close) - np.minimum(low, prev_close)

    avg1 = bp.rolling(p1).sum() / tr.rolling(p1).sum()
    avg2 = bp.rolling(p2).sum() / tr.rolling(p2).sum()
    avg3 = bp.rolling(p3).sum() / tr.rolling(p3).sum()
    return 100 * (4 * avg1 + 2 * avg2 + avg3) / 7

def _hull_ma(close: pd.Series, period: int = 9) -> pd.Series:
    half   = max(int(period / 2), 1)
    sqrtn  = max(int(np.sqrt(period)), 1)
    wma1   = close.rolling(half).mean()
    wma2   = close.rolling(period).mean()
    hull   = (2 * wma1 - wma2).rolling(sqrtn).mean()
    return hull

def _ichimoku_base(high: pd.Series, low: pd.Series, period: int = 26) -> pd.Series:
    return (high.rolling(period).max() + low.rolling(period).min()) / 2

def _vwma(close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    if volume is None or volume.sum() == 0:
        return _sma(close, period)   # fallback to SMA if no volume
    return (close * volume).rolling(period).sum() / volume.rolling(period).sum()


# ─────────────────────────────────────────────
#  VOTING LOGIC
# ─────────────────────────────────────────────

def _vote(buy_cond: bool, sell_cond: bool) -> str:
    if buy_cond:
        return "BUY"
    elif sell_cond:
        return "SELL"
    return "NEUTRAL"

def _score_to_recommendation(score: float) -> str:
    if score >= 0.5:
        return "STRONG_BUY"
    elif score >= 0.1:
        return "BUY"
    elif score <= -0.5:
        return "STRONG_SELL"
    elif score <= -0.1:
        return "SELL"
    return "NEUTRAL"

def _tally(signals: list) -> dict:
    buy    = signals.count("BUY")
    sell   = signals.count("SELL")
    neutral = signals.count("NEUTRAL")
    numeric = [1 if s == "BUY" else -1 if s == "SELL" else 0 for s in signals]
    score   = sum(numeric) / len(numeric) if numeric else 0
    return {
        "RECOMMENDATION": _score_to_recommendation(score),
        "BUY":    buy,
        "SELL":   sell,
        "NEUTRAL": neutral,
    }


# ─────────────────────────────────────────────
#  CORE SCREENER
# ─────────────────────────────────────────────

def _compute_analysis(df: pd.DataFrame, symbol: str, interval_name: str) -> Analysis:
    """Run all 26 TradingView indicators on the OHLCV DataFrame."""

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df.get("volume", pd.Series(np.zeros(len(df)), index=df.index))

    price = close.iloc[-1]

    # ── Raw indicator values ───────────────────────────────────
    rsi_val   = _rsi(close).iloc[-1]
    stoch_k, stoch_d = _stoch(high, low, close)
    stoch_k_val = stoch_k.iloc[-1]
    cci_val   = _cci(high, low, close).iloc[-1]
    adx_val, plus_di, minus_di = _adx(high, low, close)
    adx_val    = adx_val.iloc[-1]
    plus_di_v  = plus_di.iloc[-1]
    minus_di_v = minus_di.iloc[-1]
    ao         = _ao(high, low)
    ao_val     = ao.iloc[-1]
    ao_prev    = ao.iloc[-2] if len(ao) > 1 else 0
    mom_val    = _momentum(close).iloc[-1]
    macd_line, macd_signal, _ = _macd(close)
    macd_val   = macd_line.iloc[-1]
    macd_sig   = macd_signal.iloc[-1]
    stoch_rsi_k, _ = _stoch_rsi(close)
    srsi_k     = stoch_rsi_k.iloc[-1]
    wr_val     = _williams_r(high, low, close).iloc[-1]
    bull_pwr, bear_pwr = _bull_bear_power(high, low, close)
    bull_val   = bull_pwr.iloc[-1]
    bear_val   = bear_pwr.iloc[-1]
    uo_val     = _ultimate_oscillator(high, low, close).iloc[-1]

    # MAs
    ema10  = _ema(close, 10).iloc[-1]
    sma10  = _sma(close, 10).iloc[-1]
    ema20  = _ema(close, 20).iloc[-1]
    sma20  = _sma(close, 20).iloc[-1]
    ema30  = _ema(close, 30).iloc[-1]
    sma30  = _sma(close, 30).iloc[-1]
    ema50  = _ema(close, 50).iloc[-1]
    sma50  = _sma(close, 50).iloc[-1]
    ema100 = _ema(close, 100).iloc[-1]
    sma100 = _sma(close, 100).iloc[-1]
    ema200 = _ema(close, 200).iloc[-1]
    sma200 = _sma(close, 200).iloc[-1]
    ichimoku = _ichimoku_base(high, low).iloc[-1]
    vwma_val = _vwma(close, volume).iloc[-1]
    hull_val = _hull_ma(close).iloc[-1]

    # ── Oscillator signals (11) ────────────────────────────────
    osc_signals = {
        "RSI(14)":          _vote(rsi_val < 30, rsi_val > 70),
        "Stoch %K(14,3,3)": _vote(stoch_k_val < 20, stoch_k_val > 80),
        "CCI(20)":          _vote(cci_val < -100, cci_val > 100),
        "ADX(14)":          _vote(adx_val > 20 and plus_di_v > minus_di_v,
                                   adx_val > 20 and minus_di_v > plus_di_v),
        "AO":               _vote(ao_val > 0 and ao_val > ao_prev,
                                   ao_val < 0 and ao_val < ao_prev),
        "Momentum(10)":     _vote(mom_val > 0, mom_val < 0),
        "MACD(12,26,9)":    _vote(macd_val > macd_sig, macd_val < macd_sig),
        "StochRSI Fast(3,3,14,14)": _vote(srsi_k < 20, srsi_k > 80),
        "Williams %R(14)":  _vote(wr_val < -80, wr_val > -20),
        "Bull/Bear Power(13)": _vote(bull_val > 0 and bear_val > 0,
                                      bull_val < 0 and bear_val < 0),
        "UO(7,14,28)":      _vote(uo_val < 30, uo_val > 70),
    }

    # ── Moving Average signals (15) ────────────────────────────
    ma_signals = {
        "EMA10":        _vote(price > ema10,    price < ema10),
        "SMA10":        _vote(price > sma10,    price < sma10),
        "EMA20":        _vote(price > ema20,    price < ema20),
        "SMA20":        _vote(price > sma20,    price < sma20),
        "EMA30":        _vote(price > ema30,    price < ema30),
        "SMA30":        _vote(price > sma30,    price < sma30),
        "EMA50":        _vote(price > ema50,    price < ema50),
        "SMA50":        _vote(price > sma50,    price < sma50),
        "EMA100":       _vote(price > ema100,   price < ema100),
        "SMA100":       _vote(price > sma100,   price < sma100),
        "EMA200":       _vote(price > ema200,   price < ema200),
        "SMA200":       _vote(price > sma200,   price < sma200),
        "Ichimoku B/L": _vote(price > ichimoku, price < ichimoku),
        "VWMA(20)":     _vote(price > vwma_val, price < vwma_val),
        "HullMA(9)":    _vote(price > hull_val, price < hull_val),
    }

    all_signals = list(osc_signals.values()) + list(ma_signals.values())

    oscillators = {
        "RECOMMENDATION": _tally(list(osc_signals.values()))["RECOMMENDATION"],
        **_tally(list(osc_signals.values())),
        "COMPUTE": osc_signals,
    }

    moving_averages = {
        "RECOMMENDATION": _tally(list(ma_signals.values()))["RECOMMENDATION"],
        **_tally(list(ma_signals.values())),
        "COMPUTE": ma_signals,
    }

    summary = _tally(all_signals)

    # raw values for reference
    indicators = {
        "RSI":           round(rsi_val, 4),
        "Stoch.K":       round(stoch_k_val, 4),
        "Stoch.D":       round(stoch_d.iloc[-1], 4),
        "CCI":           round(cci_val, 4),
        "ADX":           round(adx_val, 4),
        "ADX+DI":        round(plus_di_v, 4),
        "ADX-DI":        round(minus_di_v, 4),
        "AO":            round(ao_val, 4),
        "Momentum":      round(mom_val, 4),
        "MACD.macd":     round(macd_val, 4),
        "MACD.signal":   round(macd_sig, 4),
        "StochRSI.K":    round(srsi_k, 4),
        "W.%R":          round(wr_val, 4),
        "BBPower":       round(bull_val + bear_val, 4),
        "UO":            round(uo_val, 4),
        "EMA10":  round(ema10, 4),  "SMA10":  round(sma10, 4),
        "EMA20":  round(ema20, 4),  "SMA20":  round(sma20, 4),
        "EMA30":  round(ema30, 4),  "SMA30":  round(sma30, 4),
        "EMA50":  round(ema50, 4),  "SMA50":  round(sma50, 4),
        "EMA100": round(ema100, 4), "SMA100": round(sma100, 4),
        "EMA200": round(ema200, 4), "SMA200": round(sma200, 4),
        "Ichimoku.BLine": round(ichimoku, 4),
        "VWMA":   round(vwma_val, 4),
        "HullMA": round(hull_val, 4),
        "close":  round(price, 4),
    }

    return Analysis(
        symbol=symbol,
        interval=interval_name,
        summary=summary,
        moving_averages=moving_averages,
        oscillators=oscillators,
        indicators=indicators,
    )


# ─────────────────────────────────────────────
#  DERIV DATA FETCHER
# ─────────────────────────────────────────────

async def _fetch_candles(symbol: str, granularity: int, count: int = 300) -> pd.DataFrame:
    """Fetch OHLC candles from Deriv WebSocket API."""
    end_time   = int(time.time())
    start_time = end_time - granularity * count

    request = {
        "ticks_history": symbol,
        "style":         "candles",
        "granularity":   granularity,
        "start":         start_time,
        "end":           end_time,
        "count":         count,
    }

    async with websockets.connect(DERIV_WS_URL) as ws:
        await ws.send(json.dumps(request))
        response = json.loads(await ws.recv())

    if "error" in response:
        raise ValueError(f"Deriv API error: {response['error']['message']}")

    candles = response.get("candles", [])
    if not candles:
        raise ValueError(f"No candle data returned for symbol: {symbol}")

    df = pd.DataFrame(candles)
    # Ensure epoch is preserved as column if needed, or use index
    df["epoch_dt"] = pd.to_datetime(df["epoch"], unit="s")
    df.set_index("epoch_dt", inplace=True)
    df = df[["open", "high", "low", "close", "epoch"]].astype(float)
    return df


# ─────────────────────────────────────────────
#  MAIN HANDLER  (mirrors TA_Handler API)
# ─────────────────────────────────────────────

class DerivTA:
    """
    Usage (mirrors tradingview_ta):

        handler  = DerivTA(symbol=Symbol.V75, interval=Interval.INTERVAL_5_MINUTES)
        analysis = handler.get_analysis()

        print(analysis.summary)
        print(analysis.moving_averages)
        print(analysis.oscillators)
    """

    def __init__(
        self,
        symbol: str = Symbol.V75,
        interval: Interval = Interval.INTERVAL_5_MINUTES,
        candle_count: int = 300,
    ):
        self.symbol       = symbol
        self.interval     = interval
        self.candle_count = candle_count
        self._df: Optional[pd.DataFrame] = None

    # ── Public API ─────────────────────────────────────────────

    def get_analysis(self) -> Analysis:
        """Fetch data and return a full Analysis object."""
        self._df = asyncio.run(
            _fetch_candles(self.symbol, self.interval.value, self.candle_count)
        )
        return _compute_analysis(self._df, self.symbol, self.interval.name)

    def get_dataframe(self) -> pd.DataFrame:
        """Return the raw OHLC DataFrame (call get_analysis first)."""
        if self._df is None:
            raise RuntimeError("Call get_analysis() first.")
        return self._df

    @staticmethod
    def list_symbols() -> dict:
        return {
            "Volatility Indices": {
                "V10": Symbol.V10, "V25": Symbol.V25, "V50": Symbol.V50,
                "V75": Symbol.V75, "V100": Symbol.V100,
            },
            "Volatility 1s Indices": {
                "V10(1s)": Symbol.V10_1S, "V25(1s)": Symbol.V25_1S,
                "V50(1s)": Symbol.V50_1S, "V75(1s)": Symbol.V75_1S,
                "V100(1s)": Symbol.V100_1S,
            },
            "Boom & Crash": {
                "Boom 300": Symbol.BOOM_300, "Boom 500": Symbol.BOOM_500,
                "Boom 1000": Symbol.BOOM_1000, "Crash 300": Symbol.CRASH_300,
                "Crash 500": Symbol.CRASH_500, "Crash 1000": Symbol.CRASH_1000,
            },
            "Jump Indices": {
                "Jump 10": Symbol.JUMP_10, "Jump 25": Symbol.JUMP_25,
                "Jump 50": Symbol.JUMP_50, "Jump 75": Symbol.JUMP_75,
                "Jump 100": Symbol.JUMP_100,
            },
        }
