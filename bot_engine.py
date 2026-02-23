import json
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from collections import deque
import websocket
import pandas as pd
import numpy as np
import ta
from deriv_ta import DerivTA, Interval

class TradingBotEngine:
    STRATEGY_MAP = {
        'strategy_1': {
            'name': 'Slow (Daily / 1h)',
            'htf_granularity': 86400, # Daily
            'ltf_granularity': 3600,  # 1h (Hardcoded per req)
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
            'name': 'Intelligence Screener v2.0',
            'htf_granularity': 3600,  # 1h
            'ltf_granularity': 60,    # 1m
            'bias_granularity': 900,   # 15m
            'm5_granularity': 300,
            'daily_granularity': 86400,
            'expiry_type': 'dynamic'
        },
        'strategy_6': {
            'name': 'Intelligence Legacy v1.0',
            'htf_granularity': 3600,  # 1h for Intelligence Core
            'ltf_granularity': 60,    # 1m for Timing
            'bias_granularity': 14400, # 4h for bias
            'expiry_type': 'dynamic'
        },
        'strategy_7': {
            'name': 'Intelligent Multi-TF Alignment',
            'expiry_type': 'dynamic',
            'ltf_granularity': 60, # Default trigger on 1m
            'htf_granularity': 3600 # Placeholder to prevent KeyErrors
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
        self.screener_thread = None
        self.strat7_cache = {} # Symbol -> { 'small': Analysis, 'mid': Analysis, 'high': Analysis, 'timestamp': float }

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
        self.symbol_streaks = {} # Symbol -> current consecutive losses
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
                    time.sleep(0.5)

                    if strat_key == 'strategy_7': continue

                    self._fetch_history(ws, symbol, strat['ltf_granularity'], 100)
                    time.sleep(0.5)
                    h_count = 200 if strat_key in ['strategy_4', 'strategy_5', 'strategy_6'] else 2
                    self._fetch_history(ws, symbol, strat['htf_granularity'], h_count)
                    time.sleep(0.5)

                    # Enhanced History for Strategies 1 & 2 (Bias filters)
                    if strat_key in ['strategy_1', 'strategy_2']:
                        self._fetch_history(ws, symbol, 14400, 100) # 4H
                        time.sleep(0.5)

                    if strat_key == 'strategy_5':
                        for g, c in [(60, 100), (300, 100), (900, 200), (3600, 200), (86400, 50)]:
                            self._fetch_history(ws, symbol, g, c)
                            time.sleep(0.5)
                        ws.send(json.dumps({"contracts_for": symbol}))
                    elif strat_key == 'strategy_6':
                        for g, c in [(60, 100), (900, 200), (3600, 200), (86400, 50)]:
                            self._fetch_history(ws, symbol, g, c)
                            time.sleep(0.5)
                        ws.send(json.dumps({"contracts_for": symbol}))
                    time.sleep(0.5)

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
                'snr_zones': [], # List of { 'price': level, 'type': 'S'|'R'|'Flip', 'touches': n }
                'consecutive_wins': 0,
                'consecutive_losses': 0,
                'daily_crosses': 0,
                'last_cross_side': None, # 'above' or 'below'
                'hourly_trade_count': 0,
                'last_trade_hour': None,
                'h4_candles': [],
                'm3_candles': [],
                'atr_1m_history': deque(maxlen=50)
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
        htf_gran = strat.get('htf_granularity')
        ltf_gran = strat.get('ltf_granularity')

        with self.data_lock:
            if symbol not in self.symbol_data: return
            sd = self.symbol_data[symbol]

            if granularity == 60:
                if len(candles) > 1: sd['ltf_candles'] = candles
                else:
                    sd['ltf_candles'].append(candles[0])
                    if len(sd['ltf_candles']) > 100: sd['ltf_candles'].pop(0)
            if granularity == 180: # 3m
                if len(candles) > 1: sd['m3_candles'] = candles
                else:
                    sd['m3_candles'].append(candles[0])
                    if len(sd['m3_candles']) > 100: sd['m3_candles'].pop(0)
            if granularity == 300:
                if len(candles) > 1: sd['m5_candles'] = candles
                else:
                    sd['m5_candles'].append(candles[0])
                    if len(sd['m5_candles']) > 100: sd['m5_candles'].pop(0)
            if granularity == 900:
                if len(candles) > 1: sd['m15_candles'] = candles
                else:
                    sd['m15_candles'].append(candles[0])
                    if len(sd['m15_candles']) > 200: sd['m15_candles'].pop(0)
                if strat_key == 'strategy_5':
                    self._calculate_snr_zones(symbol, 900) # 15m SNR
            if granularity == 3600:
                if len(candles) > 1: sd['htf_candles'] = candles
                else:
                    sd['htf_candles'].append(candles[0])
                    if len(sd['htf_candles']) > 200: sd['htf_candles'].pop(0)
            if granularity == 14400:
                if len(candles) > 1:
                    sd['bias_candles'] = candles
                    sd['h4_candles'] = candles
                else:
                    sd['bias_candles'].append(candles[0])
                    sd['h4_candles'].append(candles[0])
                    if len(sd['bias_candles']) > 100: sd['bias_candles'].pop(0)
                    if len(sd['h4_candles']) > 100: sd['h4_candles'].pop(0)

            if granularity == strat.get('bias_granularity'):
                if candles:
                    sd['current_bias_candle'] = candles[-1]

            if granularity == 86400:
                sd['daily_candles'] = candles

            if htf_gran and granularity == htf_gran:
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
                    self._calculate_snr_zones(symbol, 3600) # 1H SNR
                elif strat_key == 'strategy_6':
                    sd['htf_candles'] = candles
                    self._calculate_snr_zones(symbol, 3600) # 1H SNR

            elif ltf_gran and granularity == ltf_gran:
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

    def _calculate_supertrend(self, df, period=10, multiplier=3):
        atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=period)
        hl2 = (df['high'] + df['low']) / 2
        upperband = hl2 + (multiplier * atr)
        lowerband = hl2 - (multiplier * atr)

        final_upperband = upperband.copy()
        final_lowerband = lowerband.copy()

        for i in range(1, len(df)):
            if upperband.iloc[i] < final_upperband.iloc[i-1] or df['close'].iloc[i-1] > final_upperband.iloc[i-1]:
                final_upperband.iloc[i] = upperband.iloc[i]
            else:
                final_upperband.iloc[i] = final_upperband.iloc[i-1]

            if lowerband.iloc[i] > final_lowerband.iloc[i-1] or df['close'].iloc[i-1] < final_lowerband.iloc[i-1]:
                final_lowerband.iloc[i] = lowerband.iloc[i]
            else:
                final_lowerband.iloc[i] = final_lowerband.iloc[i-1]

        supertrend = [0.0] * len(df)
        direction = [1] * len(df) # 1 for up, -1 for down

        for i in range(1, len(df)):
            if i == 1:
                supertrend[i] = final_upperband.iloc[i]
                direction[i] = -1
                continue
            if supertrend[i-1] == final_upperband.iloc[i-1]:
                if df['close'].iloc[i] > final_upperband.iloc[i]:
                    supertrend[i] = final_lowerband.iloc[i]
                    direction[i] = 1
                else:
                    supertrend[i] = final_upperband.iloc[i]
                    direction[i] = -1
            else:
                if df['close'].iloc[i] < final_lowerband.iloc[i]:
                    supertrend[i] = final_upperband.iloc[i]
                    direction[i] = -1
                else:
                    supertrend[i] = final_lowerband.iloc[i]
                    direction[i] = 1
        return pd.Series(supertrend), pd.Series(direction)

    def _calculate_fractals(self, df, window=2):
        """Identify Swing Highs and Lows (Fractals)."""
        if len(df) < 2 * window + 1: return pd.Series([False]*len(df)), pd.Series([False]*len(df))

        highs = df['high']
        lows = df['low']

        is_high = [False] * len(df)
        is_low = [False] * len(df)

        for i in range(window, len(df) - window):
            # Swing High
            if all(highs.iloc[i] > highs.iloc[i-window:i]) and all(highs.iloc[i] > highs.iloc[i+1:i+window+1]):
                is_high[i] = True
            # Swing Low
            if all(lows.iloc[i] < lows.iloc[i-window:i]) and all(lows.iloc[i] < lows.iloc[i+1:i+window+1]):
                is_low[i] = True

        return pd.Series(is_high, index=df.index), pd.Series(is_low, index=df.index)

    def _calculate_order_blocks(self, df, lookback=100):
        """Identify Order Blocks: Last opposite candle before a strong impulsive move."""
        if len(df) < lookback: return []

        obs = []
        for i in range(len(df) - 5, 5, -1):
            if i < 10: break

            # Simple impulse check: body size > 2x average of previous 10
            avg_body = abs(df['close'].iloc[i-10:i] - df['open'].iloc[i-10:i]).mean()
            body = abs(df['close'].iloc[i] - df['open'].iloc[i])

            if body > 2 * avg_body:
                is_bullish_impulse = df['close'].iloc[i] > df['open'].iloc[i]
                # Find last opposite candle
                for j in range(i-1, i-6, -1):
                    if is_bullish_impulse and df['close'].iloc[j] < df['open'].iloc[j]:
                        obs.append({'price': df['low'].iloc[j], 'high': df['high'].iloc[j], 'type': 'Bullish OB', 'epoch': df['epoch'].iloc[j]})
                        break
                    elif not is_bullish_impulse and df['close'].iloc[j] > df['open'].iloc[j]:
                        obs.append({'price': df['high'].iloc[j], 'low': df['low'].iloc[j], 'type': 'Bearish OB', 'epoch': df['epoch'].iloc[j]})
                        break
            if len(obs) >= 5: break
        return obs

    def _calculate_fvg(self, df, lookback=50):
        """Identify Fair Value Gaps (FVG): Imbalance between 3 candles."""
        if len(df) < 3: return []

        fvgs = []
        for i in range(len(df) - 1, len(df) - lookback, -1):
            if i < 2: break

            # Bullish FVG: High of candle 1 < Low of candle 3
            if df['high'].iloc[i-2] < df['low'].iloc[i]:
                fvgs.append({
                    'top': df['low'].iloc[i],
                    'bottom': df['high'].iloc[i-2],
                    'type': 'Bullish FVG',
                    'epoch': df['epoch'].iloc[i-1]
                })
            # Bearish FVG: Low of candle 1 > High of candle 3
            elif df['low'].iloc[i-2] > df['high'].iloc[i]:
                fvgs.append({
                    'top': df['low'].iloc[i-2],
                    'bottom': df['high'].iloc[i],
                    'type': 'Bearish FVG',
                    'epoch': df['epoch'].iloc[i-1]
                })
            if len(fvgs) >= 10: break
        return fvgs

    def _detect_macd_divergence(self, df, window=20):
        if len(df) < window + 10: return 0 # No signal

        macd_ind = ta.trend.MACD(df['close'])
        macd = macd_ind.macd()

        # Bullish Divergence: Price Lower Low, MACD Higher Low
        p_idx = df['close'].iloc[-window:].idxmin()
        m_idx = macd.iloc[-window:].idxmin()

        # Check previous low
        p_prev_low = df['close'].iloc[-2*window:-window].min()
        m_prev_low = macd.iloc[-2*window:-window].min()

        if df['close'].iloc[-1] < p_prev_low and macd.iloc[-1] > m_prev_low:
            return 1 # Bullish Divergence

        # Bearish Divergence: Price Higher High, MACD Lower High
        p_idx_h = df['close'].iloc[-window:].idxmax()
        m_idx_h = macd.iloc[-window:].idxmax()

        p_prev_high = df['close'].iloc[-2*window:-window].max()
        m_prev_high = macd.iloc[-2*window:-window].max()

        if df['close'].iloc[-1] > p_prev_high and macd.iloc[-1] < m_prev_high:
            return -1 # Bearish Divergence

        return 0

    def _update_screener(self, symbol):
        strat_key = self.config.get('active_strategy', 'strategy_1')
        if strat_key == 'strategy_6':
            return self._update_screener_v1(symbol)

        with self.data_lock:
            sd = self.symbol_data.get(symbol)
            if not sd: return

            # Create copies for thread-safe processing
            m5_candles = list(sd.get('m5_candles', []))
            m15_candles = list(sd.get('m15_candles', []))
            htf_candles = list(sd.get('htf_candles', []))
            ltf_candles = list(sd.get('ltf_candles', []))
            daily_candles = list(sd.get('daily_candles', []))
            snr_zones = list(sd.get('snr_zones', []))

        contract_type = self.config.get('contract_type', 'rise_fall')
        is_multiplier = (contract_type == 'multiplier')

        # Select Base Dataframe
        df_core = None
        if is_multiplier:
            if len(htf_candles) < 100: return
            df_core = pd.DataFrame(htf_candles)
            # Calculate 1H Order Blocks & FVGs
            obs = self._calculate_order_blocks(df_core)
            fvgs = self._calculate_fvg(df_core)
            with self.data_lock:
                sd['order_blocks'] = obs
                sd['fvgs'] = fvgs
        else:
            if len(m5_candles) < 100: return
            df_core = pd.DataFrame(m5_candles)
            # Calculate 5m Fractals
            f_high, f_low = self._calculate_fractals(df_core)
            with self.data_lock:
                sd['fractal_highs'] = df_core['high'][f_high].tolist()
                sd['fractal_lows'] = df_core['low'][f_low].tolist()

        last_close = df_core['close'].iloc[-1]

        # --- 0. SESSION & INSTRUMENT CONTEXT ---
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        # Dead Hours: 22:00–06:00 UTC
        is_dead_hours = (hour >= 22 or hour < 6)
        session_threshold_bonus = 5 if is_dead_hours else 0

        # --- 1. TREND BLOCK ---
        # EMA 50/200, SuperTrend, ADX
        t_pos, t_neg = 0, 0
        ema50 = ta.trend.EMAIndicator(df_core['close'], window=50).ema_indicator().iloc[-1]
        ema200 = ta.trend.EMAIndicator(df_core['close'], window=200).ema_indicator().iloc[-1]

        if last_close > ema50: t_pos += 1
        else: t_neg += 1
        if ema50 > ema200: t_pos += 1
        else: t_neg += 1

        st, st_dir = self._calculate_supertrend(df_core)
        if st_dir.iloc[-1] == 1: t_pos += 2
        else: t_neg += 2

        adx_val = ta.trend.ADXIndicator(df_core['high'], df_core['low'], df_core['close']).adx().iloc[-1]
        if adx_val > 25:
            if last_close > ema50: t_pos += 1
            else: t_neg += 1

        trend_score = (t_pos - t_neg) / (t_pos + t_neg) if (t_pos + t_neg) > 0 else 0

        # --- 2. MOMENTUM BLOCK ---
        # RSI, Stoch RSI, MACD Divergence
        m_pos, m_neg = 0, 0
        rsi = ta.momentum.RSIIndicator(df_core['close']).rsi().iloc[-1]
        if rsi > 50: m_pos += 1
        else: m_neg += 1

        stoch_rsi_ind = ta.momentum.StochRSIIndicator(df_core['close'])
        srsi_k = stoch_rsi_ind.stochrsi_k().iloc[-1]
        srsi_d = stoch_rsi_ind.stochrsi_d().iloc[-1]
        if srsi_k > 0.5: m_pos += 1
        else: m_neg += 1
        if srsi_k > srsi_d: m_pos += 1
        else: m_neg += 1

        div = self._detect_macd_divergence(df_core)
        if div == 1: m_pos += 2
        elif div == -1: m_neg += 2

        mom_score = (m_pos - m_neg) / (m_pos + m_neg) if (m_pos + m_neg) > 0 else 0

        # --- 3. VOLATILITY BLOCK ---
        # ATR, Bollinger Bands
        v_pos, v_neg = 0, 0
        bb = ta.volatility.BollingerBands(df_core['close'])
        if last_close > bb.bollinger_mavg().iloc[-1]: v_pos += 1
        else: v_neg += 1

        # Band walk/breakout
        if last_close > bb.bollinger_hband().iloc[-1]: v_pos += 1
        elif last_close < bb.bollinger_lband().iloc[-1]: v_neg += 1

        vol_score = (v_pos - v_neg) / (v_pos + v_neg) if (v_pos + v_neg) > 0 else 0

        # --- 4. STRUCTURE BLOCK (v4.0 Enhanced) ---
        s_pos, s_neg = 0, 0
        dist = (last_close - ema50) / ema50
        if abs(dist) < 0.05: s_pos += 1
        else: s_neg += 1 # Overextended

        if is_multiplier:
            # Multiplier: Order Block & FVG Alignment
            obs = sd.get('order_blocks', [])
            fvgs = sd.get('fvgs', [])

            # Tiered Structure Mapping:
            # 1. FVG + OB Overlap = Highest Priority (+5)
            # 2. OB Only = High Priority (+3)
            # 3. FVG Only = Minor Bonus (+1)

            ob_hit = None
            for ob in obs:
                if abs(last_close - ob['price']) / ob['price'] < 0.005:
                    ob_hit = ob
                    break

            fvg_hit = None
            for fvg in fvgs:
                if last_close >= fvg['bottom'] and last_close <= fvg['top']:
                    fvg_hit = fvg
                    break

            if ob_hit and fvg_hit:
                if ob_hit['type'].startswith('Bullish') and fvg_hit['type'].startswith('Bullish'): s_pos += 5
                elif ob_hit['type'].startswith('Bearish') and fvg_hit['type'].startswith('Bearish'): s_neg += 5
            elif ob_hit:
                if ob_hit['type'].startswith('Bullish'): s_pos += 3
                else: s_neg += 3
            elif fvg_hit:
                if fvg_hit['type'].startswith('Bullish'): s_pos += 1
                else: s_neg += 1
        else:
            # Rise & Fall: Fractal Retests
            f_highs = sd.get('fractal_highs', [])[-5:]
            f_lows = sd.get('fractal_lows', [])[-5:]
            for fh in f_highs:
                if abs(last_close - fh) / fh < 0.002: s_neg += 3 # Resistance retest
            for fl in f_lows:
                if abs(last_close - fl) / fl < 0.002: s_pos += 3 # Support retest

        # HTF SNR Alignment
        for z in snr_zones:
            if abs(last_close - z['price']) / z['price'] < 0.005:
                if z['type'] in ['S', 'Flip']: s_pos += 2
                elif z['type'] in ['R', 'Flip']: s_neg += 2

        struct_score = (s_pos - s_neg) / (s_pos + s_neg) if (s_pos + s_neg) > 0 else 0

        # --- FINAL CONFIDENCE & WEIGHTING (v4.0 Regime Switch) ---
        if adx_val > 25:
            # Trending Regime: 80% Weight to Trend/Volatility, Disable Oscillators
            regime_type = "Trending"
            # Trend(40) + Vol(40) + Struct(20)
            confidence = (trend_score * 40) + (vol_score * 40) + (struct_score * 20)
        elif adx_val < 20:
            # Ranging Regime: 80% Weight to Momentum/Structure
            regime_type = "Ranging"
            # Mom(40) + Struct(40) + Vol(20)
            confidence = (mom_score * 40) + (struct_score * 40) + (vol_score * 20)
        else:
            # Transition/Mixed
            regime_type = "Mixed"
            if is_multiplier:
                confidence = (trend_score * 40) + (vol_score * 30) + (struct_score * 20) + (mom_score * 10)
            else:
                confidence = (struct_score * 35) + (mom_score * 35) + (vol_score * 20) + (trend_score * 10)

        # Multiplier / Expiry Logic
        atr_val = ta.volatility.AverageTrueRange(df_core['high'], df_core['low'], df_core['close']).average_true_range().iloc[-1]

        # 1m ATR for Volatility Freeze
        atr_1m = 0
        if ltf_candles:
            df_1m = pd.DataFrame(ltf_candles)
            if len(df_1m) >= 14:
                atr_1m = ta.volatility.AverageTrueRange(df_1m['high'], df_1m['low'], df_1m['close']).average_true_range().iloc[-1]

        suggested_multiplier = 10
        if is_multiplier:
            # v2.1 CORRECTED ATR Logic:
            rel_atr = atr_val / last_close
            if rel_atr >= 0.008 and adx_val > 30:
                suggested_multiplier = 50
            elif rel_atr >= 0.005 and adx_val > 25:
                suggested_multiplier = 20
            elif rel_atr >= 0.003 and adx_val > 20:
                suggested_multiplier = 10
            else:
                suggested_multiplier = 5

            # v4.0 Session Filter: Cap Multiplier at 10x during Dead Hours
            if is_dead_hours:
                suggested_multiplier = min(suggested_multiplier, 10)

        suggested_expiry = 5
        if abs(confidence) > 75: suggested_expiry = 15
        elif abs(confidence) > 60: suggested_expiry = 10

        # Adaptive Threshold Adjustment (v4.0 Streak Tracking)
        streak = sd.get('consecutive_losses', 0)
        base_threshold = 72 if not is_multiplier else 68
        adaptive_threshold = base_threshold + session_threshold_bonus
        if streak >= 3:
            adaptive_threshold += 10

        # 1m ATR for Volatility Freeze (v4.0 Instrument Specific)
        atr_1m = 0
        atr_24h = 0
        if ltf_candles:
            df_1m = pd.DataFrame(ltf_candles)
            if len(df_1m) >= 14:
                atr_1m = ta.volatility.AverageTrueRange(df_1m['high'], df_1m['low'], df_1m['close']).average_true_range().iloc[-1]

        # Calculate Baseline ATR from 1H candles (24 periods)
        if len(htf_candles) >= 24:
            df_24h = pd.DataFrame(htf_candles[-24:])
            atr_24h = ta.volatility.AverageTrueRange(df_24h['high'], df_24h['low'], df_24h['close']).average_true_range().iloc[-1]

        self.screener_data[symbol] = {
            'confidence': round(confidence, 1),
            'threshold': adaptive_threshold,
            'streak': streak,
            'direction': 'CALL' if confidence > 0 else 'PUT',
            'regime': regime_type,
            'trend': round(trend_score, 1),
            'momentum': round(mom_score, 1),
            'volatility': round(vol_score, 1),
            'structure': round(struct_score, 1),
            'adx': round(adx_val, 1),
            'srsi_k': round(srsi_k, 4),
            'atr': round(atr_val, 4),
            'atr_1m': round(atr_1m, 6),
            'atr_24h': round(atr_24h, 6),
            'is_dead_hours': is_dead_hours,
            'expiry_min': suggested_expiry,
            'multiplier': suggested_multiplier,
            'st_dir': st_dir.iloc[-1],
            'last_update': time.time()
        }

        self.emit('screener_update', {'symbol': symbol, 'data': self.screener_data[symbol]})

    def _update_screener_v1(self, symbol):
        with self.data_lock:
            sd = self.symbol_data.get(symbol)
            if not sd: return
            htf_candles = list(sd.get('htf_candles', [])) # 1H Macro Bias
            m15_candles = list(sd.get('m15_candles', [])) # 15m Trend
            daily_candles = list(sd.get('daily_candles', []))
            snr_zones = list(sd.get('snr_zones', []))

        if len(m15_candles) < 200: return

        df_h = pd.DataFrame(m15_candles) # Using 15m for Core Analysis smoothing
        df_macro = pd.DataFrame(htf_candles) # 1H for Bias
        last_close = df_h['close'].iloc[-1]

        # --- v4.0 Dimensionality Reduction: Requires at least ONE from each category ---

        # --- A) TREND BLOCK (Weight 3) ---
        t_signals = []
        ema50 = ta.trend.EMAIndicator(df_h['close'], window=50).ema_indicator().iloc[-1]
        ema200 = ta.trend.EMAIndicator(df_h['close'], window=200).ema_indicator().iloc[-1]
        sma20 = ta.trend.SMAIndicator(df_h['close'], window=20).sma_indicator().iloc[-1]

        if last_close > ema50: t_signals.append(1)
        else: t_signals.append(-1)
        if ema50 > ema200: t_signals.append(1)
        else: t_signals.append(-1)
        if last_close > sma20: t_signals.append(1)
        else: t_signals.append(-1)

        adx_ind = ta.trend.ADXIndicator(df_h['high'], df_h['low'], df_h['close'])
        adx = adx_ind.adx().iloc[-1]
        if adx > 25:
            if last_close > ema50: t_signals.append(1)
            else: t_signals.append(-1)

        ichimoku = ta.trend.IchimokuIndicator(df_h['high'], df_h['low'])
        span_a = ichimoku.ichimoku_a().iloc[-1]
        span_b = ichimoku.ichimoku_b().iloc[-1]
        if last_close > span_a and last_close > span_b: t_signals.append(1)
        elif last_close < span_a and last_close < span_b: t_signals.append(-1)

        macd_ind = ta.trend.MACD(df_h['close'])
        if macd_ind.macd().iloc[-1] > macd_ind.macd_signal().iloc[-1]: t_signals.append(1)
        else: t_signals.append(-1)

        trend_sentiment = sum(t_signals) / len(t_signals) if t_signals else 0
        trend_score = trend_sentiment * 3

        # --- B) MOMENTUM BLOCK (Weight 2) ---
        m_signals = []
        rsi = ta.momentum.RSIIndicator(df_h['close']).rsi().iloc[-1]
        if rsi > 50: m_signals.append(1)
        else: m_signals.append(-1)

        stoch_rsi = ta.momentum.StochRSIIndicator(df_h['close']).stochrsi_k().iloc[-1]
        if stoch_rsi > 0.5: m_signals.append(1)
        else: m_signals.append(-1)

        wr = ta.momentum.WilliamsRIndicator(df_h['high'], df_h['low'], df_h['close']).williams_r().iloc[-1]
        if wr > -50: m_signals.append(1)
        else: m_signals.append(-1)

        roc = ta.momentum.ROCIndicator(df_h['close']).roc().iloc[-1]
        if roc > 0: m_signals.append(1)
        else: m_signals.append(-1)

        cci = ta.trend.CCIIndicator(df_h['high'], df_h['low'], df_h['close']).cci().iloc[-1]
        if cci > 0: m_signals.append(1)
        else: m_signals.append(-1)

        mom_sentiment = sum(m_signals) / len(m_signals) if m_signals else 0
        mom_score = mom_sentiment * 2

        # --- C) VOLATILITY BLOCK (Weight 1) ---
        v_pos, v_neg = 0, 0
        atr_ind = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close'])
        atr = atr_ind.average_true_range().iloc[-1]
        atr_prev = atr_ind.average_true_range().iloc[-2]
        if atr > atr_prev: v_pos += 0.5

        bb = ta.volatility.BollingerBands(df_h['close'])
        bbw = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / bb.bollinger_mavg().iloc[-1]
        prev_bbw = (bb.bollinger_hband().iloc[-2] - bb.bollinger_lband().iloc[-2]) / bb.bollinger_mavg().iloc[-2]
        if bbw > prev_bbw: v_pos += 0.5

        dc = ta.volatility.DonchianChannel(df_h['high'], df_h['low'], df_h['close'])
        dc_mid = (dc.donchian_channel_hband().iloc[-1] + dc.donchian_channel_lband().iloc[-1]) / 2
        if last_close > dc_mid: v_pos += 0.5
        else: v_neg += 0.5

        kc = ta.volatility.KeltnerChannel(df_h['high'], df_h['low'], df_h['close'])
        if last_close > kc.keltner_channel_mband().iloc[-1]: v_pos += 0.5
        else: v_neg += 0.5

        vol_sentiment = (v_pos - v_neg) / (v_pos + v_neg) if (v_pos + v_neg) > 0 else 0
        vol_score = vol_sentiment * 1

        # --- D) STRUCTURE BLOCK (Weight 2) ---
        s_pos, s_neg = 0, 0
        dist = (last_close - ema50) / ema50
        if abs(dist) < 0.05: s_pos += 1
        elif abs(dist) > 0.1: s_neg += 0.5

        sma20_v = df_h['close'].rolling(window=20).mean()
        std20_v = df_h['close'].rolling(window=20).std()
        z_score = (last_close - sma20_v.iloc[-1]) / std20_v.iloc[-1]
        if abs(z_score) < 2: s_pos += 1
        else: s_neg += 1

        if last_close >= bb.bollinger_hband().iloc[-1]: s_neg += 1
        elif last_close <= bb.bollinger_lband().iloc[-1]: s_pos += 1

        if daily_candles and len(daily_candles) >= 2:
            prev_day = daily_candles[-2]
            pivot = (prev_day['high'] + prev_day['low'] + prev_day['close']) / 3
            r1 = 2 * pivot - prev_day['low']
            s1 = 2 * pivot - prev_day['high']
            if last_close > pivot: s_pos += 0.5
            if last_close > r1: s_neg += 0.5
            if last_close < s1: s_pos += 0.5

            day_high = prev_day['high']
            day_low = prev_day['low']
            if abs(last_close - day_high) / day_high < 0.01: s_neg += 0.5
            if abs(last_close - day_low) / day_low < 0.01: s_pos += 0.5

        for z in snr_zones:
            if abs(last_close - z['price']) / z['price'] < 0.005:
                if z['type'] in ['S', 'Flip']: s_pos += 1
                elif z['type'] in ['R', 'Flip']: s_neg += 1

        struct_sentiment = (s_pos - s_neg) / (s_pos + s_neg) if (s_pos + s_neg) > 0 else 0
        struct_score = struct_sentiment * 2

        # Dimensionality Reduction: Requires at least ONE CATEGORY to be strictly directional
        # Ensure Trend and Momentum agree
        if (trend_sentiment > 0 and mom_sentiment < 0) or (trend_sentiment < 0 and mom_sentiment > 0):
            # Divergence between blocks: Reduce confidence
            trend_score *= 0.5
            mom_score *= 0.5

        raw_sum = trend_score + mom_score + vol_score + struct_score
        confidence = (raw_sum / 8.0) * 100

        abs_conf = abs(confidence)
        suggested_expiry = 5
        if abs_conf >= 70: suggested_expiry = 15
        elif abs_conf >= 55: suggested_expiry = 10

        suggested_multiplier = 5
        if abs_conf >= 80: suggested_multiplier = 50
        elif abs_conf >= 65: suggested_multiplier = 20

        # 1m ATR for UI and Volatility Freeze
        atr_1m = 0
        if not df_h.empty:
            atr_1m = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close']).average_true_range().iloc[-1]

        self.screener_data[symbol] = {
            'confidence': round(confidence, 1),
            'direction': 'CALL' if confidence > 0 else 'PUT',
            'regime': "Trending" if adx > 25 else "Ranging",
            'trend': round(trend_score, 1),
            'momentum': round(mom_score, 1),
            'volatility': round(vol_score, 1),
            'structure': round(struct_score, 1),
            'adx': round(adx, 1),
            'atr_1m': round(atr_1m, 6),
            'expiry_min': suggested_expiry,
            'multiplier': suggested_multiplier,
            'last_update': time.time()
        }

        self.emit('screener_update', {'symbol': symbol, 'data': self.screener_data[symbol]})

    def _background_screener_loop(self):
        """Background thread to update screener analysis for Strategies 5, 6, and 7 without blocking main engine."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            while not self.stop_event.is_set():
                strat_key = self.config.get('active_strategy')
                symbols = self.config.get('symbols', [])

                if strat_key == 'strategy_7':
                    for symbol in symbols:
                        if self.stop_event.is_set(): break
                        executor.submit(self._update_strat7_analysis, symbol)
                        time.sleep(2.0) # Increased throttle for Strategy 7 (heavy network)
                elif strat_key in ['strategy_5', 'strategy_6']:
                    for symbol in symbols:
                        if self.stop_event.is_set(): break
                        executor.submit(self._update_screener, symbol)
                        time.sleep(0.5)

                # Dynamic sleep: shorter if we need frequent updates, longer otherwise
                sleep_time = 30 if strat_key == 'strategy_7' else 10
                for _ in range(sleep_time):
                    if self.stop_event.is_set(): break
                    time.sleep(1)

    def _calculate_adr(self, daily_candles, window=14):
        if len(daily_candles) < window: return 0
        ranges = [c['high'] - c['low'] for c in daily_candles[-window:]]
        return sum(ranges) / len(ranges)

    def _update_strat7_analysis(self, symbol):
        tf_small_val = int(self.config.get('strat7_small_tf', 60))
        tf_mid_val = int(self.config.get('strat7_mid_tf', 300))
        tf_high_val = int(self.config.get('strat7_high_tf', 3600))

        def val_to_interval(val):
            for item in Interval:
                if item.value == val: return item
            return Interval.INTERVAL_1_MINUTE

        try:
            h_small = DerivTA(symbol=symbol, interval=val_to_interval(tf_small_val))
            h_mid = DerivTA(symbol=symbol, interval=val_to_interval(tf_mid_val))
            h_high = DerivTA(symbol=symbol, interval=val_to_interval(tf_high_val))

            a_small = h_small.get_analysis()
            a_mid = h_mid.get_analysis()
            a_high = h_high.get_analysis()

            self.strat7_cache[symbol] = {
                'small': a_small,
                'mid': a_mid,
                'high': a_high,
                'timestamp': time.time()
            }

            # v4.0 Pullback Alignment Logic
            # High TF (1H): Bullish/Bearish
            # Mid TF (15m): Bullish/Bearish (Same as High)
            # Small TF (1m): Must be opposite (Pullback)

            rec_small = a_small.summary['RECOMMENDATION']
            rec_mid = a_mid.summary['RECOMMENDATION']
            rec_high = a_high.summary['RECOMMENDATION']

            # ADR Guard
            sd = self.symbol_data.get(symbol, {})
            adr = self._calculate_adr(sd.get('daily_candles', []))
            today_range = 0
            if sd.get('daily_candles'):
                tc = sd['daily_candles'][-1]
                today_range = tc['high'] - tc['low']

            over_adr = adr > 0 and today_range > adr

            # Confidence based on Alignment
            total_buy = a_small.summary['BUY'] + a_mid.summary['BUY'] + a_high.summary['BUY']
            total_sell = a_small.summary['SELL'] + a_mid.summary['SELL'] + a_high.summary['SELL']
            total_signals = total_buy + total_sell + a_small.summary['NEUTRAL'] + a_mid.summary['NEUTRAL'] + a_high.summary['NEUTRAL']
            confidence = ((total_buy - total_sell) / total_signals) * 100 if total_signals > 0 else 0

            df_mid = h_mid.get_dataframe()
            mid_atr = ta.volatility.AverageTrueRange(df_mid['high'], df_mid['low'], df_mid['close']).average_true_range().iloc[-1]

            # Pullback Detection: Small must be opposite to Mid/High
            is_pullback_buy = "BUY" in rec_high and "BUY" in rec_mid and ("SELL" in rec_small or "NEUTRAL" in rec_small)
            is_pullback_sell = "SELL" in rec_high and "SELL" in rec_mid and ("BUY" in rec_small or "NEUTRAL" in rec_small)

            label = "NEUTRAL"
            if is_pullback_buy: label = "PULLBACK_BUY"
            elif is_pullback_sell: label = "PULLBACK_SELL"
            elif "BUY" in rec_high and "BUY" in rec_mid and "BUY" in rec_small: label = "ALIGNED_BUY"
            elif "SELL" in rec_high and "SELL" in rec_mid and "SELL" in rec_small: label = "ALIGNED_SELL"

            self.screener_data[symbol] = {
                'confidence': round(confidence, 1),
                'label': label,
                'direction': 'CALL' if ("BUY" in rec_high) else 'PUT',
                'over_adr': over_adr,
                'regime': a_mid.summary['RECOMMENDATION'],
                'summary_small': a_small.summary['RECOMMENDATION'],
                'summary_mid': a_mid.summary['RECOMMENDATION'],
                'summary_high': a_high.summary['RECOMMENDATION'],
                'atr': round(mid_atr, 4),
                'last_update': time.time()
            }
            self.emit('screener_update', {'symbol': symbol, 'data': self.screener_data[symbol]})

        except Exception as e:
            logging.error(f"Strategy 7 update error for {symbol}: {e}")

    def _process_strategy_7(self, symbol, is_candle_close):
        sd = self.symbol_data[symbol]
        cache = self.strat7_cache.get(symbol)
        if not cache: return

        metrics = self.screener_data.get(symbol, {})
        if metrics.get('over_adr'):
            return # Volatility Decay Guard

        # Ensure cache isn't stale
        if time.time() - cache['timestamp'] > 65: return

        rec_small = cache['small'].summary['RECOMMENDATION']
        rec_mid = cache['mid'].summary['RECOMMENDATION']
        rec_high = cache['high'].summary['RECOMMENDATION']

        # v4.0 Pullback Alignment: Enter when Small flips back to main trend
        # We need to track previous state to detect the flip
        prev_small = sd.get('last_strat7_small_rec')
        sd['last_strat7_small_rec'] = rec_small

        signal = None

        # Pullback Entry: HTF/Mid Bullish, Small WAS not Bullish, NOW IS Bullish
        if "BUY" in rec_high and "BUY" in rec_mid:
            if "BUY" in rec_small and (prev_small is None or "BUY" not in prev_small):
                signal = 'buy'
                self.log(f"Strategy 7: Pullback entry BUY on {symbol} (1m flipped back to trend)")
        elif "SELL" in rec_high and "SELL" in rec_mid:
            if "SELL" in rec_small and (prev_small is None or "SELL" not in prev_small):
                signal = 'sell'
                self.log(f"Strategy 7: Pullback entry SELL on {symbol}")

        if signal:
            now = time.time()
            time_key = int(now // 60)
            if sd.get('last_trade_ltf') != time_key:
                sd['last_trade_ltf'] = time_key
                # Sync ATR to engine snapshot
                sd['last_trade_snapshot'] = {
                    'confidence': self.screener_data[symbol].get('confidence', 0),
                    'atr': self.screener_data[symbol].get('atr', 0),
                    'entry_time': now
                }
                self._execute_trade(symbol, signal)

    def _calculate_snr_zones(self, symbol, granularity=None):
        sd = self.symbol_data.get(symbol)
        if not sd: return

        strat_key = self.config.get('active_strategy')

        # v4.0 Zone Width Validation: Skip if > 1.5x ATR
        atr_val = 0
        if granularity == 3600:
            df = pd.DataFrame(sd.get('htf_candles', []))
            if len(df) >= 14: atr_val = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range().iloc[-1]
        elif granularity == 900:
            df = pd.DataFrame(sd.get('m15_candles', []))
            if len(df) >= 14: atr_val = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range().iloc[-1]
        if granularity is None:
            strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])
            granularity = strat['htf_granularity']

        candles = []
        if granularity == 3600: candles = sd.get('htf_candles', [])
        elif granularity == 900: candles = sd.get('m15_candles', [])
        elif granularity == 300: candles = sd.get('m5_candles', [])

        if len(candles) < 20: return

        candles = candles[-100:]
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

        # v4.0 Zone Width Validation check
        final_zones = []
        for z in active_zones:
            # We don't have explicit width here, but we can check cluster range
            # Actually, let's just use the touch counter and freshness
            # Carry over touch counts for existing zones
            old_zones = sd.get('snr_zones', [])
            for oz in old_zones:
                if abs(z['price'] - oz['price']) / oz['price'] < 0.001:
                    z['total_lifetime_touches'] = oz.get('total_lifetime_touches', 0)
                    break
            if 'total_lifetime_touches' not in z: z['total_lifetime_touches'] = z['touches']

            # Retirement check: Retire after 5 touches
            if z['total_lifetime_touches'] <= 5:
                final_zones.append(z)

        # Sort by strength (touches) and take top 5
        final_zones.sort(key=lambda x: x['touches'], reverse=True)
        sd['snr_zones'] = final_zones[:5]

        if final_zones:
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

    def _track_daily_open_crosses(self, symbol, current_price):
        sd = self.symbol_data[symbol]
        htf_open = sd['htf_open']
        if htf_open is None: return

        current_side = 'above' if current_price > htf_open else 'below'
        if sd['last_cross_side'] is not None and sd['last_cross_side'] != current_side:
            sd['daily_crosses'] += 1
            self.log(f"Daily Open Cross detected for {symbol}. Total crosses: {sd['daily_crosses']}")
        sd['last_cross_side'] = current_side

    def _score_reversal_pattern(self, symbol, pattern, candles):
        if not candles: return 0
        c = candles[-1]
        prev = candles[-2] if len(candles) > 1 else None

        score = 0
        body = abs(c['close'] - c['open'])
        total_range = c['high'] - c['low']
        if total_range == 0: return 0

        # 1. Wick-to-body ratio (>2:1)
        upper_wick = c['high'] - max(c['open'], c['close'])
        lower_wick = min(c['open'], c['close']) - c['low']
        max_wick = max(upper_wick, lower_wick)
        if body > 0 and (max_wick / body) >= 2: score += 1
        elif body == 0: score += 1

        # 2. Close position within candle (top/bottom 25%)
        if pattern.startswith('bullish'):
            if c['close'] >= (c['low'] + total_range * 0.75): score += 1
        elif pattern.startswith('bearish'):
            if c['close'] <= (c['low'] + total_range * 0.25): score += 1
        elif pattern == 'doji': score += 1

        # 3. Prior candle strongly directional
        if prev:
            prev_body = abs(prev['close'] - prev['open'])
            prev_range = prev['high'] - prev['low']
            if prev_range > 0 and (prev_body / prev_range) > 0.6: score += 1

        return score

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

        # v4.0 Track crosses for Strategy 1 whipsaw limit
        if strat_key == 'strategy_1':
            self._track_daily_open_crosses(symbol, current_price)

        if strat_key == 'strategy_4':
            # SNR Price Action Logic
            # Hard Invalidation: If price closes a full 1m candle through the zone
            zones = sd.get('snr_zones', [])
            if is_candle_close:
                remaining_zones = []
                for z in zones:
                    # Bullish zone (Support) broken if closed below
                    if z['type'] in ['S', 'Flip'] and current_ltf['close'] < (z['price'] * 0.9995):
                        self.log(f"Strategy 4: Zone {z['price']:.2f} broken (Bearish close through).")
                        continue
                    # Bearish zone (Resistance) broken if closed above
                    if z['type'] in ['R', 'Flip'] and current_ltf['close'] > (z['price'] * 1.0005):
                        self.log(f"Strategy 4: Zone {z['price']:.2f} broken (Bullish close through).")
                        continue
                    remaining_zones.append(z)
                sd['snr_zones'] = remaining_zones
                zones = remaining_zones

            if not is_candle_close: return # Only on 1m candle close
            if not zones: return

            pattern = self._check_price_action_patterns(sd['ltf_candles'])
            if not pattern or pattern == "marubozu": return

            # v4.0 Momentum Exhaustion Filter: 5m RSI
            if len(sd.get('m5_candles', [])) >= 14:
                df_m5 = pd.DataFrame(sd['m5_candles'])
                rsi_m5 = ta.momentum.RSIIndicator(df_m5['close']).rsi().iloc[-1]
            else: rsi_m5 = 50

            # Pattern Scoring
            pattern_score = self._score_reversal_pattern(symbol, pattern, sd['ltf_candles'])
            if pattern_score < 2: return

            # HTF EMA Confluence
            if len(sd.get('htf_candles', [])) >= 50:
                df_h1 = pd.DataFrame(sd['htf_candles'])
                ema50_h1 = ta.trend.EMAIndicator(df_h1['close'], window=50).ema_indicator().iloc[-1]
            else: ema50_h1 = None

            # Check if current candle touched any zone
            for z in zones:
                # Buffer: 0.02%
                buffer = z['price'] * 0.0002
                touched = current_ltf['low'] <= (z['price'] + buffer) and current_ltf['high'] >= (z['price'] - buffer)

                if touched:
                    # Bullish Reversal at Support or Flip
                    if z['type'] in ['S', 'Flip'] and pattern in ['bullish_pin', 'bullish_engulfing', 'doji', 'tweezer_bottom', 'bullish_harami']:
                        if rsi_m5 < 80: # Momentum exhaustion filter
                            # Confluence: Alignment with H1 EMA 50
                            if ema50_h1 is None or current_price > ema50_h1:
                                signal = 'buy'
                                z['total_lifetime_touches'] = z.get('total_lifetime_touches', 0) + 1
                                self.log(f"Strategy 4 BUY Signal: {pattern} (Score: {pattern_score}) at {z['type']} zone {z['price']:.2f}")
                                break
                    # Bearish Reversal at Resistance or Flip
                    elif z['type'] in ['R', 'Flip'] and pattern in ['bearish_pin', 'bearish_engulfing', 'doji', 'tweezer_top', 'bearish_harami']:
                        if rsi_m5 > 20: # Momentum exhaustion filter
                            if ema50_h1 is None or current_price < ema50_h1:
                                signal = 'sell'
                                z['total_lifetime_touches'] = z.get('total_lifetime_touches', 0) + 1
                                self.log(f"Strategy 4 SELL Signal: {pattern} (Score: {pattern_score}) at {z['type']} zone {z['price']:.2f}")
                                break
        elif strat_key == 'strategy_7':
            # Strategy 7: Multi-Timeframe Alignment
            self._process_strategy_7(symbol, is_candle_close)

        elif strat_key in ['strategy_5', 'strategy_6']:
            # Intelligence Screener Strategy
            metrics = self.screener_data.get(symbol)
            if not metrics: return

            contract_type = self.config.get('contract_type', 'rise_fall')
            is_multiplier = (contract_type == 'multiplier')

            threshold = metrics.get('threshold', 72 if not is_multiplier else 68)
            if strat_key == 'strategy_6': threshold = 60 # Default for legacy v1

            if is_multiplier:
                # Mode B: Multiplier (Day Trading)
                if abs(metrics['confidence']) >= threshold:
                    direction = metrics['direction']
                    if strat_key == 'strategy_6':
                        # Legacy v1 simple execution
                        signal = 'buy' if direction == 'CALL' else 'sell'
                        self.log(f"Strategy 6 MULTIPLIER {direction} on {symbol} - Conf: {metrics['confidence']}%")
                        return # Skip v2.1 logic below

                    # Trend & Structure Alignment: Pullback to 15m EMA 50 or SuperTrend
                    df_m15 = pd.DataFrame(sd.get('m15_candles', []))
                    if not df_m15.empty:
                        ema50_15 = ta.trend.EMAIndicator(df_m15['close'], window=50).ema_indicator().iloc[-1]
                        st_15, st_dir_15 = self._calculate_supertrend(df_m15)

                        price_15 = df_m15['close'].iloc[-1]
                        near_zone = (abs(price_15 - ema50_15) / ema50_15 < 0.005) or \
                                    (abs(price_15 - st_15.iloc[-1]) / st_15.iloc[-1] < 0.005)

                        if near_zone:
                            # 5m chart shows momentum resumption
                            df_m5 = pd.DataFrame(sd.get('m5_candles', []))
                            if not df_m5.empty:
                                last_m5 = df_m5.iloc[-1]
                                m5_resumed = (direction == 'CALL' and last_m5['close'] > last_m5['open']) or \
                                             (direction == 'PUT' and last_m5['close'] < last_m5['open'])

                                if m5_resumed:
                                    # v2.1 ADDITION: 1m Entry Confirmation (Precision leg)
                                    if sd['ltf_candles']:
                                        last_ltf = sd['ltf_candles'][-1]
                                        ltf_confirmed = (direction == 'CALL' and last_ltf['close'] > last_ltf['open']) or \
                                                        (direction == 'PUT' and last_ltf['close'] < last_ltf['open'])
                                        if ltf_confirmed:
                                            signal = 'buy' if direction == 'CALL' else 'sell'
                                            self.log(f"Strategy 5 MULTIPLIER {direction} on {symbol} - Conf: {metrics['confidence']}% (Threshold: {threshold}%)")
            else:
                # Mode A: Rise & Fall (Scalping)
                # Signal Trigger (Adaptive Threshold, v4.0)
                if abs(metrics['confidence']) >= threshold:
                    direction = metrics['direction']

                    # Structure Context: 5m Fractal Retest OR 1H SNR/15m BB touch
                    at_structure = False

                    price_5m = sd['current_ltf_candle']['close'] if sd.get('current_ltf_candle') else sd['last_tick']

                    # 5m Fractals (Resistance for PUT, Support for CALL)
                    f_highs = sd.get('fractal_highs', [])[-3:]
                    f_lows = sd.get('fractal_lows', [])[-3:]

                    fractal_touch = False
                    if direction == 'PUT':
                        fractal_touch = any(abs(price_5m - fh) / fh < 0.002 for fh in f_highs)
                    else:
                        fractal_touch = any(abs(price_5m - fl) / fl < 0.002 for fl in f_lows)

                    at_structure = fractal_touch

                    # Fallback to S/R or BB
                    if not at_structure:
                        df_m15 = pd.DataFrame(sd.get('m15_candles', []))
                        if not df_m15.empty:
                            bb_15 = ta.volatility.BollingerBands(df_m15['close'])
                            price_15 = df_m15['close'].iloc[-1]
                            at_bb = (direction == 'PUT' and price_15 >= bb_15.bollinger_hband().iloc[-1]) or \
                                    (direction == 'CALL' and price_15 <= bb_15.bollinger_lband().iloc[-1])

                            zones = sd.get('snr_zones', [])
                            at_snr = any(abs(price_15 - z['price']) / z['price'] < 0.002 for z in zones)
                            at_structure = at_bb or at_snr

                    if at_structure or strat_key == 'strategy_6':
                        if strat_key == 'strategy_6':
                            # Legacy v1 simple execution (Bypass structure patterns)
                            signal = 'buy' if direction == 'CALL' else 'sell'
                            self.log(f"Strategy 6 SCALP {direction} on {symbol} - Conf: {metrics['confidence']}%")
                        else:
                            # v4.0 MANDATORY CO-CONDITION: Stoch RSI Extreme Zone for Fractal touches
                            srsi_k = metrics.get('srsi_k', 0.5)
                            stoch_extreme = (direction == 'CALL' and srsi_k <= 0.2) or \
                                            (direction == 'PUT' and srsi_k >= 0.8)

                            if fractal_touch and not stoch_extreme:
                                # Mandatory extreme RSI only for fractal setups
                                return

                            # 1m chart reversal candle (Trigger)
                            if sd['ltf_candles']:
                                pattern = self._check_price_action_patterns(sd['ltf_candles'])
                                if direction == 'CALL' and pattern in ['bullish_pin', 'bullish_engulfing', 'tweezer_bottom']:
                                    signal = 'buy'
                                elif direction == 'PUT' and pattern in ['bearish_pin', 'bearish_engulfing', 'tweezer_top']:
                                    signal = 'sell'
                                if signal:
                                    self.log(f"Strategy 5 SCALP {direction} on {symbol} - Conf: {metrics['confidence']}% (Threshold: {threshold}%) - Pattern: {pattern}")
        else:
            # Default Breakout Logic (Strategy 1, 2, 3)
            if strat_key == 'strategy_1' and not is_candle_close:
                return

            check_price = current_ltf['close'] if is_candle_close else current_price

            if strat_key == 'strategy_1':
                # v4.0 Strategy 1 Improvements
                if sd.get('daily_crosses', 0) > 3:
                    return # Whipsaw limit reached

                # Trend Filter: 4H 100 EMA
                if len(sd.get('h4_candles', [])) >= 100:
                    df_h4 = pd.DataFrame(sd['h4_candles'])
                    ema100_h4 = ta.trend.EMAIndicator(df_h4['close'], window=100).ema_indicator().iloc[-1]
                    if current_price > ema100_h4: trend_bias = 'buy'
                    else: trend_bias = 'sell'
                else: trend_bias = None

                # Breakout Logic
                if current_ltf['open'] <= htf_open and check_price > htf_open and check_price > current_ltf['open']:
                    if trend_bias is None or trend_bias == 'buy': signal = 'buy'
                elif current_ltf['open'] >= htf_open and check_price < htf_open and check_price < current_ltf['open']:
                    if trend_bias is None or trend_bias == 'sell': signal = 'sell'

            elif strat_key == 'strategy_2':
                # v4.0 Strategy 2 Improvements
                # 1. Momentum Qualifier: 3m RSI
                if len(sd.get('m3_candles', [])) >= 14:
                    df_m3 = pd.DataFrame(sd['m3_candles'])
                    rsi_m3 = ta.momentum.RSIIndicator(df_m3['close']).rsi().iloc[-1]
                else: rsi_m3 = 50

                # 2. HTF Bias Gate: 4H EMA 21 vs 50
                if len(sd.get('h4_candles', [])) >= 50:
                    df_h4 = pd.DataFrame(sd['h4_candles'])
                    ema21_h4 = ta.trend.EMAIndicator(df_h4['close'], window=21).ema_indicator().iloc[-1]
                    ema50_h4 = ta.trend.EMAIndicator(df_h4['close'], window=50).ema_indicator().iloc[-1]
                    bias = 'buy' if ema21_h4 > ema50_h4 else 'sell'
                else: bias = None

                # Breakout Logic with filters
                if current_ltf['open'] <= htf_open and check_price > htf_open and check_price > current_ltf['open']:
                    if rsi_m3 > 55 and (bias is None or bias == 'buy'): signal = 'buy'
                elif current_ltf['open'] >= htf_open and check_price < htf_open and check_price < current_ltf['open']:
                    if rsi_m3 < 45 and (bias is None or bias == 'sell'): signal = 'sell'

            elif strat_key == 'strategy_3':
                # v4.0 Strategy 3 Improvements
                # 1. Trade Cap: 4 entries per hour
                now = datetime.now(timezone.utc)
                current_hour = now.hour
                if sd.get('last_trade_hour') != current_hour:
                    sd['last_trade_hour'] = current_hour
                    sd['hourly_trade_count'] = 0

                if sd.get('hourly_trade_count', 0) >= 4: return

                # 2. Volatility Regime Check: 1m ATR percentile
                atr_1m = 0
                if len(sd['ltf_candles']) >= 14:
                    df_1m = pd.DataFrame(sd['ltf_candles'])
                    atr_1m = ta.volatility.AverageTrueRange(df_1m['high'], df_1m['low'], df_1m['close']).average_true_range().iloc[-1]
                    sd['atr_1m_history'].append(atr_1m)

                if len(sd['atr_1m_history']) >= 20:
                    # bottom 20th percentile check
                    p20 = np.percentile(list(sd['atr_1m_history']), 20)
                    if atr_1m < p20: return # Low volatility skip

                # 3. Candle Sequence Filter: 2 consecutive 1m closes
                if len(sd['ltf_candles']) >= 2:
                    c1 = sd['ltf_candles'][-1]
                    c2 = sd['ltf_candles'][-2]

                    if side == 'buy': # Will be determined below
                        pass

                    # Manual breakout check since we need sequence
                    is_bullish_seq = c1['close'] > htf_open and c2['close'] > htf_open and c1['close'] > c1['open']
                    is_bearish_seq = c1['close'] < htf_open and c2['close'] < htf_open and c1['close'] < c1['open']

                    if is_bullish_seq: signal = 'buy'
                    elif is_bearish_seq: signal = 'sell'

            else:
                # Default behavior for other strategies or Fallback
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

            # --- DECISION MAKING POSITION ENGINE v4.0 ---
            strat_key = self.config.get('active_strategy')

            if strat_key == 'strategy_1' and current_price:
                sd = self.symbol_data.get(symbol, {})
                htf_open = sd.get('htf_open')
                if htf_open:
                    # Exit if closed back across Daily Open
                    if (side == 'long' and current_price < htf_open) or (side == 'short' and current_price > htf_open):
                        self.log(f"Strategy 1 EXIT for {symbol}: Price crossed back Daily Open.")
                        self._close_contract(cid)
                        continue

                    # Exit at +2 Daily ATRs
                    if len(sd.get('daily_candles', [])) >= 14:
                        df_d = pd.DataFrame(sd['daily_candles'])
                        daily_atr = ta.volatility.AverageTrueRange(df_d['high'], df_d['low'], df_d['close']).average_true_range().iloc[-1]
                        entry_p = c.get('entry_price')
                        if entry_p:
                            profit_dist = (current_price - entry_p) if is_long else (entry_p - current_price)
                            if profit_dist > (2 * daily_atr):
                                self.log(f"Strategy 1 EXIT for {symbol}: +2 Daily ATR target reached.")
                                self._close_contract(cid)
                                continue

            if (strat_key == 'strategy_5' or strat_key == 'strategy_7') and current_price:
                sd = self.symbol_data.get(symbol, {})
                df_h = pd.DataFrame(sd.get('htf_candles', []))
                df_m15 = pd.DataFrame(sd.get('m15_candles', []))

                if not df_h.empty and len(df_h) >= 20 and not df_m15.empty and len(df_m15) >= 20:
                    exit_reason = None

                    # 1. Divergence Hard Exit (1H)
                    div = self._detect_macd_divergence(df_h)
                    if (is_long and div == -1) or (not is_long and div == 1):
                        exit_reason = "MACD Divergence detected"

                    # 2. Multiplier Management
                    is_multiplier = c.get('contract_type') in ['MULTUP', 'MULTDOWN']
                    if is_multiplier and not exit_reason:
                        entry_price = c.get('entry_price')
                        atr_1h = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close']).average_true_range().iloc[-1]

                        if entry_price:
                            profit_pips = (current_price - entry_price) if is_long else (entry_price - current_price)

                            # v4.0 "Free Ride" Protocol: Trailing Zone via Fractals/ATR
                            if profit_pips >= 1.5 * atr_1h and not c.get('is_freeride'):
                                self.log(f"Multiplier FREE RIDE for {symbol}: 1.5 ATR profit reached. Moving SL to structural safety zone.")

                                # Find recent 1m Fractal for structural safety
                                trailing_sl = entry_price
                                if sd.get('fractal_lows') and is_long:
                                    trailing_sl = sd['fractal_lows'][-1]
                                elif sd.get('fractal_highs') and not is_long:
                                    trailing_sl = sd['fractal_highs'][-1]
                                else:
                                    # Fallback to Entry + ATR buffer if no fractal
                                    buffer = atr_1h * 0.2
                                    trailing_sl = (entry_price + buffer) if is_long else (entry_price - buffer)

                                c['sl_price'] = trailing_sl
                                c['is_freeride'] = True

                            # SuperTrend Trailing (15m)
                            if c.get('is_freeride'):
                                _, st_dir = self._calculate_supertrend(df_m15)
                                if (is_long and st_dir.iloc[-1] == -1) or (not is_long and st_dir.iloc[-1] == 1):
                                    exit_reason = "15m SuperTrend reversal (Trailing)"

                    if exit_reason:
                        self.log(f"Strategy {strat_key[-1]} Engine EXIT for {symbol} ({cid}): {exit_reason}.")
                        self._close_contract(cid)
                        continue

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

        sd = self.symbol_data.get(symbol)
        if not sd: return

        strat_key = self.config.get('active_strategy', 'strategy_1')
        strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])

        duration_seconds = 0
        now = datetime.now(timezone.utc)
        expiry_label = ""

        custom_expiry = self.config.get('custom_expiry', 'default')

        if strat_key == 'strategy_1':
            # v4.0 Dynamic Expiry: Check if price moved > 2 Daily ATRs
            daily_atr = 0
            if len(sd.get('daily_candles', [])) >= 14:
                df_d = pd.DataFrame(sd['daily_candles'])
                daily_atr = ta.volatility.AverageTrueRange(df_d['high'], df_d['low'], df_d['close']).average_true_range().iloc[-1]

            # For now, Strategy 1 remains EOD as base, but we will add ATR TP in monitor
            end_of_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            duration_seconds = int((end_of_day - now).total_seconds())
            expiry_label = f"Expiry: {end_of_day.strftime('%H:%M:%S')} UTC"

        elif strat_key == 'strategy_2':
            # v4.0 Distance-Based Expiry:
            # If moved > 1 ATR away from 1H open, reduce to 30m
            duration_seconds = 3600
            if len(sd.get('htf_candles', [])) >= 14:
                df_h = pd.DataFrame(sd['htf_candles'])
                h1_atr = ta.volatility.AverageTrueRange(df_h['high'], df_h['low'], df_h['close']).average_true_range().iloc[-1]
                dist = abs(sd['last_tick'] - sd['htf_open'])
                if dist > h1_atr:
                    duration_seconds = 1800
                    self.log(f"Strategy 2: Exhaustion detected (dist {dist:.2f} > ATR {h1_atr:.2f}). Reducing expiry to 30m.")

            expiry_label = f"Expiry: {duration_seconds // 60}m"

        elif strat_key == 'strategy_3':
            # v4.0 Dynamic Expiry: remaining time on current 15m candle + 2m
            htf_gran = 900 # 15m
            next_close_epoch = ((int(now.timestamp()) // htf_gran) + 1) * htf_gran
            duration_seconds = (next_close_epoch - int(now.timestamp())) + 120
            expiry_label = f"Expiry: {duration_seconds // 60}m {duration_seconds % 60}s"

            # Increment trade count for hourly cap
            sd['hourly_trade_count'] = sd.get('hourly_trade_count', 0) + 1

        elif strat_key == 'strategy_5':
            metrics = self.screener_data.get(symbol, {})
            contract_type = self.config.get('contract_type', 'rise_fall')
            is_multiplier = (contract_type == 'multiplier')

            if not is_multiplier:
                # Rise & Fall Constraints
                # 1. Late Entry Penalty
                if sd['ltf_candles']:
                    last_c = sd['ltf_candles'][-1]
                    body = abs(last_c['close'] - last_c['open'])
                    df_ltf = pd.DataFrame(sd['ltf_candles'])
                    avg_atr = ta.volatility.AverageTrueRange(df_ltf['high'], df_ltf['low'], df_ltf['close']).average_true_range().mean()
                    if body > (avg_atr * 0.3):
                        self.log(f"Strategy 5 Scalp CANCELLED: Late entry (body {body:.4f} > 30% avg ATR {avg_atr*0.3:.4f})")
                        return

                # 2. Volatility Freeze (v4.0 Instrument Specific)
                atr_1m = metrics.get('atr_1m', 0)
                atr_24h = metrics.get('atr_24h', 0)
                if atr_24h > 0 and atr_1m < (atr_24h * 0.1):
                    self.log(f"Strategy 5 Scalp PAUSED: Volatility too low (1m ATR {atr_1m} < 10% of 24h ATR {atr_24h})")
                    return
                elif atr_1m < 0.00001: # Fail-safe absolute baseline
                    self.log(f"Strategy 5 Scalp PAUSED: Volatility too low (1m ATR: {atr_1m})")
                    return

                # Dynamic expiry based on trigger timeframe
                # We simplified this in _update_screener's suggested_expiry
                duration_minutes = metrics.get('expiry_min', 5)
                duration_seconds = duration_minutes * 60
                expiry_label = f"Scalp Expiry: {duration_minutes}m"
            else:
                expiry_label = "Multiplier Position"
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

        # v4.0 Strategy 4 Position Sizing: Reduce after 3 touches
        if strat_key == 'strategy_4':
            sd = self.symbol_data.get(symbol, {})
            # Find the zone we are trading
            current_price = sd['last_tick']
            for z in sd.get('snr_zones', []):
                if abs(current_price - z['price']) / z['price'] < 0.005:
                    if z.get('total_lifetime_touches', 0) >= 3:
                        amount *= 0.5
                        self.log(f"Strategy 4: Zone heavily tested ({z.get('total_lifetime_touches')} touches). Reducing position size by 50%.")
                    break
        if not self.config.get('use_fixed_balance'):
            amount = (amount / 100.0) * self.account_balance

        amount = max(0.35, round(amount, 2))

        contract_type = self.config.get('contract_type', 'rise_fall')
        is_multiplier = (strat_key == 'strategy_5' and contract_type == 'multiplier')

        if is_multiplier:
            # Use 5% of balance for multipliers
            if not self.config.get('use_fixed_balance'):
                amount = max(0.35, round(self.account_balance * 0.05, 2))

            # Multiplier tied to Volatility (ATR)
            metrics = self.screener_data.get(symbol, {})
            mult_val = metrics.get('multiplier', int(self.config.get('multiplier_value', 100)))

            # TP/SL based on 1H ATR (Mode B: 1.5x ATR SL, 3.0x ATR TP)
            atr_1h = metrics.get('atr', 1.0)
            # We need to convert ATR-based price targets to USD profit/loss for multipliers
            # Profit = (Price_Change / Entry_Price) * Multiplier * Stake
            # So, Target_USD = (ATR_Multiple / Entry_Price) * Multiplier * Stake
            entry_price = sd['last_tick']
            sl_usd = ( (1.5 * atr_1h) / entry_price ) * mult_val * amount
            tp_usd = ( (3.0 * atr_1h) / entry_price ) * mult_val * amount

            self.log(f"Opening MULTIPLIER {side.upper()} on {symbol} | Stake: {amount} | Mult: {mult_val}x | ATR: {atr_1h}")

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

            limit_order = {
                'take_profit': round(tp_usd, 2),
                'stop_loss': round(sl_usd, 2)
            }
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
            # Capture entry snapshot
            if is_multiplier:
                metrics = self.screener_data.get(symbol, {})
                sd['last_trade_snapshot'] = {
                    'confidence': metrics.get('confidence', 0),
                    'atr': metrics.get('atr', 0),
                    'entry_time': time.time()
                }

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

                    sd = self.symbol_data.get(symbol)

                    if profit > 0:
                        self.total_trade_profit += profit
                        self.wins_count += 1
                        # v4.0 Streak Reset: 2 consecutive wins OR 1 win + ADX > 20
                        if sd:
                            sd['consecutive_wins'] = sd.get('consecutive_wins', 0) + 1
                            sd['consecutive_losses'] = 0

                            metrics = self.screener_data.get(symbol, {})
                            adx_val = metrics.get('adx', 0)

                            if sd['consecutive_wins'] >= 2 or adx_val > 20:
                                if sd.get('consecutive_losses', 0) >= 3:
                                    self.log(f"Adaptive Sensitivity RESET for {symbol} (Wins: {sd['consecutive_wins']}, ADX: {adx_val})")
                                sd['consecutive_losses'] = 0
                    else:
                        self.total_trade_loss += abs(profit)
                        self.losses_count += 1
                        # v4.0 Streak Tracking: Increment streak on loss
                        if sd:
                            sd['consecutive_losses'] += 1
                            sd['consecutive_wins'] = 0
                            if sd['consecutive_losses'] >= 3:
                                self.log(f"Adaptive Sensitivity: {symbol} on {sd['consecutive_losses']} loss streak. Threshold increased.", "warning")

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
                'status': c.get('status', 'Holding'),
                'is_freeride': c.get('is_freeride', False)
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
            'active_strategy': self.config.get('active_strategy'),
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

        if not self.screener_thread or not self.screener_thread.is_alive():
            self.screener_thread = threading.Thread(target=self._background_screener_loop, daemon=True)
            self.screener_thread.start()

        if not self.ws_thread or not self.ws_thread.is_alive():
            self.stop_event.clear()
            self.ws_thread = threading.Thread(target=self._run_ws, daemon=True)
            self.ws_thread.start()
        elif self.is_running and self.ws and self.ws.sock and self.ws.sock.connected:
            # Already connected but just started trading, trigger subscriptions
            self.log("Already connected, triggering trading subscriptions...")
            strat_key = self.config.get('active_strategy', 'strategy_1')
            strat = self.STRATEGY_MAP.get(strat_key, self.STRATEGY_MAP['strategy_1'])
            h_count = 200 if strat_key in ['strategy_4', 'strategy_5', 'strategy_6'] else 2

            for symbol in self.config.get('symbols', []):
                self._init_symbol_data(symbol)
                self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                time.sleep(0.5)

                if strat_key == 'strategy_7': continue

                self._fetch_history(self.ws, symbol, strat['ltf_granularity'], 100)
                time.sleep(0.5)
                self._fetch_history(self.ws, symbol, strat['htf_granularity'], h_count)
                time.sleep(0.5)

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
            with self.data_lock:
                # Keep subscription ids but clear candles/opens
                for sym in self.symbol_data:
                    sub_id = self.symbol_data[sym].get('subscription_id')
                    self._init_symbol_data(sym)
                    self.symbol_data[sym]['subscription_id'] = sub_id

            if self.ws and self.ws.sock and self.ws.sock.connected:
                strat = self.STRATEGY_MAP.get(new_strat, self.STRATEGY_MAP['strategy_1'])
                h_count = 200 if new_strat in ['strategy_4', 'strategy_5', 'strategy_6'] else 2

                for sym in new_symbols:
                    if new_strat == 'strategy_7':
                        self.ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
                        time.sleep(0.5)
                        continue

                    self._fetch_history(self.ws, sym, strat['ltf_granularity'], 100)
                    time.sleep(0.5)
                    self._fetch_history(self.ws, sym, strat['htf_granularity'], h_count)
                    time.sleep(0.5)

                    if new_strat == 'strategy_5':
                        for g, c in [(60, 100), (300, 100), (900, 200), (3600, 200), (86400, 50)]:
                            self._fetch_history(self.ws, sym, g, c)
                            time.sleep(0.4)
                        self.ws.send(json.dumps({"contracts_for": sym}))
                    elif new_strat == 'strategy_6':
                        for g, c in [(60, 100), (900, 200), (3600, 200), (86400, 50)]:
                            self._fetch_history(self.ws, sym, g, c)
                            time.sleep(0.4)
                        self.ws.send(json.dumps({"contracts_for": sym}))
                    time.sleep(0.5)
            return {"success": True}

        # If only symbols changed and we are connected
        if self.ws and self.ws.sock and self.ws.sock.connected:
            strat = self.STRATEGY_MAP.get(new_strat, self.STRATEGY_MAP['strategy_1'])
            h_count = 200 if new_strat in ['strategy_4', 'strategy_5', 'strategy_6'] else 2
            added_symbols = new_symbols - old_symbols
            for symbol in added_symbols:
                self.log(f"Subscribing to new symbol: {symbol}")
                self._init_symbol_data(symbol)
                self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                time.sleep(0.5)

                if new_strat == 'strategy_7': continue

                self._fetch_history(self.ws, symbol, strat['ltf_granularity'], 100)
                time.sleep(0.5)
                self._fetch_history(self.ws, symbol, strat['htf_granularity'], h_count)
                time.sleep(0.5)

                if new_strat == 'strategy_5':
                    for g, c in [(60, 100), (300, 100), (900, 200), (3600, 200), (86400, 50)]:
                        self._fetch_history(self.ws, symbol, g, c)
                        time.sleep(0.4)
                    self.ws.send(json.dumps({"contracts_for": symbol}))
                elif new_strat == 'strategy_6':
                    for g, c in [(60, 100), (900, 200), (3600, 200), (86400, 50)]:
                        self._fetch_history(self.ws, symbol, g, c)
                        time.sleep(0.4)
                    self.ws.send(json.dumps({"contracts_for": symbol}))
                time.sleep(0.5)

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
