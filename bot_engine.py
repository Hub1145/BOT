import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from collections import deque
import websocket
import pandas as pd
import ta

class TradingBotEngine:
    STRATEGY_MAP = {
        'strategy_1': {
            'name': 'Slow (Daily / 15m)',
            'htf_granularity': 86400, # Daily
            'ltf_granularity': 900,   # 15m
            'expiry_type': 'eod'      # End of Day
        },
        'strategy_2': {
            'name': 'Moderate',
            'htf_granularity': 3600,  # 1h
            'ltf_granularity': 180,   # 3m
            'expiry_type': 'fixed',
            'duration': 3600          # 1 hour
        },
        'strategy_3': {
            'name': 'Fast',
            'htf_granularity': 900,   # 15m
            'ltf_granularity': 60,    # 1m
            'expiry_type': 'fixed',
            'duration': 900           # 15 minutes
        },
        'strategy_4': {
            'name': 'SNR Price Action',
            'htf_granularity': 300,   # 5m for SNR
            'ltf_granularity': 60,    # 1m for Entry
            'expiry_type': 'fixed',
            'duration': 300           # 5m expiry
        },
        'strategy_5': {
            'name': 'Intelligence Screener',
            'htf_granularity': 3600,  # 1h for Intelligence Core
            'ltf_granularity': 60,    # 1m for Timing
            'bias_granularity': 14400, # 4h for intermediate regime
            'expiry_type': 'dynamic'
        }
    }

    def __init__(self, config_path, emit_callback):
        self.config_path = config_path
        self.emit = emit_callback
        self.console_logs = deque(maxlen=500)
        self.screener_data = {} # Symbol -> Screener metrics
        self.config = self._load_config()

        self.is_running = False
        self.is_connected = False
        self.ws = None
        self.ws_thread = None
        self.stop_event = threading.Event()

        # Account metrics
        self.account_balance = 0.0
        self.available_balance = 0.0
        self.total_equity = 0.0
        self.net_profit = 0.0
        self.total_trades_count = 0
        self.trade_fees = 0.0
        self.used_fees = 0.0
        self.size_fees = 0.0
        self.cached_pos_notional = 0.0
        self.used_amount_notional = 0.0
        self.remaining_amount_notional = 0.0
        self.max_allowed_display = 0.0
        self.max_amount_display = 0.0
        self.total_capital_2nd = 0.0
        self.net_trade_profit = 0.0
        self.total_trade_profit = 0.0
        self.total_trade_loss = 0.0
        self.wins_count = 0
        self.losses_count = 0
        self.daily_start_balance = 0.0
        self.last_balance_reset_date = None

        # Positions and data
        self.open_trades = []
        self.contracts = {} # contract_id -> contract_info
        self.symbol_data = {} # Symbol -> { 'ltf_candles': [], 'htf_open': price, 'last_tick': price, ... }

        # UI Compatibility (aggregated or first symbol)
        self.in_position = {'long': False, 'short': False}
        self.position_entry_price = {'long': 0.0, 'short': 0.0}
        self.position_qty = {'long': 0.0, 'short': 0.0}
        self.current_take_profit = {'long': 0.0, 'short': 0.0}
        self.current_stop_loss = {'long': 0.0, 'short': 0.0}

        self.data_lock = threading.Lock()

        # Configure logging
        numeric_level = getattr(logging, self.config.get('log_level', 'INFO').upper(), logging.INFO)
        root_logger = logging.getLogger()
        root_logger.setLevel(numeric_level)

        # Clear handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Console handler
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        root_logger.addHandler(ch)

        # File handler
        fh = logging.FileHandler('debug.log', encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        root_logger.addHandler(fh)

    def _load_config(self):
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            # We can't use self.log yet because it might emit before engine is ready
            logging.error(f"Error loading config: {e}")
            return {}

    def log(self, message, level='info'):
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = {'timestamp': timestamp, 'message': message, 'level': level}
        self.console_logs.append(log_entry)
        self.emit('console_log', log_entry)

        # Also write to standard logging for the debug.log file
        if level == 'error':
            logging.error(message)
        elif level == 'warning':
            logging.warning(message)
        else:
            logging.info(message)

    def _get_ws_url(self):
        app_id = self.config.get('deriv_app_id', '62845')
        return f"wss://ws.binaryws.com/websockets/v3?app_id={app_id}"

    def on_open(self, ws):
        self.log("Deriv WebSocket connected.")
        self.is_connected = True
        self._emit_updates() # Send initial state
        auth_request = {"authorize": self.config.get('deriv_api_token')}
        ws.send(json.dumps(auth_request))

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
        except Exception as e:
            self.log(f"Error parsing message: {e}", 'error')
            return

        msg_type = data.get('msg_type')

        if 'error' in data:
            self.log(f"Deriv Error: {data['error']['message']}", 'error')
            if data['error'].get('code') == 'AuthorizationRequired':
                self.is_running = False
            return

        if msg_type == 'authorize':
            self.log("Authorization successful.")
            auth_data = data.get('authorize', {})
            self.account_balance = auth_data.get('balance', 0.0)
            self.available_balance = self.account_balance
            self.total_equity = self.account_balance

            # Initial daily start balance capture
            if self.daily_start_balance == 0.0:
                self.daily_start_balance = self.account_balance
                self.last_balance_reset_date = datetime.now(timezone.utc).date()
                self.log(f"Daily starting balance set: {self.daily_start_balance}")

            self._emit_updates()

            ws.send(json.dumps({"balance": 1, "subscribe": 1}))

            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

            if self.is_running:
                strat_key = self.config.get('active_strategy', 'strategy_1')
                strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])

                for symbol in self.config.get('symbols', []):
                    self._init_symbol_data(symbol)
                    ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                    self._fetch_history(ws, symbol, strat['ltf_granularity'], 100)
                    h_count = 200 if strat_key in ['strategy_4', 'strategy_5'] else 2
                    self._fetch_history(ws, symbol, strat['htf_granularity'], h_count)

                    if strat_key == 'strategy_5':
                        self._fetch_history(ws, symbol, 60, 100) # 1m
                        self._fetch_history(ws, symbol, 300, 100) # 5m
                        self._fetch_history(ws, symbol, 900, 100) # 15m
                        self._fetch_history(ws, symbol, 3600, 200) # 1h (Need 200 for EMA200)
                        self._fetch_history(ws, symbol, 14400, 100) # 4h
                        self._fetch_history(ws, symbol, 86400, 50) # Daily data
                        ws.send(json.dumps({"contracts_for": symbol}))

        elif msg_type == 'balance':
            self.account_balance = data['balance']['balance']
            self.available_balance = self.account_balance
            self.total_equity = self.account_balance
            self._emit_updates()

        elif msg_type == 'candles':
            echo = data.get('echo_req', {})
            symbol = echo.get('ticks_history')
            granularity = echo.get('granularity')
            candles = data.get('candles', [])
            self._handle_candles(symbol, granularity, candles)

        elif msg_type == 'tick':
            sub_id = data.get('subscription', {}).get('id')
            self._handle_tick(data['tick'], sub_id)

        elif msg_type == 'proposal_open_contract':
            poc = data.get('proposal_open_contract')
            if poc and 'contract_id' in poc:
                self._handle_contract_update(poc)

        elif msg_type == 'contracts_for':
            echo = data.get('echo_req', {})
            symbol = echo.get('contracts_for')
            contracts = data.get('contracts_for', {}).get('available', [])
            multipliers = []
            for c in contracts:
                if c.get('contract_type') == 'MULTUP':
                    multipliers = c.get('multiplier_range', [])
                    break
            if multipliers:
                self.log(f"Available multipliers for {symbol}: {multipliers}")
                self.emit('multipliers_update', {'symbol': symbol, 'multipliers': multipliers})

        elif msg_type == 'buy':
            buy_data = data.get('buy')
            if buy_data:
                cid = buy_data.get('contract_id')
                self.log(f"Trade opened: {cid} for {buy_data.get('buy_price')} USD")

                # Initialize contract entry to start monitoring immediately
                params = data.get('echo_req', {}).get('parameters', {})
                symbol = params.get('symbol')
                ctype = params.get('contract_type')
                side = 'long' if ctype in ['CALL', 'MULTUP'] else 'short'

                with self.data_lock:
                    sd = self.symbol_data.get(symbol, {})
                    self.contracts[cid] = {
                        'id': cid, 'symbol': symbol, 'side': side,
                        'contract_type': ctype,
                        'stake': buy_data.get('buy_price', 0),
                        'pnl': 0, 'is_closing': False,
                        'status': 'Opened',
                        'multiplier': params.get('multiplier'),
                        'tp_price': None, 'sl_price': None,
                        'entry_price': None,
                        'entry_snapshot': sd.get('last_trade_snapshot', {})
                    }
                    # Use last known tick as preliminary entry price for immediate TP/SL tracking
                    if symbol in self.symbol_data and self.symbol_data[symbol].get('last_tick'):
                        self.contracts[cid]['entry_price'] = self.symbol_data[symbol]['last_tick']
                        self._calculate_target_prices(cid)

        elif msg_type == 'sell':
            sell_data = data.get('sell')
            if sell_data:
                self.log(f"Trade closed: {sell_data.get('contract_id')}")

    def _init_symbol_data(self, symbol):
        if symbol not in self.symbol_data:
            self.symbol_data[symbol] = {
                'ltf_candles': [],
                'htf_candles': [],
                'bias_candles': [], # 15m or 4h for Strategy 5
                'daily_candles': [], # Daily for Strategy 5 Multiplier
                'm5_candles': [],
                'm15_candles': [],
                'htf_open': None,
                'htf_epoch': None,
                'last_tick': None,
                'last_processed_ltf': None,
                'last_trade_ltf': None,
                'current_ltf_candle': None,
                'current_htf_candle': None, # for tracking HTF closes
                'current_bias_candle': None,
                'snr_zones': [] # List of { 'price': level, 'type': 'S'|'R'|'Flip', 'touches': n }
            }

    def _fetch_history(self, ws, symbol, granularity, count):
        request = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "granularity": granularity,
            "style": "candles"
        }
        ws.send(json.dumps(request))

    def _handle_candles(self, symbol, granularity, candles):
        strat_key = self.config.get('active_strategy', 'strategy_1')
        strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])

        with self.data_lock:
            if symbol not in self.symbol_data: return
            sd = self.symbol_data[symbol]

            if granularity == 60:
                if len(candles) > 1: sd['ltf_candles'] = candles
                else:
                    sd['ltf_candles'].append(candles[0])
                    if len(sd['ltf_candles']) > 100: sd['ltf_candles'].pop(0)
            if granularity == 300:
                if len(candles) > 1: sd['m5_candles'] = candles
                else:
                    sd['m5_candles'].append(candles[0])
                    if len(sd['m5_candles']) > 100: sd['m5_candles'].pop(0)
            if granularity == 900:
                if len(candles) > 1: sd['m15_candles'] = candles
                else:
                    sd['m15_candles'].append(candles[0])
                    if len(sd['m15_candles']) > 100: sd['m15_candles'].pop(0)
            if granularity == 3600:
                if len(candles) > 1: sd['htf_candles'] = candles
                else:
                    sd['htf_candles'].append(candles[0])
                    if len(sd['htf_candles']) > 200: sd['htf_candles'].pop(0)
            if granularity == 14400:
                if len(candles) > 1: sd['bias_candles'] = candles
                else:
                    sd['bias_candles'].append(candles[0])
                    if len(sd['bias_candles']) > 100: sd['bias_candles'].pop(0)

            if granularity == strat.get('bias_granularity'):
                if candles:
                    sd['current_bias_candle'] = candles[-1]

            if granularity == 86400:
                sd['daily_candles'] = candles
                if strat_key == 'strategy_5':
                    self._update_screener(symbol)

            if granularity == strat['htf_granularity']:
                if candles:
                    now_utc = datetime.now(timezone.utc)
                    # For Daily (86400), start is 00:00 UTC
                    # For Hourly (3600), start is top of the hour
                    # For 15m (900), start is every 15m
                    htf_start_epoch = int(now_utc.replace(second=0, microsecond=0).timestamp())
                    if granularity == 86400:
                        htf_start_epoch = int(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                    elif granularity == 3600:
                        htf_start_epoch = int(now_utc.replace(minute=0, second=0, microsecond=0).timestamp())
                    elif granularity == 900:
                        htf_start_epoch = int(now_utc.replace(minute=(now_utc.minute // 15) * 15, second=0, microsecond=0).timestamp())

                    target_candle = candles[-1]

                    if target_candle['epoch'] < htf_start_epoch:
                        sd['htf_open'] = target_candle['close']
                        sd['htf_epoch'] = htf_start_epoch
                        self.log(f"HTF Open for {symbol} set from previous close: {sd['htf_open']} (Target candle not in history yet)")
                    else:
                        sd['htf_open'] = target_candle['open']
                        sd['htf_epoch'] = target_candle['epoch']
                        self.log(f"HTF Open for {symbol}: {sd['htf_open']} (Epoch: {sd['htf_epoch']})")

                if strat_key == 'strategy_4':
                    sd['htf_candles'] = candles
                    self._calculate_snr_zones(symbol)

                if strat_key == 'strategy_5':
                    sd['htf_candles'] = candles
                    self._update_screener(symbol)

            elif granularity == strat['ltf_granularity']:
                sd['ltf_candles'] = candles
                if candles:
                    sd['current_ltf_candle'] = candles[-1]

    def _handle_tick(self, tick, sub_id=None):
        symbol = tick['symbol']
        price = tick['quote']
        tick_time = datetime.fromtimestamp(tick['epoch'], tz=timezone.utc)
        tick_date = tick_time.date()

        strat_key = self.config.get('active_strategy', 'strategy_1')
        strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])
        ltf_min = strat['ltf_granularity'] // 60
        htf_sec = strat['htf_granularity']

        with self.data_lock:
            # Check for new day to reset daily starting balance
            if self.last_balance_reset_date is None or tick_date > self.last_balance_reset_date:
                self.daily_start_balance = self.account_balance
                self.last_balance_reset_date = tick_date
                self.log(f"New day detected ({tick_date}). Daily starting balance reset to: {self.daily_start_balance}")

                # Refresh daily open if strategy 1 is active (Strategy 1 uses Daily)
                if strat_key == 'strategy_1':
                    for sym in self.config.get('symbols', []):
                        self._fetch_history(self.ws, sym, 86400, 2)

            if symbol not in self.symbol_data: return
            sd = self.symbol_data[symbol]
            sd['last_tick'] = price
            if sub_id and not sd.get('subscription_id'):
                sd['subscription_id'] = sub_id

            # Background Position Monitoring (Force Close, TP/SL)
            # This runs even if is_running is False, as long as WS is connected
            self._monitor_open_contracts(symbol, price)

            if self.is_running:
                # Refresh all timeframes for Strategy 5 periodically
                if strat_key == 'strategy_5':
                    now_epoch = tick.get('epoch')
                    # Refresh every 1m for confirmation
                    if now_epoch % 60 == 0: self._fetch_history(self.ws, symbol, 60, 2)
                    # Refresh every 5m for confirmation
                    if now_epoch % 300 == 0: self._fetch_history(self.ws, symbol, 300, 2)
                    # Refresh every 15m for mid-term
                    if now_epoch % 900 == 0: self._fetch_history(self.ws, symbol, 900, 2)
                    # Refresh every 1h for htf
                    if now_epoch % 3600 == 0: self._fetch_history(self.ws, symbol, 3600, 2)
                    # Refresh every 4h for bias
                    if now_epoch % 14400 == 0: self._fetch_history(self.ws, symbol, 14400, 2)

                # HTF/Bias Refresh for Strategy 5
                if strat_key == 'strategy_5':
                    bias_gran = strat['bias_granularity']
                    if sd['current_bias_candle'] is None or tick.get('epoch') >= sd['current_bias_candle']['epoch'] + bias_gran:
                        self._fetch_history(self.ws, symbol, bias_gran, 200)

                # HTF Refresh for Strategy 2, 3, 4, 5
                if strat_key in ['strategy_2', 'strategy_3', 'strategy_4', 'strategy_5']:
                    htf_gran = strat['htf_granularity']
                    if sd['htf_epoch'] is None or tick.get('epoch') >= sd['htf_epoch'] + htf_gran:
                        last_fetch = sd.get('last_htf_fetch_time', 0)
                        if time.time() - last_fetch > 60: # Throttle to once per minute
                            sd['last_htf_fetch_time'] = time.time()
                            # Fetch more for Strategy 4/5 to recalculate zones/indicators
                            count = 200 if strat_key in ['strategy_4', 'strategy_5'] else 2
                            self._fetch_history(self.ws, symbol, htf_gran, count)

                # HTF Candle Management (Internal tracking for closure triggers)
                if sd['current_htf_candle']:
                    htf_sec = strat['htf_granularity']
                    htf_start = datetime.fromtimestamp(sd['current_htf_candle']['epoch'], tz=timezone.utc)
                    if tick_time >= htf_start + timedelta(seconds=htf_sec):
                        # Store closed HTF candle
                        sd['htf_candles'].append(sd['current_htf_candle'])
                        if len(sd['htf_candles']) > 200: sd['htf_candles'].pop(0)

                        # New HTF candle
                        new_htf_start = int((tick_time.timestamp() // htf_sec) * htf_sec)
                        sd['current_htf_candle'] = {
                            'epoch': new_htf_start, 'open': price, 'high': price, 'low': price, 'close': price
                        }

                        if strat_key == 'strategy_5':
                            self._update_screener(symbol)
                    else:
                        sd['current_htf_candle']['close'] = price
                        sd['current_htf_candle']['high'] = max(sd['current_htf_candle']['high'], price)
                        sd['current_htf_candle']['low'] = min(sd['current_htf_candle']['low'], price)
                else:
                    htf_sec = strat['htf_granularity']
                    new_htf_start = int((tick_time.timestamp() // htf_sec) * htf_sec)
                    sd['current_htf_candle'] = {
                        'epoch': new_htf_start, 'open': price, 'high': price, 'low': price, 'close': price
                    }

                # LTF Candle Management
                if sd['current_ltf_candle']:
                    candle_start = datetime.fromtimestamp(sd['current_ltf_candle']['epoch'], tz=timezone.utc)
                    if tick_time >= candle_start + timedelta(seconds=strat['ltf_granularity']):
                        # LTF Candle transition
                        self.log(f"LTF ({ltf_min}m) Candle closed for {symbol} at {sd['current_ltf_candle']['close']}")

                        # Store closed candle for pattern recognition
                        sd['ltf_candles'].append(sd['current_ltf_candle'])
                        if len(sd['ltf_candles']) > 100: sd['ltf_candles'].pop(0)

                        if self.config.get('entry_type') == 'candle_close':
                            self._process_strategy(symbol, True)

                        # New LTF candle start time
                        new_start_minute = (tick_time.minute // ltf_min) * ltf_min
                        sd['current_ltf_candle'] = {
                            'epoch': int(tick_time.replace(minute=new_start_minute, second=0, microsecond=0).timestamp()),
                            'open': price, 'high': price, 'low': price, 'close': price
                        }
                    else:
                        sd['current_ltf_candle']['close'] = price
                        sd['current_ltf_candle']['high'] = max(sd['current_ltf_candle']['high'], price)
                        sd['current_ltf_candle']['low'] = min(sd['current_ltf_candle']['low'], price)

                if self.config.get('entry_type') == 'tick':
                    self._process_strategy(symbol, False)
                    if strat_key == 'strategy_5':
                        self._update_screener(symbol)

    def _update_screener(self, symbol):
        sd = self.symbol_data.get(symbol)
        if not sd or len(sd.get('htf_candles', [])) < 200: return

        df_h = pd.DataFrame(sd['htf_candles'])
        last_close = df_h['close'].iloc[-1]

        # --- A) TREND BLOCK (Weight 3) ---
        # Indicators: EMA 50, EMA 200, SMA 20, ADX, Ichimoku, MACD, Aroon, DPO, KST, TRIX, Vortex, WMA
        t_pos, t_neg = 0, 0

        ema50 = ta.trend.EMAIndicator(df_h['close'], window=50).ema_indicator().iloc[-1]
        ema200 = ta.trend.EMAIndicator(df_h['close'], window=200).ema_indicator().iloc[-1]
        sma20 = ta.trend.SMAIndicator(df_h['close'], window=20).sma_indicator().iloc[-1]

        # EMA/SMA Alignment
        if last_close > ema50: t_pos += 1
        else: t_neg += 1
        if ema50 > ema200: t_pos += 1
        else: t_neg += 1
        if last_close > sma20: t_pos += 1
        else: t_neg += 1

        # ADX (Trend Strength)
        adx_ind = ta.trend.ADXIndicator(df_h['high'], df_h['low'], df_h['close'])
        adx = adx_ind.adx().iloc[-1]
        if adx > 25:
            if last_close > ema50: t_pos += 1
            else: t_neg += 1

        # Ichimoku Cloud
        ichimoku = ta.trend.IchimokuIndicator(df_h['high'], df_h['low'])
        span_a = ichimoku.ichimoku_a().iloc[-1]
        span_b = ichimoku.ichimoku_b().iloc[-1]
        if last_close > span_a and last_close > span_b: t_pos += 1
        elif last_close < span_a and last_close < span_b: t_neg += 1

        # MACD
        macd_ind = ta.trend.MACD(df_h['close'])
        if macd_ind.macd().iloc[-1] > macd_ind.macd_signal().iloc[-1]: t_pos += 1
        else: t_neg += 1

        # Aroon
        aroon = ta.trend.AroonIndicator(df_h['high'], df_h['low'])
        if aroon.aroon_up().iloc[-1] > aroon.aroon_down().iloc[-1]: t_pos += 1
        else: t_neg += 1

        # DPO
        dpo = ta.trend.DPOIndicator(df_h['close']).dpo().iloc[-1]
        if dpo > 0: t_pos += 1
        else: t_neg += 1

        # KST
        kst = ta.trend.KSTIndicator(df_h['close'])
        if kst.kst().iloc[-1] > kst.kst_sig().iloc[-1]: t_pos += 1
        else: t_neg += 1

        # TRIX
        trix = ta.trend.TRIXIndicator(df_h['close']).trix().iloc[-1]
        if trix > 0: t_pos += 1
        else: t_neg += 1

        # Vortex
        vortex = ta.trend.VortexIndicator(df_h['high'], df_h['low'], df_h['close'])
        if vortex.vortex_indicator_pos().iloc[-1] > vortex.vortex_indicator_neg().iloc[-1]: t_pos += 1
        else: t_neg += 1

        # WMA
        wma = ta.trend.WMAIndicator(df_h['close'], window=9).wma_indicator().iloc[-1]
        if last_close > wma: t_pos += 1
        else: t_neg += 1

        trend_sentiment = (t_pos - t_neg) / (t_pos + t_neg) if (t_pos + t_neg) > 0 else 0
        trend_score = trend_sentiment * 3

        # --- B) MOMENTUM BLOCK (Weight 2) ---
        # Indicators: RSI, Stoch RSI, Williams %R, ROC, CCI, TSI, Ultimate Oscillator, PPO
        m_pos, m_neg = 0, 0

        rsi = ta.momentum.RSIIndicator(df_h['close']).rsi().iloc[-1]
        if rsi > 50: m_pos += 1
        else: m_neg += 1

        stoch_rsi = ta.momentum.StochRSIIndicator(df_h['close']).stochrsi().iloc[-1]
        if stoch_rsi > 0.5: m_pos += 1
        else: m_neg += 1

        wr = ta.momentum.WilliamsRIndicator(df_h['high'], df_h['low'], df_h['close']).williams_r().iloc[-1]
        if wr > -50: m_pos += 1
        else: m_neg += 1

        roc = ta.momentum.ROCIndicator(df_h['close']).roc().iloc[-1]
        if roc > 0: m_pos += 1
        else: m_neg += 1

        cci = ta.trend.CCIIndicator(df_h['high'], df_h['low'], df_h['close']).cci().iloc[-1]
        if cci > 0: m_pos += 1
        else: m_neg += 1

        # TSI
        tsi = ta.momentum.TSIIndicator(df_h['close']).tsi().iloc[-1]
        if tsi > 0: m_pos += 1
        else: m_neg += 1

        # Ultimate Oscillator
        uo = ta.momentum.UltimateOscillator(df_h['high'], df_h['low'], df_h['close']).ultimate_oscillator().iloc[-1]
        if uo > 50: m_pos += 1
        else: m_neg += 1

        # PPO
        ppo = ta.momentum.PercentagePriceOscillator(df_h['close']).ppo().iloc[-1]
        if ppo > 0: m_pos += 1
        else: m_neg += 1

        mom_sentiment = (m_pos - m_neg) / (m_pos + m_neg) if (m_pos + m_neg) > 0 else 0
        mom_score = mom_sentiment * 2

        # --- C) VOLATILITY BLOCK (Weight 1) ---
        # Indicators: ATR, Bollinger Bands, Donchian Channel, Keltner Channel, Ulcer Index, Mass Index
        v_pos, v_neg = 0, 0

        atr_ind = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close'])
        atr = atr_ind.average_true_range().iloc[-1]
        atr_prev = atr_ind.average_true_range().iloc[-2]
        if atr > atr_prev: v_pos += 0.5 # Expanding volatility is trend-supportive

        bb = ta.volatility.BollingerBands(df_h['close'])
        bbw = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / bb.bollinger_mavg().iloc[-1]
        prev_bbw = (bb.bollinger_hband().iloc[-2] - bb.bollinger_lband().iloc[-2]) / bb.bollinger_mavg().iloc[-2]
        if bbw > prev_bbw: v_pos += 0.5

        dc = ta.volatility.DonchianChannel(df_h['high'], df_h['low'], df_h['close'])
        # Price relative to DC midline
        dc_mid = (dc.donchian_channel_hband().iloc[-1] + dc.donchian_channel_lband().iloc[-1]) / 2
        if last_close > dc_mid: v_pos += 0.5
        else: v_neg += 0.5

        kc = ta.volatility.KeltnerChannel(df_h['high'], df_h['low'], df_h['close'])
        if last_close > kc.keltner_channel_mband().iloc[-1]: v_pos += 0.5
        else: v_neg += 0.5

        # Ulcer Index
        ui = ta.volatility.UlcerIndex(df_h['close']).ulcer_index().iloc[-1]
        if ui < 5: v_pos += 0.5
        else: v_neg += 0.5

        # Mass Index
        mi = ta.trend.MassIndex(df_h['high'], df_h['low']).mass_index().iloc[-1]
        if mi < 25: v_pos += 0.5
        else: v_neg += 0.5

        vol_sentiment = (v_pos - v_neg) / (v_pos + v_neg) if (v_pos + v_neg) > 0 else 0
        vol_score = vol_sentiment * 1

        # --- D) STRUCTURE / MEAN REVERSION BLOCK (Weight 2) ---
        # Indicators: Price distance from EMA, Z-score, BB band touch, Pivot points, Daily Proximity, SNR Alignment
        s_pos, s_neg = 0, 0

        # Price distance from EMA 50
        dist = (last_close - ema50) / ema50
        if trend_score > 0:
            if 0 < dist < 0.05: s_pos += 1 # Healthy distance
            elif dist > 0.1: s_neg += 0.5 # Overextended
        elif trend_score < 0:
            if -0.05 < dist < 0: s_pos += 1
            elif dist < -0.1: s_neg += 0.5

        # Z-Score (20 period)
        sma20_v = df_h['close'].rolling(window=20).mean()
        std20_v = df_h['close'].rolling(window=20).std()
        z_score = (last_close - sma20_v.iloc[-1]) / std20_v.iloc[-1]
        if abs(z_score) < 2: s_pos += 1
        else: s_neg += 1 # Overextended

        # BB Band Touch
        if last_close >= bb.bollinger_hband().iloc[-1]: s_neg += 1 # Resistance touch
        elif last_close <= bb.bollinger_lband().iloc[-1]: s_pos += 1 # Support touch

        # Pivot Points (Standard)
        if sd.get('daily_candles') and len(sd['daily_candles']) >= 2:
            prev_day = sd['daily_candles'][-2]
            pivot = (prev_day['high'] + prev_day['low'] + prev_day['close']) / 3
            r1 = 2 * pivot - prev_day['low']
            s1 = 2 * pivot - prev_day['high']
            if last_close > pivot: s_pos += 0.5
            if last_close > r1: s_neg += 0.5 # Potential reversal
            if last_close < s1: s_pos += 0.5 # Potential bounce

            # Daily Proximity
            day_high = prev_day['high']
            day_low = prev_day['low']
            if abs(last_close - day_high) / day_high < 0.01: s_neg += 0.5 # Near daily high
            if abs(last_close - day_low) / day_low < 0.01: s_pos += 0.5 # Near daily low

        # HTF SNR Alignment
        zones = sd.get('snr_zones', [])
        for z in zones:
            if abs(last_close - z['price']) / z['price'] < 0.005:
                if z['type'] == 'S': s_pos += 1
                elif z['type'] == 'R': s_neg += 1

        struct_sentiment = (s_pos - s_neg) / (s_pos + s_neg) if (s_pos + s_neg) > 0 else 0
        struct_score = struct_sentiment * 2

        # --- Confidence Calculation ---
        # Total Max Weight = 3 + 2 + 1 + 2 = 8
        raw_sum = trend_score + mom_score + vol_score + struct_score
        confidence = (raw_sum / 8.0) * 100

        regime = "Ranging"
        _adx = adx
        _ema50 = ema50
        _ema200 = ema200
        if _adx > 25:
            regime = "Trending Up" if _ema50 > _ema200 else "Trending Down"

        # Recommendations
        abs_conf = abs(confidence)
        suggested_expiry = 5
        if abs_conf >= 70: suggested_expiry = 15
        elif abs_conf >= 55: suggested_expiry = 10
        elif abs_conf >= 40: suggested_expiry = 5

        suggested_multiplier = 5
        if abs_conf >= 80: suggested_multiplier = 50
        elif abs_conf >= 65: suggested_multiplier = 20
        elif abs_conf >= 50: suggested_multiplier = 5

        # SL/TP calculation using ATR (1H)
        atr_1h = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close']).average_true_range().iloc[-1]
        # RiskFactor = 0.5-1.5 based on confidence
        risk_factor = 0.5 + (abs_conf / 100.0)
        sl_pips = atr_1h * risk_factor
        tp_pips = sl_pips * 1.5 # 1:1.5 RR

        self.screener_data[symbol] = {
            'confidence': round(confidence, 1),
            'direction': 'CALL' if confidence > 0 else 'PUT',
            'regime': regime,
            'trend': round(trend_score, 1),
            'momentum': round(mom_score, 1),
            'volatility': round(vol_score, 1),
            'structure': round(struct_score, 1),
            'adx': round(_adx, 1),
            'expiry_min': suggested_expiry,
            'multiplier': suggested_multiplier,
            'sl_pips': round(sl_pips, 4),
            'tp_pips': round(tp_pips, 4)
        }

        # Emit to UI
        self.emit('screener_update', {'symbol': symbol, 'data': self.screener_data[symbol]})

    def _calculate_snr_zones(self, symbol):
        sd = self.symbol_data.get(symbol)
        if not sd or not sd['htf_candles']: return

        candles = sd['htf_candles'][-100:]
        if len(candles) < 20: return

        levels = []
        # Find local peaks and troughs
        for i in range(1, len(candles) - 1):
            # Resistance: Peak
            if candles[i]['high'] > candles[i-1]['high'] and candles[i]['high'] > candles[i+1]['high']:
                levels.append({'price': candles[i]['high'], 'type': 'R'})
            # Support: Trough
            if candles[i]['low'] < candles[i-1]['low'] and candles[i]['low'] < candles[i+1]['low']:
                levels.append({'price': candles[i]['low'], 'type': 'S'})

        # Cluster levels
        # Threshold: 0.05% of price
        if not levels: return
        avg_price = sum(c['close'] for c in candles) / len(candles)
        threshold = avg_price * 0.0005

        clusters = []
        for l in levels:
            found = False
            for c in clusters:
                if abs(l['price'] - c['price']) < threshold:
                    c['prices'].append(l['price'])
                    c['touches'] += 1
                    # If it was R and now S, it's a Flip
                    if l['type'] != c['last_type']:
                        c['is_flip'] = True
                    c['last_type'] = l['type']
                    found = True
                    break
            if not found:
                clusters.append({
                    'price': l['price'],
                    'touches': 1,
                    'is_flip': False,
                    'last_type': l['type'],
                    'prices': [l['price']]
                })

        # Refine clusters: calculate mean price and filter by touches >= 2
        active_zones = []
        for c in clusters:
            if c['touches'] >= 2:
                mean_price = sum(c['prices']) / len(c['prices'])
                active_zones.append({
                    'price': mean_price,
                    'touches': c['touches'],
                    'is_flip': c['is_flip'],
                    'type': 'Flip' if c['is_flip'] else c['last_type']
                })

        # Sort by strength (touches) and take top 5
        active_zones.sort(key=lambda x: x['touches'], reverse=True)
        sd['snr_zones'] = active_zones[:5]

        if active_zones:
            levels_str = ", ".join([f"{z['price']:.2f}({z['type']})" for z in sd['snr_zones']])
            self.log(f"SNR Zones for {symbol}: {levels_str}")

    def _check_price_action_patterns(self, candles):
        if len(candles) < 2: return None

        curr = candles[-1]
        prev = candles[-2]

        body = abs(curr['close'] - curr['open'])
        upper_wick = curr['high'] - max(curr['open'], curr['close'])
        lower_wick = min(curr['open'], curr['close']) - curr['low']
        total_range = curr['high'] - curr['low']

        if total_range == 0: return None

        # Marubozu (Aggression) check
        # If body is more than 90% of total range, it's aggressive
        is_marubozu = body > (total_range * 0.9)
        if is_marubozu: return "marubozu"

        # Pin Bar / Hammer
        # Body is small (less than 35% of range), one wick is > 60% of range
        if body < (total_range * 0.35):
            if lower_wick > (total_range * 0.6):
                return "bullish_pin"
            if upper_wick > (total_range * 0.6):
                return "bearish_pin"

        # Engulfing
        prev_body = abs(prev['close'] - prev['open'])
        if body > prev_body:
            # Bullish Engulfing
            if curr['close'] > curr['open'] and prev['close'] < prev['open']:
                if curr['close'] >= prev['open'] and curr['open'] <= prev['close']:
                    return "bullish_engulfing"
            # Bearish Engulfing
            if curr['close'] < curr['open'] and prev['close'] > prev['open']:
                if curr['close'] <= prev['open'] and curr['open'] >= prev['close']:
                    return "bearish_engulfing"

        # Harami (Inside bar)
        if body < prev_body * 0.5:
            if max(curr['open'], curr['close']) <= max(prev['open'], prev['close']) and \
               min(curr['open'], curr['close']) >= min(prev['open'], prev['close']):
                if curr['close'] > curr['open']: return "bullish_harami"
                else: return "bearish_harami"

        # Tweezer
        if abs(curr['high'] - prev['high']) < (total_range * 0.05) and curr['high'] > max(curr['open'], curr['close']):
            return "tweezer_top"
        if abs(curr['low'] - prev['low']) < (total_range * 0.05) and curr['low'] < min(curr['open'], curr['close']):
            return "tweezer_bottom"

        # Doji (Indecision)
        # Body is very small (less than 10% of range)
        if body < (total_range * 0.1):
            return "doji"

        return None

    def _process_strategy(self, symbol, is_candle_close):
        # Check Max Daily Loss relative to starting balance of the day
        max_loss_pct = self.config.get('max_daily_loss_pct', 5)
        if self.daily_start_balance > 0:
            # Current Net Profit is total since start.
            # We need daily pnl = (current_equity - daily_start_balance)
            current_equity = self.account_balance + sum(c.get('pnl', 0) for c in self.contracts.values())
            daily_pnl = current_equity - self.daily_start_balance
            current_loss_pct = (daily_pnl / self.daily_start_balance) * 100

            if current_loss_pct <= -max_loss_pct:
                if self.is_running:
                    self.log(f"Max daily loss reached ({current_loss_pct:.2f}% of starting balance). Trading paused.", "warning")
                    self.is_running = False
                return

        sd = self.symbol_data[symbol]
        htf_open = sd['htf_open']
        current_ltf = sd['current_ltf_candle']
        current_price = sd['last_tick']

        if htf_open is None or current_ltf is None or current_price is None:
            return

        time_key = current_ltf['epoch']
        if sd.get('last_processed_ltf') == time_key and is_candle_close:
            return # Already processed this candle close

        # Strategy Signal Logic
        signal = None
        strat_key = self.config.get('active_strategy', 'strategy_1')

        if strat_key == 'strategy_4':
            # SNR Price Action Logic
            if not is_candle_close: return # Only on 1m candle close

            zones = sd.get('snr_zones', [])
            if not zones: return

            pattern = self._check_price_action_patterns(sd['ltf_candles'])
            if not pattern or pattern == "marubozu": return

            # Check if current candle touched any zone
            for z in zones:
                # Buffer: 0.02%
                buffer = z['price'] * 0.0002
                touched = current_ltf['low'] <= (z['price'] + buffer) and current_ltf['high'] >= (z['price'] - buffer)

                if touched:
                    # Bullish Reversal at Support or Flip
                    if z['type'] in ['S', 'Flip'] and pattern in ['bullish_pin', 'bullish_engulfing', 'doji']:
                        if current_ltf['close'] > current_ltf['open']: # Confirm bullish
                            signal = 'buy'
                            self.log(f"Strategy 4 BUY Signal: {pattern} at {z['type']} zone {z['price']:.2f}")
                            break
                    # Bearish Reversal at Resistance or Flip
                    elif z['type'] in ['R', 'Flip'] and pattern in ['bearish_pin', 'bearish_engulfing', 'doji']:
                        if current_ltf['close'] < current_ltf['open']: # Confirm bearish
                            signal = 'sell'
                            self.log(f"Strategy 4 SELL Signal: {pattern} at {z['type']} zone {z['price']:.2f}")
                            break
        elif strat_key == 'strategy_5':
            # Intelligence Screener Strategy
            metrics = self.screener_data.get(symbol)
            if not metrics: return

            # 15m Bias Check
            if not sd['bias_candles']: return
            bias_c = sd['bias_candles'][-1]
            bias_bullish = bias_c['close'] > bias_c['open']

            # Confidence Threshold (e.g. > 60%)
            if abs(metrics['confidence']) >= 60:
                direction = metrics['direction']

                # Multi-TF Alignment
                if direction == 'CALL' and bias_bullish:
                    # Timing: Wait for bullish LTF candle or breakout
                    if not sd['ltf_candles']: return
                    last_ltf = sd['ltf_candles'][-1]
                    if last_ltf['close'] > last_ltf['open']:
                        self.log(f"Strategy 5 BUY on {symbol} - Confidence: {metrics['confidence']}%")
                        signal = 'buy'
                elif direction == 'PUT' and not bias_bullish:
                    if not sd['ltf_candles']: return
                    last_ltf = sd['ltf_candles'][-1]
                    if last_ltf['close'] < last_ltf['open']:
                        self.log(f"Strategy 5 SELL on {symbol} - Confidence: {metrics['confidence']}%")
                        signal = 'sell'
        else:
            # Default Breakout Logic (Strategy 1, 2, 3)
            if strat_key == 'strategy_1' and not is_candle_close:
                return

            check_price = current_ltf['close'] if is_candle_close else current_price

            # BUY: LTF open <= HTF Open AND check_price > HTF Open AND bullish
            if current_ltf['open'] <= htf_open and check_price > htf_open and check_price > current_ltf['open']:
                signal = 'buy'
            # SELL: LTF open >= HTF Open AND check_price < HTF Open AND bearish
            elif current_ltf['open'] >= htf_open and check_price < htf_open and check_price < current_ltf['open']:
                signal = 'sell'

        if signal:
            # Check if we already traded this LTF period for this symbol to avoid multiple entries on ticks
            if sd.get('last_trade_ltf') == time_key:
                return

            sd['last_trade_ltf'] = time_key
            if is_candle_close:
                sd['last_processed_ltf'] = time_key

            self._execute_trade(symbol, signal)

    def _monitor_open_contracts(self, symbol=None, current_price=None):
        now_epoch = int(time.time())
        force_close_enabled = self.config.get('force_close_enabled', False)
        force_close_duration = self.config.get('force_close_duration', 60)
        tp_enabled = self.config.get('tp_enabled', False)
        sl_enabled = self.config.get('sl_enabled', False)

        for cid in list(self.contracts.keys()):
            c = self.contracts[cid]
            if symbol != c['symbol']: continue

            side = c.get('side')
            is_long = side == 'long'

            # --- DECISION MAKING POSITION ENGINE ---
            strat_key = self.config.get('active_strategy')
            if strat_key == 'strategy_5' and current_price:
                # Get current market intelligence
                metrics = self.screener_data.get(symbol, {})
                sd = self.symbol_data.get(symbol, {})
                df_h = pd.DataFrame(sd.get('htf_candles', []))

                if not df_h.empty and len(df_h) >= 200:
                    # 1. Score-based Decision Engine
                    exit_reason = None
                    status = "Holding"

                    trend_score = metrics.get('trend', 0)
                    mom_score = metrics.get('momentum', 0)
                    conf = metrics.get('confidence', 0)

                    # Hard Exit Conditions
                    if (is_long and trend_score < -1) or (not is_long and trend_score > 1):
                        exit_reason = f"Trend score flip ({trend_score})"
                    elif abs(conf) < 30:
                        exit_reason = f"Confidence weak ({conf}%)"
                    elif (is_long and mom_score < -2) or (not is_long and mom_score > 2):
                        exit_reason = f"Momentum reversal ({mom_score})"

                    # Confidence Decay Check (Relative to entry)
                    entry_conf = c.get('entry_snapshot', {}).get('confidence', 0)
                    if (is_long and conf < entry_conf - 25) or (not is_long and conf > entry_conf + 25):
                        exit_reason = f"Intelligence decay ({entry_conf}% -> {conf}%)"

                    # Alert/Weakening conditions (Non-exit)
                    if not exit_reason:
                        if abs(conf) < 50 or abs(trend_score) < 2:
                            status = "Weakening"
                        else:
                            status = "Holding"
                    else:
                        status = "Closing"

                    c['status'] = status

                    if exit_reason:
                        self.log(f"Strategy 5 Engine EXIT for {symbol} ({cid}): {exit_reason}.")
                        self._close_contract(cid)
                        continue

                    # 2. Dynamic Trailing Stop Loss (Multipliers Only)
                    is_multiplier = c.get('contract_type') in ['MULTUP', 'MULTDOWN']
                    if is_multiplier:
                        atr = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close']).average_true_range().iloc[-1]
                    entry_price = c.get('entry_price')
                    if entry_price:
                        profit_pips = (current_price - entry_price) if is_long else (entry_price - current_price)

                        # Move to Breakeven at 1 ATR profit
                        if profit_pips >= atr and not c.get('is_breakeven'):
                            self.log(f"Multiplier TRAIL for {symbol}: 1 ATR profit reached. Moving SL to breakeven.")
                            # We don't have server-side SL adjustment here easily without another API call,
                            # but we can track it internally for our failsafe tracking.
                            c['sl_price'] = entry_price
                            c['is_breakeven'] = True

                        # Trail at 2 ATR profit
                        if profit_pips >= 2 * atr:
                            ema20 = ta.trend.EMAIndicator(df_h['close'], window=20).ema_indicator().iloc[-1]
                            new_sl = ema20 if is_long else ema20
                            # Ensure we don't move SL backwards
                            if is_long:
                                if new_sl > c.get('sl_price', 0):
                                    c['sl_price'] = new_sl
                            else:
                                if c.get('sl_price', 999999) > new_sl:
                                    c['sl_price'] = new_sl

            # Price-based TP/SL trigger (Fail-safe tracking for both types)
            if current_price and symbol == c['symbol'] and (tp_enabled or sl_enabled):
                    tp_price = c.get('tp_price')
                    sl_price = c.get('sl_price')

                    if is_long:
                        if tp_enabled and tp_price and current_price >= tp_price:
                            self.log(f"TP reached for {c['symbol']} ({cid}): {current_price} >= {tp_price}. Closing...")
                            self._close_contract(cid)
                            continue
                        if sl_enabled and sl_price and current_price <= sl_price:
                            self.log(f"SL reached for {c['symbol']} ({cid}): {current_price} <= {sl_price}. Closing...")
                            self._close_contract(cid)
                            continue
                    else:
                        if tp_enabled and tp_price and current_price <= tp_price:
                            self.log(f"TP reached for {c['symbol']} ({cid}): {current_price} <= {tp_price}. Closing...")
                            self._close_contract(cid)
                            continue
                        if sl_enabled and sl_price and current_price >= sl_price:
                            self.log(f"SL reached for {c['symbol']} ({cid}): {current_price} >= {sl_price}. Closing...")
                            self._close_contract(cid)
                            continue

            # Ghost cleanup: if expired more than 60s ago and still here
            if c.get('expiry_time') and now_epoch > c['expiry_time'] + 60:
                self.log(f"Cleaning up ghost contract {cid} for {c['symbol']} (expired 60s ago).")
                del self.contracts[cid]
                continue

            if c.get('is_closing'):
                # Retry closing if it's been in is_closing state for more than 30s
                if c.get('last_close_attempt') and now_epoch - c['last_close_attempt'] > 30:
                    self.log(f"Retrying close for contract {cid} ({c['symbol']})...")
                    self._close_contract(cid)
                continue

            # Force Close Check
            purchase_time = c.get('purchase_time')
            if force_close_enabled and purchase_time:
                elapsed = now_epoch - purchase_time
                if elapsed >= force_close_duration:
                    self.log(f"Force close duration reached for {c['symbol']} ({cid}): {elapsed}s elapsed. Closing...")
                    self.contracts[cid]['is_closing'] = True
                    self._close_contract(cid)
                    continue

            # TP/SL check (Redundant but safe if proposal updates are slow)
            profit = c.get('pnl', 0)
            stake = c.get('stake', 0)
            use_fixed = self.config.get('use_fixed_balance', True)

            tp_val = self.config.get('tp_value', 0)
            sl_val = self.config.get('sl_value', 0)

            if use_fixed:
                tp_threshold = tp_val
                sl_threshold = -sl_val # SL is input as positive, we check for profit <= negative
            else:
                tp_threshold = stake * (tp_val / 100.0)
                sl_threshold = -stake * (sl_val / 100.0)

            if tp_enabled and tp_val > 0 and profit >= tp_threshold:
                self.log(f"TP reached (monitor) for {c['symbol']} ({cid}): {profit:.2f} USD (Target: >= {tp_threshold:.2f}). Closing...")
                self.contracts[cid]['is_closing'] = True
                self._close_contract(cid)
            elif sl_enabled and sl_val > 0 and profit <= sl_threshold:
                self.log(f"SL reached (monitor) for {c['symbol']} ({cid}): {profit:.2f} USD (Target: <= {sl_threshold:.2f}). Closing...")
                self.contracts[cid]['is_closing'] = True
                self._close_contract(cid)

    def _execute_trade(self, symbol, side):
        # side is 'buy' or 'sell' from strategy
        internal_side = 'long' if side == 'buy' else 'short'

        strat_key = self.config.get('active_strategy', 'strategy_1')
        strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])

        duration_seconds = 0
        now = datetime.now(timezone.utc)
        expiry_label = ""

        custom_expiry = self.config.get('custom_expiry', 'default')

        if strat_key == 'strategy_5':
            # Dynamic expiry based on confidence and volatility
            metrics = self.screener_data.get(symbol, {})
            duration_minutes = metrics.get('expiry_min', 10)
            duration_seconds = duration_minutes * 60
            expiry_label = f"Dynamic Expiry: {duration_minutes}m"
        elif strat['expiry_type'] == 'eod':
            # End of day calculation (UTC)
            end_of_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            duration_seconds = int((end_of_day - now).total_seconds())
            expiry_label = f"Expiry: {end_of_day.strftime('%H:%M:%S')} UTC"
        elif strat['expiry_type'] == 'fixed':
            if custom_expiry != 'default':
                try:
                    duration_seconds = int(custom_expiry)
                except:
                    duration_seconds = strat['duration']
            else:
                # Calculate duration till NEXT HTF candle close for Strategy 2 and 3
                # if the user wants "Time till candle close" behavior
                if strat_key in ['strategy_2', 'strategy_3']:
                    htf_gran = strat['htf_granularity']
                    next_close_epoch = ((int(now.timestamp()) // htf_gran) + 1) * htf_gran
                    duration_seconds = next_close_epoch - int(now.timestamp())
                else:
                    duration_seconds = strat['duration']

            if duration_seconds >= 60:
                expiry_label = f"Expiry: {duration_seconds // 60}m {duration_seconds % 60}s"
            else:
                expiry_label = f"Expiry: {duration_seconds} seconds"

        if duration_seconds <= 0:
            return

        # Position management: One trade per symbol, cancel opposite
        existing_cid = None
        for cid, c in self.contracts.items():
            if c['symbol'] == symbol:
                if c['side'] == internal_side:
                    self.log(f"Trade already exists for {symbol} in {side} direction.")
                    return
                else:
                    existing_cid = cid
                    break

        if existing_cid:
            self.log(f"Closing opposite {self.contracts[existing_cid]['side']} trade for {symbol}.")
            self._close_contract(existing_cid)

        # Place trade
        amount = self.config.get('balance_value', 10)
        if not self.config.get('use_fixed_balance'):
            amount = (amount / 100.0) * self.account_balance

        amount = max(0.35, round(amount, 2))

        contract_type = self.config.get('contract_type', 'rise_fall')
        is_multiplier = (strat_key == 'strategy_5' and contract_type == 'multiplier')

        if is_multiplier:
            # Use 5% of balance for multipliers to grow exponentially but safely
            if not self.config.get('use_fixed_balance'):
                amount = max(0.35, round(self.account_balance * 0.05, 2))

            # Use suggested multiplier from screener if available
            metrics = self.screener_data.get(symbol, {})
            mult_val = metrics.get('multiplier', int(self.config.get('multiplier_value', 100)))

            # Multiplier TP/SL must be absolute USD
            tp_val_config = self.config.get('tp_value', 0)
            sl_val_config = self.config.get('sl_value', 0)
            use_fixed = self.config.get('use_fixed_balance', True)

            tp_usd = tp_val_config if use_fixed else (amount * tp_val_config / 100.0)
            sl_usd = sl_val_config if use_fixed else (amount * sl_val_config / 100.0)

            self.log(f"Opening MULTIPLIER {side.upper()} on {symbol} | Stake: {amount} | Mult: {mult_val}x")

            buy_request = {
                "buy": 1,
                "price": amount,
                "parameters": {
                    "amount": amount,
                    "basis": "stake",
                    "contract_type": "MULTUP" if side == 'buy' else "MULTDOWN",
                    "currency": "USD",
                    "multiplier": mult_val,
                    "symbol": symbol
                }
            }

            # Multipliers use limit_order for TP/SL
            limit_order = {}
            if self.config.get('tp_enabled') and tp_usd > 0:
                limit_order['take_profit'] = round(tp_usd, 2)
            if self.config.get('sl_enabled') and sl_usd > 0:
                limit_order['stop_loss'] = round(sl_usd, 2)

            if limit_order:
                buy_request['parameters']['limit_order'] = limit_order
        else:
            self.log(f"Opening {side.upper()} on {symbol} | Stake: {amount} | {expiry_label}")

            buy_request = {
                "buy": 1,
                "price": amount,
                "parameters": {
                    "amount": amount,
                    "basis": "stake",
                    "contract_type": "CALL" if side == 'buy' else "PUT",
                    "currency": "USD",
                    "duration": duration_seconds,
                    "duration_unit": "s",
                    "symbol": symbol
                }
            }
        if self.ws and self.ws.sock and self.ws.sock.connected:
            # Capture entry snapshot for Multiplier position management
            if is_multiplier:
                metrics = self.screener_data.get(symbol, {})
                sd = self.symbol_data.get(symbol)
                df_h = pd.DataFrame(sd.get('htf_candles', []))

                snapshot = {
                    'direction': side,
                    'confidence': metrics.get('confidence', 0),
                    'atr': ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close']).average_true_range().iloc[-1] if not df_h.empty else 0,
                    'adx': metrics.get('adx', 0),
                    'ema50': ta.trend.EMAIndicator(df_h['close'], window=50).ema_indicator().iloc[-1] if not df_h.empty else 0,
                    'ema200': ta.trend.EMAIndicator(df_h['close'], window=200).ema_indicator().iloc[-1] if not df_h.empty else 0,
                }
                sd['last_trade_snapshot'] = snapshot

            self.ws.send(json.dumps(buy_request))

    def _close_contract(self, contract_id):
        if self.ws and self.ws.sock and self.ws.sock.connected:
            if contract_id in self.contracts:
                self.contracts[contract_id]['last_close_attempt'] = int(time.time())
            self.ws.send(json.dumps({"sell": contract_id, "price": 0}))

    def _handle_contract_update(self, contract):
        try:
            cid = contract['contract_id']
            symbol = contract['underlying']
            is_sold = contract['is_sold']
            # Map Deriv types to our long/short internal state
            ctype = contract['contract_type']
            side = 'long' if ctype in ['CALL', 'MULTUP'] else 'short'

            if is_sold:
                if cid in self.contracts:
                    self.contracts[cid]['status'] = 'Sold'
                    profit = contract.get('profit', 0)
                    self.log(f"Trade {cid} ({symbol}) closed. PnL: {profit}")
                    self.net_trade_profit += profit
                    if profit > 0:
                        self.total_trade_profit += profit
                        self.wins_count += 1
                    else:
                        self.total_trade_loss += abs(profit)
                        self.losses_count += 1
                    self.total_trades_count += 1
                    del self.contracts[cid]
            else:
                profit = contract.get('profit', 0)
                is_closing = self.contracts.get(cid, {}).get('is_closing', False)
                entry_tick = contract.get('entry_tick')

                # Retrieve or initialize contract data
                c_data = self.contracts.get(cid, {})

                self.contracts[cid] = {
                    'id': cid, 'symbol': symbol, 'side': side,
                    'contract_type': ctype,
                    'entry_price': entry_tick,
                    'pnl': profit,
                    'stake': contract.get('buy_price', 0),
                    'purchase_time': contract.get('purchase_time'),
                    'expiry_time': contract.get('date_expiry'),
                    'is_closing': is_closing,
                    'status': c_data.get('status', 'Active'),
                    'multiplier': contract.get('multiplier'),
                    'tp_price': c_data.get('tp_price'),
                    'sl_price': c_data.get('sl_price')
                }

                # Calculate TP/SL prices if not yet set and we have an entry price
                if entry_tick and not self.contracts[cid]['tp_price']:
                    self._calculate_target_prices(cid)

                # TP/SL check
                if not is_closing:
                    # Force Close Duration Check
                    force_close_enabled = self.config.get('force_close_enabled', False)
                    force_close_duration = self.config.get('force_close_duration', 60)
                    purchase_time = contract.get('purchase_time')

                    if force_close_enabled and purchase_time:
                        now_epoch = int(time.time())
                        if now_epoch - purchase_time >= force_close_duration:
                            self.log(f"Force close duration reached for {symbol} ({cid}). Closing...")
                            self.contracts[cid]['is_closing'] = True
                            self._close_contract(cid)
                            return # Skip further checks if closing

                    tp_enabled = self.config.get('tp_enabled', False)
                    sl_enabled = self.config.get('sl_enabled', False)

                    use_fixed = self.config.get('use_fixed_balance', True)
                    stake = contract.get('buy_price', 0)

                    tp_val = self.config.get('tp_value', 0)
                    sl_val = self.config.get('sl_value', 0)

                    if use_fixed:
                        tp_threshold = tp_val
                        sl_threshold = -sl_val
                    else:
                        tp_threshold = stake * (tp_val / 100.0)
                        sl_threshold = -stake * (sl_val / 100.0)

                    if tp_enabled and tp_val > 0 and profit >= tp_threshold:
                        self.log(f"TP reached for {symbol} ({cid}): {profit:.2f} USD (Target: >= {tp_threshold:.2f}). Closing...")
                        self.contracts[cid]['is_closing'] = True
                        self._close_contract(cid)
                    elif sl_enabled and sl_val > 0 and profit <= sl_threshold:
                        self.log(f"SL reached for {symbol} ({cid}): {profit:.2f} USD (Target: <= {sl_threshold:.2f}). Closing...")
                        self.contracts[cid]['is_closing'] = True
                        self._close_contract(cid)

            self._update_aggregated_positions()
            self._emit_updates()
        except Exception as e:
            self.log(f"Error handling contract update: {e}", 'error')

    def _calculate_target_prices(self, cid):
        c = self.contracts[cid]
        entry = c['entry_price']
        if not entry: return

        tp_val = self.config.get('tp_value', 0)
        sl_val = self.config.get('sl_value', 0)
        use_fixed = self.config.get('use_fixed_balance', True)
        stake = c['stake']
        multiplier = c.get('multiplier')
        side = c['side'] # 'long' or 'short'

        if not (tp_val > 0 or sl_val > 0): return

        # Calculate threshold in USD
        tp_usd = tp_val if use_fixed else (stake * tp_val / 100.0)
        sl_usd = sl_val if use_fixed else (stake * sl_val / 100.0)

        if multiplier:
            # Check if Strategy 5 generated specific levels
            metrics = self.screener_data.get(c['symbol'])
            strat_key = self.config.get('active_strategy')

            if strat_key == 'strategy_5' and metrics:
                tp_pips = metrics.get('tp_pips', 0)
                sl_pips = metrics.get('sl_pips', 0)
                if side == 'long':
                    self.contracts[cid]['tp_price'] = entry + tp_pips
                    self.contracts[cid]['sl_price'] = entry - sl_pips
                else:
                    self.contracts[cid]['tp_price'] = entry - tp_pips
                    self.contracts[cid]['sl_price'] = entry + sl_pips
            else:
                # Fallback to fixed USD/percentage TP/SL
                # Profit = (Price - Entry) / Entry * Multiplier * Stake
                # Price = Entry * (1 + Profit / (Multiplier * Stake))
                denom = multiplier * stake
                if denom == 0: return

                if side == 'long':
                    if tp_val > 0: self.contracts[cid]['tp_price'] = entry * (1 + tp_usd / denom)
                    if sl_val > 0: self.contracts[cid]['sl_price'] = entry * (1 - sl_usd / denom)
                else:
                    if tp_val > 0: self.contracts[cid]['tp_price'] = entry * (1 - tp_usd / denom)
                    if sl_val > 0: self.contracts[cid]['sl_price'] = entry * (1 + sl_usd / denom)
        else:
            # For Rise & Fall, price-based TP/SL is an approximation
            # We'll use a 0.5% move as a default "unit" if no other info, but that's arbitrary.
            # Better: if it's Rise & Fall, we mostly rely on the 'profit' field monitoring
            # which we already do. But let's set a wide price trigger as a safety.
            # Assume 1% move corresponds to a significant win/loss for binary.
            if side == 'long':
                if tp_val > 0: self.contracts[cid]['tp_price'] = entry * 1.01
                if sl_val > 0: self.contracts[cid]['sl_price'] = entry * 0.99
            else:
                if tp_val > 0: self.contracts[cid]['tp_price'] = entry * 0.99
                if sl_val > 0: self.contracts[cid]['sl_price'] = entry * 1.01

    def _update_aggregated_positions(self):
        # Update UI compatibility fields
        self.in_position = {'long': False, 'short': False}
        self.position_entry_price = {'long': 0.0, 'short': 0.0}
        self.position_qty = {'long': 0.0, 'short': 0.0}

        for c in self.contracts.values():
            side = c['side'] # 'long' or 'short'
            if side in self.in_position:
                self.in_position[side] = True
                # For simplicity, if multiple symbols, we show the first one's price/qty or avg
                if self.position_entry_price[side] == 0:
                    self.position_entry_price[side] = c['entry_price'] or 0.0
                    self.position_qty[side] = c['stake'] or 0.0

    def _emit_updates(self):
        self.open_trades = []
        floating_pnl = 0.0
        used_notional = 0.0
        for cid, c in self.contracts.items():
            self.open_trades.append({
                'id': cid, 'type': c['side'].capitalize(), 'symbol': c['symbol'],
                'entry_spot_price': c['entry_price'], 'stake': c['stake'], 'pnl': c['pnl'],
                'expiry_time': c['expiry_time'],
                'status': c.get('status', 'Holding')
            })
            floating_pnl += c['pnl']
            used_notional += c['stake']

        self.net_profit = floating_pnl + self.net_trade_profit
        self.used_amount_notional = used_notional
        self.cached_pos_notional = used_notional

        win_rate = 0.0
        if self.total_trades_count > 0:
            win_rate = (self.wins_count / self.total_trades_count) * 100

        avg_pnl = 0.0
        if self.total_trades_count > 0:
            avg_pnl = self.net_trade_profit / self.total_trades_count

        payload = {
            'running': self.is_running,
            'is_demo': self.config.get('is_demo', True),
            'total_balance': self.account_balance,
            'available_balance': self.available_balance,
            'open_trades': self.open_trades,
            'net_profit': self.net_profit,
            'total_trades': self.total_trades_count + len(self.open_trades),
            'win_rate': round(win_rate, 1),
            'avg_pnl': round(avg_pnl, 2),
            'total_capital': self.total_equity,
            'total_capital_2nd': self.total_capital_2nd,
            'used_amount': self.used_amount_notional,
            'remaining_amount': self.remaining_amount_notional,
            'max_allowed_used_display': self.max_allowed_display,
            'max_amount_display': self.max_amount_display,
            'used_fees': self.used_fees,
            'size_fees': self.size_fees,
            'net_trade_profit': self.net_trade_profit,
            'total_trade_profit': self.total_trade_profit,
            'total_trade_loss': self.total_trade_loss,
            'in_position': self.in_position,
            'position_entry_price': self.position_entry_price
        }
        self.emit('account_update', payload)
        self.emit('trades_update', {'trades': self.open_trades})

    def start(self, passive_monitoring=False):
        self.is_running = not passive_monitoring
        self.log(f"Bot started | Trading: {'ON' if self.is_running else 'OFF'}")

        if not self.ws_thread or not self.ws_thread.is_alive():
            self.stop_event.clear()
            self.ws_thread = threading.Thread(target=self._run_ws, daemon=True)
            self.ws_thread.start()
        elif self.is_running and self.ws and self.ws.sock and self.ws.sock.connected:
            # Already connected but just started trading, trigger subscriptions
            self.log("Already connected, triggering trading subscriptions...")
            strat_key = self.config.get('active_strategy', 'strategy_1')
            strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])
            for symbol in self.config.get('symbols', []):
                self._init_symbol_data(symbol)
                self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                self._fetch_history(self.ws, symbol, strat['ltf_granularity'], 100)
                self._fetch_history(self.ws, symbol, strat['htf_granularity'], 2)

    def _run_ws(self):
        while not self.stop_event.is_set():
            # Connect if we have a token, to allow balance monitoring
            if not self.config.get('deriv_api_token'):
                time.sleep(2)
                continue

            try:
                self.ws = websocket.WebSocketApp(
                    self._get_ws_url(),
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=lambda ws, err: self.log(f"WS Error: {err}", 'error'),
                    on_close=lambda ws, code, msg: self.log("WS Connection Closed")
                )
                self.ws.run_forever()
            except Exception as e:
                self.log(f"WS Exception: {e}", 'error')
            if not self.stop_event.is_set():
                time.sleep(5)

    def stop(self):
        self.is_running = False
        self.log("Bot trading paused")

        # Unsubscribe from ticks to save resources
        self.log("Unsubscribing from ticks to save resources (Passive Monitoring active).")
        if self.ws and self.ws.sock and self.ws.sock.connected:
            with self.data_lock:
                for sym, sd in self.symbol_data.items():
                    if sd.get('subscription_id'):
                        self.ws.send(json.dumps({"forget": sd['subscription_id']}))
                        sd['subscription_id'] = None

    def stop_bot(self):
        self.stop_event.set()
        if self.ws:
            self.ws.close()
        self.log("Bot engine shut down")

    def check_credentials(self):
        if not self.config.get('deriv_api_token'):
            return False, "API Token missing"
        return True, "API Token present"

    def test_api_credentials(self):
        token = self.config.get('deriv_api_token')
        if not token: return False
        try:
            ws = websocket.create_connection(self._get_ws_url(), timeout=10)
            ws.send(json.dumps({"authorize": token}))
            res = json.loads(ws.recv())
            ws.close()
            return 'authorize' in res and 'error' not in res
        except: return False

    def apply_live_config_update(self, new_config):
        old_symbols = set(self.config.get('symbols', []))
        new_symbols = set(new_config.get('symbols', []))

        old_token = self.config.get('deriv_api_token')
        new_token = new_config.get('deriv_api_token')

        old_strat = self.config.get('active_strategy', 'strategy_1')
        new_strat = new_config.get('active_strategy', 'strategy_1')

        self.config = new_config
        self.log("Config applied live")

        # If token changed, we need a full reconnect
        if old_token != new_token:
            self._apply_api_credentials()
            return {"success": True}

        # If strategy changed, reset all symbol data to re-fetch with new granularities
        if old_strat != new_strat:
            self.log(f"Strategy changed to {new_strat}. Resetting data...")
            if new_strat == 'strategy_5':
                # Immediately calculate if data exists
                for sym in self.symbol_data:
                    self._update_screener(sym)
            with self.data_lock:
                # Keep subscription ids but clear candles/opens
                for sym in self.symbol_data:
                    sub_id = self.symbol_data[sym].get('subscription_id')
                    self._init_symbol_data(sym)
                    self.symbol_data[sym]['subscription_id'] = sub_id

            if self.ws and self.ws.sock and self.ws.sock.connected:
                strat = self.STRATEGY_MAP.get(new_strat, self.STRATEGY_MAP['strategy_1'])
                h_count = 200 if new_strat in ['strategy_4', 'strategy_5'] else 2
                for sym in new_symbols:
                    self._fetch_history(self.ws, sym, strat['ltf_granularity'], 100)
                    self._fetch_history(self.ws, sym, strat['htf_granularity'], h_count)
                    if new_strat == 'strategy_5':
                        self._fetch_history(self.ws, sym, 60, 100) # 1m
                        self._fetch_history(self.ws, sym, 300, 100) # 5m
                        self._fetch_history(self.ws, sym, 900, 100) # 15m
                        self._fetch_history(self.ws, sym, 3600, 200) # 1h
                        self._fetch_history(self.ws, sym, 14400, 100) # 4h
                        self._fetch_history(self.ws, sym, 86400, 50) # Daily data
                        self.ws.send(json.dumps({"contracts_for": sym}))
            return {"success": True}

        # If only symbols changed and we are connected
        if self.ws and self.ws.sock and self.ws.sock.connected:
            strat = self.STRATEGY_MAP.get(new_strat, self.STRATEGY_MAP['strategy_1'])
            h_count = 200 if new_strat in ['strategy_4', 'strategy_5'] else 2
            added_symbols = new_symbols - old_symbols
            for symbol in added_symbols:
                self.log(f"Subscribing to new symbol: {symbol}")
                self._init_symbol_data(symbol)
                self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                self._fetch_history(self.ws, symbol, strat['ltf_granularity'], 100)
                self._fetch_history(self.ws, symbol, strat['htf_granularity'], h_count)
                if new_strat == 'strategy_5':
                    self._fetch_history(self.ws, symbol, 60, 100) # 1m
                    self._fetch_history(self.ws, symbol, 300, 100) # 5m
                    self._fetch_history(self.ws, symbol, 900, 100) # 15m
                    self._fetch_history(self.ws, symbol, 3600, 200) # 1h
                    self._fetch_history(self.ws, symbol, 14400, 100) # 4h
                    self._fetch_history(self.ws, symbol, 86400, 50) # Daily data
                    self.ws.send(json.dumps({"contracts_for": symbol}))

            removed_symbols = old_symbols - new_symbols
            for symbol in removed_symbols:
                sd = self.symbol_data.get(symbol)
                if sd and sd.get('subscription_id'):
                    self.log(f"Unsubscribing from symbol: {symbol}")
                    self.ws.send(json.dumps({"forget": sd['subscription_id']}))
                with self.data_lock:
                    if symbol in self.symbol_data:
                        del self.symbol_data[symbol]

        return {"success": True}

    def _apply_api_credentials(self):
        self.log("Applying new API credentials, reconnecting...")
        if self.ws:
            self.ws.close()
            # The run_forever loop in _run_ws will handle reconnection

    def fetch_account_data_sync(self):
        self._emit_updates()

    def batch_modify_tpsl(self): pass

    def batch_cancel_orders(self):
        self.log("Cancelling all open trades...")
        with self.data_lock:
            for cid in list(self.contracts.keys()):
                self._close_contract(cid)

    def emergency_sl(self):
        self.batch_cancel_orders()
