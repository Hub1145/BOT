import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from collections import deque
import websocket

class TradingBotEngine:
    STRATEGY_MAP = {
        'strategy_1': {
            'name': 'Slow',
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
        }
    }

    def __init__(self, config_path, emit_callback):
        self.config_path = config_path
        self.emit = emit_callback
        self.console_logs = deque(maxlen=500)
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
                    self._fetch_history(ws, symbol, strat['htf_granularity'], 2)

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

        elif msg_type == 'buy':
            buy_data = data.get('buy')
            if buy_data:
                self.log(f"Trade opened: {buy_data.get('contract_id')} for {buy_data.get('buy_price')} USD")

        elif msg_type == 'sell':
            sell_data = data.get('sell')
            if sell_data:
                self.log(f"Trade closed: {sell_data.get('contract_id')}")

    def _init_symbol_data(self, symbol):
        if symbol not in self.symbol_data:
            self.symbol_data[symbol] = {
                'ltf_candles': [],
                'htf_open': None,
                'htf_epoch': None,
                'last_tick': None,
                'last_processed_ltf': None,
                'last_trade_ltf': None,
                'current_ltf_candle': None
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
            self._monitor_open_contracts()

            if self.is_running:
                # HTF Refresh for Strategy 2 (Hourly) and Strategy 3 (15m)
                if strat_key in ['strategy_2', 'strategy_3']:
                    htf_gran = strat['htf_granularity']
                    if sd['htf_epoch'] is None or tick.get('epoch') >= sd['htf_epoch'] + htf_gran:
                        last_fetch = sd.get('last_htf_fetch_time', 0)
                        if time.time() - last_fetch > 60: # Throttle to once per minute
                            sd['last_htf_fetch_time'] = time.time()
                            self._fetch_history(self.ws, symbol, htf_gran, 2)

                # LTF Candle Management
                if sd['current_ltf_candle']:
                    candle_start = datetime.fromtimestamp(sd['current_ltf_candle']['epoch'], tz=timezone.utc)
                    if tick_time >= candle_start + timedelta(seconds=strat['ltf_granularity']):
                        # LTF Candle transition
                        self.log(f"LTF ({ltf_min}m) Candle closed for {symbol} at {sd['current_ltf_candle']['close']}")
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

        # Strategy Breakout Logic
        signal = None
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

    def _monitor_open_contracts(self):
        now_epoch = int(time.time())
        force_close_enabled = self.config.get('force_close_enabled', False)
        force_close_duration = self.config.get('force_close_duration', 60)
        tp_enabled = self.config.get('tp_enabled', False)
        sl_enabled = self.config.get('sl_enabled', False)

        for cid in list(self.contracts.keys()):
            c = self.contracts[cid]

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

        if strat['expiry_type'] == 'eod':
            # End of day calculation (UTC)
            end_of_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            duration_seconds = int((end_of_day - now).total_seconds())
            expiry_label = f"Expiry: {end_of_day.strftime('%H:%M:%S')} UTC"
        elif strat['expiry_type'] == 'fixed':
            if custom_expiry != 'default':
                try:
                    duration_seconds = int(custom_expiry)
                    if duration_seconds >= 60:
                        expiry_label = f"Expiry: {duration_seconds // 60} minutes"
                    else:
                        expiry_label = f"Expiry: {duration_seconds} seconds"
                except:
                    duration_seconds = strat['duration']
                    expiry_label = f"Expiry: {duration_seconds // 60} minutes"
            else:
                duration_seconds = strat['duration']
                expiry_label = f"Expiry: {duration_seconds // 60} minutes"

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
            side = 'long' if contract['contract_type'] == 'CALL' else 'short'

            if is_sold:
                if cid in self.contracts:
                    profit = contract.get('profit', 0)
                    self.log(f"Trade {cid} ({symbol}) closed. PnL: {profit}")
                    self.net_trade_profit += profit
                    if profit > 0: self.total_trade_profit += profit
                    else: self.total_trade_loss += abs(profit)
                    self.total_trades_count += 1
                    del self.contracts[cid]
            else:
                profit = contract.get('profit', 0)
                is_closing = self.contracts.get(cid, {}).get('is_closing', False)

                self.contracts[cid] = {
                    'id': cid, 'symbol': symbol, 'side': side,
                    'entry_price': contract.get('entry_tick'),
                    'pnl': profit,
                    'stake': contract.get('buy_price', 0),
                    'purchase_time': contract.get('purchase_time'),
                    'expiry_time': contract.get('date_expiry'),
                    'is_closing': is_closing
                }

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
                'expiry_time': c['expiry_time']
            })
            floating_pnl += c['pnl']
            used_notional += c['stake']

        self.net_profit = floating_pnl + self.net_trade_profit
        self.used_amount_notional = used_notional
        self.cached_pos_notional = used_notional

        payload = {
            'running': self.is_running,
            'is_demo': self.config.get('is_demo', True),
            'total_balance': self.account_balance,
            'available_balance': self.available_balance,
            'open_trades': self.open_trades,
            'net_profit': self.net_profit,
            'total_trades': self.total_trades_count + len(self.open_trades),
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
            with self.data_lock:
                # Keep subscription ids but clear candles/opens
                for sym in self.symbol_data:
                    sub_id = self.symbol_data[sym].get('subscription_id')
                    self._init_symbol_data(sym)
                    self.symbol_data[sym]['subscription_id'] = sub_id

            if self.ws and self.ws.sock and self.ws.sock.connected:
                strat = self.STRATEGY_MAP.get(new_strat, self.STRATEGY_MAP['strategy_1'])
                for sym in new_symbols:
                    self._fetch_history(self.ws, sym, strat['ltf_granularity'], 100)
                    self._fetch_history(self.ws, sym, strat['htf_granularity'], 2)
            return {"success": True}

        # If only symbols changed and we are connected
        if self.ws and self.ws.sock and self.ws.sock.connected:
            strat = self.STRATEGY_MAP.get(new_strat, self.STRATEGY_MAP['strategy_1'])
            added_symbols = new_symbols - old_symbols
            for symbol in added_symbols:
                self.log(f"Subscribing to new symbol: {symbol}")
                self._init_symbol_data(symbol)
                self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                self._fetch_history(self.ws, symbol, strat['ltf_granularity'], 100)
                self._fetch_history(self.ws, symbol, strat['htf_granularity'], 2)

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
