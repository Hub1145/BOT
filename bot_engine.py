import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from collections import deque
import websocket
import pandas as pd
import numpy as np

class TradingBotEngine:
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

        # Positions and data
        self.open_trades = []
        self.contracts = {} # contract_id -> contract_info
        self.symbol_data = {} # Symbol -> { '15min_candles': [], 'daily_open': price, 'last_tick': price, ... }

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
            print(f"Error loading config: {e}")
            return {}

    def log(self, message, level='info'):
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = {'timestamp': timestamp, 'message': message, 'level': level}
        self.console_logs.append(log_entry)
        self.emit('console_log', log_entry)

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
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            for symbol in self.config.get('symbols', []):
                self._init_symbol_data(symbol)
                ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                self._fetch_history(ws, symbol, 900, 100) # 15min
                self._fetch_history(ws, symbol, 86400, 1)  # Daily
            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

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
            self._handle_tick(data['tick'])

        elif msg_type == 'proposal_open_contract':
            self._handle_contract_update(data['proposal_open_contract'])

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
                '15min_candles': [],
                'daily_open': None,
                'last_tick': None,
                'last_signal_15min': None,
                'current_15min_candle': None
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
        with self.data_lock:
            if symbol not in self.symbol_data: return
            if granularity == 86400:
                if candles:
                    self.symbol_data[symbol]['daily_open'] = candles[-1]['open']
                    self.log(f"Daily Open for {symbol}: {self.symbol_data[symbol]['daily_open']}")
            elif granularity == 900:
                self.symbol_data[symbol]['15min_candles'] = candles
                if candles:
                    self.symbol_data[symbol]['current_15min_candle'] = candles[-1]

    def _handle_tick(self, tick):
        symbol = tick['symbol']
        price = tick['quote']
        tick_time = datetime.fromtimestamp(tick['epoch'], tz=timezone.utc)

        with self.data_lock:
            if symbol not in self.symbol_data: return
            sd = self.symbol_data[symbol]
            sd['last_tick'] = price

            if sd['current_15min_candle']:
                candle_start = datetime.fromtimestamp(sd['current_15min_candle']['epoch'], tz=timezone.utc)
                if tick_time >= candle_start + timedelta(minutes=15):
                    # 15min Candle transition
                    self.log(f"15m Candle closed for {symbol} at {sd['current_15min_candle']['close']}")
                    if self.is_running and self.config.get('entry_type') == 'candle_close':
                        self._process_strategy(symbol, True)

                    # Refresh daily open at start of day
                    if tick_time.hour == 0 and tick_time.minute == 0:
                        self._fetch_history(self.ws, symbol, 86400, 1)

                    # New 15m candle start time
                    new_start_minute = (tick_time.minute // 15) * 15
                    sd['current_15min_candle'] = {
                        'epoch': int(tick_time.replace(minute=new_start_minute, second=0, microsecond=0).timestamp()),
                        'open': price, 'high': price, 'low': price, 'close': price
                    }
                else:
                    sd['current_15min_candle']['close'] = price
                    sd['current_15min_candle']['high'] = max(sd['current_15min_candle']['high'], price)
                    sd['current_15min_candle']['low'] = min(sd['current_15min_candle']['low'], price)

            if self.is_running and self.config.get('entry_type') == 'tick':
                self._process_strategy(symbol, False)

    def _process_strategy(self, symbol, is_candle_close):
        sd = self.symbol_data[symbol]
        daily_open = sd['daily_open']
        current_15m = sd['current_15min_candle']
        current_price = sd['last_tick']

        if daily_open is None or current_15m is None or current_price is None:
            return

        time_key = current_15m['epoch']
        if sd.get('last_processed_15m') == time_key and is_candle_close:
            return # Already processed this candle close

        # Strategy
        signal = None
        check_price = current_15m['close'] if is_candle_close else current_price

        # BUY: 15m open <= Daily Open AND check_price > Daily Open AND check_price > 15m open (bullish)
        if current_15m['open'] <= daily_open and check_price > daily_open and check_price > current_15m['open']:
            signal = 'buy'
        # SELL: 15m open >= Daily Open AND check_price < Daily Open AND check_price < 15m open (bearish)
        elif current_15m['open'] >= daily_open and check_price < daily_open and check_price < current_15m['open']:
            signal = 'sell'

        if signal:
            # Check if we already traded this 15m period for this symbol to avoid multiple entries on ticks
            if sd.get('last_trade_15m') == time_key:
                return

            sd['last_trade_15m'] = time_key
            if is_candle_close:
                sd['last_processed_15m'] = time_key

            self._execute_trade(symbol, signal)

    def _execute_trade(self, symbol, side):
        # End of day calculation (UTC)
        now = datetime.now(timezone.utc)
        end_of_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        duration_seconds = int((end_of_day - now).total_seconds())

        if duration_seconds < 60:
            return

        # Position management: One trade per symbol, cancel opposite
        existing_cid = None
        for cid, c in self.contracts.items():
            if c['symbol'] == symbol:
                if c['side'] == side:
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

        self.log(f"Opening {side.upper()} on {symbol} | Stake: {amount} | Expiry: {end_of_day.strftime('%H:%M:%S')} UTC")

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
                self.contracts[cid] = {
                    'id': cid, 'symbol': symbol, 'side': side,
                    'entry_price': contract.get('entry_tick'),
                    'pnl': contract.get('profit', 0),
                    'stake': contract.get('buy_price', 0)
                }

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
                'entry_spot_price': c['entry_price'], 'stake': c['stake'], 'pnl': c['pnl']
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

    def _run_ws(self):
        while not self.stop_event.is_set():
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

    def stop_bot(self):
        self.stop_event.set()
        if self.ws:
            self.ws.close()
        self.log("Bot engine shut down")

    def _apply_api_credentials(self): pass

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
        self.config = new_config
        self.log("Config applied live")
        return {"success": True}

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
