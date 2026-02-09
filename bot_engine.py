import json
import time
import logging
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import websocket # The 'websocket-client' package provides the 'websocket' module
import ta
import threading
from collections import deque
import os # Added for file path operations
import requests
import hashlib
import hmac
import base64
import math
import _thread
from logging.handlers import RotatingFileHandler

# OKX API configuration defaults (now managed per instance)
OKX_REST_API_BASE_URL = "https://www.okx.com"

# Rate Limiter Class - Token Bucket Algorithm
class RateLimiter:
    """
    Token bucket rate limiter to prevent API rate limit errors.
    Supports different rate limits for different endpoint categories.
    """
    def __init__(self):
        self.locks = {}
        self.buckets = {}
        
        # Define rate limits per endpoint category (requests per second)
        # OKX limits: ~20-60 req/2s depending on endpoint
        self.limits = {
            'account': {'rate': 3, 'capacity': 6},      # Account endpoints: 3 req/s, burst 6
            'trade': {'rate': 3, 'capacity': 6},        # Trade endpoints: 3 req/s, burst 6
            'market': {'rate': 10, 'capacity': 20},     # Market data: 10 req/s, burst 20
            'public': {'rate': 10, 'capacity': 20},     # Public endpoints: 10 req/s, burst 20
            'default': {'rate': 5, 'capacity': 10}      # Default: 5 req/s, burst 10
        }
        
        # Initialize buckets
        for category in self.limits:
            self.locks[category] = threading.Lock()
            self.buckets[category] = {
                'tokens': self.limits[category]['capacity'],
                'last_update': time.time()
            }
    
    def _get_category(self, path):
        """Determine endpoint category from API path"""
        if '/account/' in path:
            return 'account'
        elif '/trade/' in path:
            return 'trade'
        elif '/market/' in path:
            return 'market'
        elif '/public/' in path:
            return 'public'
        else:
            return 'default'
    
    def acquire(self, path, tokens=1):
        """
        Acquire tokens before making a request.
        Blocks if insufficient tokens available.
        """
        category = self._get_category(path)
        lock = self.locks[category]
        
        with lock:
            while True:
                now = time.time()
                bucket = self.buckets[category]
                limit = self.limits[category]
                
                # Refill tokens based on time elapsed
                time_passed = now - bucket['last_update']
                bucket['tokens'] = min(
                    limit['capacity'],
                    bucket['tokens'] + time_passed * limit['rate']
                )
                bucket['last_update'] = now
                
                # Check if we have enough tokens
                if bucket['tokens'] >= tokens:
                    bucket['tokens'] -= tokens
                    return
                
                # Calculate wait time for next token
                tokens_needed = tokens - bucket['tokens']
                wait_time = tokens_needed / limit['rate']
                time.sleep(min(wait_time, 0.5))  # Sleep max 0.5s at a time

# product_info structure template (now managed per instance)
product_info_TEMPLATE = {
    "pricePrecision": None,
    "qtyPrecision": None,
    "priceTickSize": None,
    "minOrderQty": None,
    "contractSize": None,
}

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default

# Top-level signature helper (kept simple, takes params)
def generate_okx_signature(api_secret, timestamp, method, request_path, body_str=''):
    """Generate HMAC SHA256 signature for OKX API."""
    message = str(timestamp) + method.upper() + request_path + body_str
    hashed = hmac.new(api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    signature = base64.b64encode(hashed.digest()).decode('utf-8')
    return signature
    return signature

class TradingBotEngine:
    def __init__(self, config_path, emit_callback):
        self.config_path = config_path
        self.emit = emit_callback
        
        self.console_logs = deque(maxlen=500)
        self.config = self._load_config()

        # Configure logging based on config.log_level
        numeric_level = getattr(logging, self.config.get('log_level', 'info').upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f'Invalid log level: {self.config.get("log_level")}')
        
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG) # Set root logger to DEBUG to ensure all messages are captured

        # Clear existing handlers to prevent duplicate messages if bot is restarted
        for handler in root_logger.handlers[:]: # Iterate over a slice to safely modify list
            root_logger.removeHandler(handler)
        
        # Phase 0: Console Output (INFO and higher)
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)

        # Phase 1: Standard info.log (Capped at 5MB, Always active)
        info_handler = RotatingFileHandler('info.log', maxBytes=5*1024*1024, backupCount=1, encoding='utf-8')
        info_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        info_handler.setFormatter(info_formatter)
        info_handler.setLevel(logging.INFO)
        root_logger.addHandler(info_handler)

        # Phase 2: Detailed debug.log (Capped at 5MB, Only if log_level is DEBUG)
        if self.config.get('log_level', 'info').lower() == 'debug':
            debug_handler = RotatingFileHandler('debug.log', maxBytes=5*1024*1024, backupCount=1, encoding='utf-8')
            debug_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            debug_handler.setFormatter(debug_formatter)
            debug_handler.setLevel(logging.DEBUG)
            root_logger.addHandler(debug_handler)
            logging.info('Dual logging active: info.log and debug.log initialized.')
        else:
            logging.info('Standard logging active: info.log initialized. (debug.log suppressed)')

        self._apply_api_credentials()
        
        # Dual WebSocket Support
        self.ws_public = None
        self.ws_private = None
        
        self.ws_public_thread = None
        self.ws_private_thread = None
        
        self.is_running = False
        self.stop_event = threading.Event()
        self.bot_start_time = int(time.time() * 1000) # Track start time in ms
        
        self.current_balance = 0.0
        self.open_trades = []
        self.is_bot_initialized = threading.Event()
        
        # OKX specific variables (from example bot)
        self.historical_data_store = {}
        self.data_lock = threading.Lock()
        self.trade_data_lock = threading.Lock()
        self.latest_trade_price = None
        self.latest_trade_timestamp = None
        self.last_price_update_time = time.time() # High-precision timestamp of last price arrival
        self.account_balance = 0.0
        self.available_balance = 0.0
        self.total_equity = 0.0
        self.initial_total_capital = 0.0 # Session-based, in-memory only
        self.account_info_lock = threading.Lock()
        self.net_profit = 0.0 # Track actual PnL
    
        # Financial Display Metrics
        self.max_allowed_display = 0.0
        self.max_amount_display = 0.0
        self.remaining_amount_notional = 0.0
        self.trade_fees = 0.0
        self.total_realized_fees = 0.0 # [NEW] Authoritative sum from fills history
        
        # Audit Fix: Thread safety for batch operations (TP/SL Sync)
        self.batch_update_lock = threading.Lock()
        self.batch_update_in_progress = False
        
        # New State Variables for Client Logic
        self.total_capital_2nd = 0.0
        self.cumulative_margin_used = 0.0
        self.last_add_price = 0.0 # Tracks price of last entry/add for Gap Trigger
        self.auto_add_step_count = 0 # Tracks if we are on Step 1 (Market) or Step 2 (Limit)

        
        # Refactored for Dual-Direction Support
        self.in_position = {'long': False, 'short': False}
        self.position_entry_price = {'long': 0.0, 'short': 0.0}
        self.position_qty = {'long': 0.0, 'short': 0.0}
        self.position_liq = {'long': 0.0, 'short': 0.0}
        self.position_details = {'long': {}, 'short': {}} # Store raw OKX position data (lever, etc.)
        self.current_stop_loss = {'long': 0.0, 'short': 0.0}
        self.current_take_profit = {'long': 0.0, 'short': 0.0}
        self.position_exit_orders = {'long': {}, 'short': {}} # { 'long': {'tp': id, 'sl': id}, ... }
        self.entry_reduced_tp_flag = {'long': False, 'short': False}
        
        self.batch_counter = 0 # Track batches for logging
        self.monitoring_tick = 0 # Track monitoring cycles
        self.used_amount_notional = 0.0
        self.session_bot_used_notional = 0.0 # [NEW] Tracks bot-only volume for current session
        self.position_lock = threading.Lock()
        self.pending_entry_ids = [] # List to track multiple pending entry orders
        self.pending_entry_order_id = None # Kept for backward compatibility/single tracking if needed
        self.pending_entry_order_details = {} # Now will store details per order ID in a dict
        self.entry_sl_price = 0.0 # This might need migration too if we have concurrent entries? 
                                  # Entries are usually batch-based and transient. 
        self.sl_hit_triggered = False
        self.sl_hit_lock = threading.Lock()
        self.entry_order_with_sl = None
        self.entry_order_sl_lock = threading.Lock()
        self.tp_hit_triggered = False
        self.tp_hit_lock = threading.Lock()
        
        # Cooldown management
        self.last_close_time = {'long': 0.0, 'short': 0.0}
        self.monitoring_tick = 0
        self.bot_startup_complete = False
        self._should_update_tpsl = False
        self.last_emit_time = 0.0 # Throttling for real-time WS updates
        self.last_account_sync_time = 0 # Track last WS account update


        self.ws_subscriptions_ready = threading.Event()
        self.pending_subscriptions = set()
        
        self.total_trades_count = 0 # Persistent counter for individual fills
        self.credentials_invalid = False
        
        # Session-level realized profit tracking
        self.total_trade_profit = 0.0  # Cumulative profit from winning trades
        self.total_trade_loss = 0.0    # Cumulative loss from losing trades
        self.net_trade_profit = 0.0    # Net realized profit (profit - loss)
    
        # Instance-specific OKX State
        self.server_time_offset = 0
        self.okx_api_key = ""
        self.okx_api_secret = ""
        self.okx_passphrase = ""
        self.okx_simulated_trading_header = {}
        self.product_info = {
            "pricePrecision": None,
            "qtyPrecision": None,
            "priceTickSize": None,
            "minOrderQty": None,
            "contractSize": None,
        }.copy()
        self.okx_rest_api_base_url = "https://www.okx.com" # Assuming this was a global constant

        self.confirmed_subscriptions = set()
        
        # Initialize persistent analytics
        self.analytics_path = "analytics.json"
        self.total_trade_profit = 0.0
        self.total_trade_loss = 0.0
        self.net_trade_profit = 0.0
        self.daily_reports = []
        self._load_analytics()
        # self._load_position_state() # REMOVED: Live Data Only

        # Concurrency Guards for Authoritative Exit
        self.exit_lock = threading.Lock()
        self.authoritative_exit_in_progress = False

        # Initialize rate limiter for API request throttling (RESTORED)
        self.rate_limiter = RateLimiter()

        self.intervals = {
            '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
            '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
            '12h': 43200, '1d': 86400, '1w': 604800, '1M': 2592000
        }
        
    def log(self, message, level='info', to_file=False, filename=None):
        # Map levels to numerical priorities
        LEVEL_MAP = {
            'debug': 10,
            'info': 20,
            'warning': 30,
            'error': 40,
            'critical': 50
        }
        
        # Get configured log level from config
        configured_level_str = self.config.get('log_level', 'info').lower()
        configured_level = LEVEL_MAP.get(configured_level_str, 20)
        current_level = LEVEL_MAP.get(level.lower(), 20)
        
        # If the level of this message is lower than the configured level, skip it
        if current_level < configured_level:
            return
            
        # Suppress non-critical logs if credentials are known to be invalid
        if self.credentials_invalid and level.lower() != 'critical':
            return

        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = {'timestamp': timestamp, 'message': message, 'level': level}
        
        # Always append to console_logs for internal history if it passed the filter
        self.console_logs.append(log_entry)
        
        # Emit to the frontend
        self.emit('console_log', log_entry)
        
        # Write to standard logger (Console/File handling now managed at root level)
        if level == 'info':
            logging.info(message)
        elif level == 'warning':
            logging.warning(message)
        elif level == 'error':
            logging.error(message)
        elif level == 'debug':
            logging.debug(message)
        elif level == 'critical':
            logging.critical(message)
    
    def check_credentials(self):
        """Verifies if the current API credentials are valid and configured."""
        self._apply_api_credentials()
        
        if not self.okx_api_key or not self.okx_api_secret or not self.okx_passphrase:
            return False, "API Key, Secret, or Passphrase missing for selected mode."
        
        try:
            path = "/api/v5/account/balance"
            params = {"ccy": "USDT"}
            # Use max_retries=1 to fail quickly if invalid
            response = self._okx_request("GET", path, params=params, max_retries=1)
            
            if response and response.get('code') == '0':
                return True, "Credentials valid."
            elif response and response.get('code') == '50110': # Invalid API key
                return False, "Invalid API credentials."
            elif response and response.get('msg'):
                return False, f"API Error: {response.get('msg')}"
            else:
                return False, "Unknown API error during validation."
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def start(self, passive_monitoring=False):
        if self.is_running and not passive_monitoring:
            self.log('Bot is already trading', 'warning')
            return
        
        if not passive_monitoring:
            self.log('Bot starting trading logic...', 'info')
            # Reset session-based trade metrics for a clean start
            self.total_trade_profit = 0.0
            self.total_trade_loss = 0.0
            self.net_trade_profit = 0.0
            self.total_trades_count = 0
            self.log('Session trade metrics reset.', 'info')
        else:
            self.log('Bot starting background monitoring...', 'info')
        
        # 0. Apply Credentials
        self._apply_api_credentials()
        
        # 0.1 Perform initial credential validity check
        # Validation: check if credentials are provided first
        if not self.okx_api_key or not self.okx_api_secret or not self.okx_passphrase:
            self.log("⚠️ API Credentials not configured for the selected mode.", "error")
            self.credentials_invalid = True
            if not passive_monitoring:
                self.emit('error', {'message': 'API Credentials not configured.'})
                self.is_running = False
            return

        self.log("Verifying API credentials...", level="debug")
        valid, msg = self.check_credentials()
        if not valid:
            # We only set credentials_invalid if it was an auth error (handled in _okx_request)
            # or if it's explicitly an auth-related message
            if any(err in msg.lower() for err in ['invalid', 'credentials', 'key', 'secret', 'passphrase', '401']):
                self.credentials_invalid = True
                
            if not self.credentials_invalid:
                 # If it's a network error, we don't set credentials_invalid, 
                 # but we might still want to stop the bot from starting if not passive
                 self.log(f"⚠️ API Connection/Verification Failed: {msg}", "error")
            else:
                 self.log(f"⚠️ API Credentials Verification Failed: {msg}", "error")

            if not passive_monitoring:
                self.emit('error', {'message': f'API Credentials Error: {msg}'})
                self.is_running = False
            return

        # New initialization sequence for OKX
        if not self._sync_server_time():
            self.log("Failed to synchronize server time. Please check network connection or API.", 'error')
            if not passive_monitoring:
                self.is_running = False
            self.emit('bot_status', {'running': False})
            return
        
        if not self._fetch_product_info(self.config['symbol']):
            self.log("Failed to fetch product info. Exiting.", 'error')
            if not passive_monitoring:
                self.is_running = False
            self.emit('bot_status', {'running': False})
            return
 
        if not passive_monitoring:
            # 1. Position Mode Sync (Must happen BEFORE leverage)
            target_pos_mode = self.config.get('okx_pos_mode', 'net_mode')
            if not self._okx_set_position_mode(target_pos_mode):
                 self.log("Failed to verify/set position mode. Exiting.", 'error')
                 self.is_running = False
                 self.emit('bot_status', {'running': False})
                 return

            # 2. Leverage Sync (Requires posSide in Hedge Mode)
            lev_val = self.config.get('leverage', 20)
            symbol = self.config['symbol']
            lev_success = False
            
            if target_pos_mode == 'long_short_mode':
                # Set for both sides in hedge mode
                l_ok = self._okx_set_leverage(symbol, lev_val, pos_side="long")
                s_ok = self._okx_set_leverage(symbol, lev_val, pos_side="short")
                lev_success = l_ok and s_ok
            else:
                # Set for net side in one-way mode
                lev_success = self._okx_set_leverage(symbol, lev_val, pos_side="net")

            if not lev_success:
                self.log("Failed to set leverage. Exiting.", 'error')
                self.is_running = False
                self.emit('bot_status', {'running': False})
                return
        
            self.log("Checking for and closing any existing open positions...", level="info")
            self._check_and_close_any_open_position()

        # Mark as running ONLY after all initialization is complete
        if not passive_monitoring:
            self.is_running = True
        
        self.bot_start_time = int(time.time() * 1000) # Reset session start time

        # Check if threads are already running
        public_alive = getattr(self, 'ws_public_thread', None) and self.ws_public_thread.is_alive()
        private_alive = getattr(self, 'ws_private_thread', None) and self.ws_private_thread.is_alive()
        main_alive = getattr(self, 'ws_thread', None) and self.ws_thread.is_alive()

        if public_alive or private_alive or main_alive:
            # If the symbol has changed, we need to restart the WebSocket
            if getattr(self, 'subscribed_symbol', None) != self.config.get('symbol'):
                 self.log(f"Symbol changed to {self.config.get('symbol')}, restarting WebSockets...", level="info")
                 try:
                     if self.ws_public: self.ws_public.close()
                     if self.ws_private: self.ws_private.close()
                 except:
                     pass
            else:
                 self.log("WebSocket and Management threads are already active. Re-applied any credential changes.", level="debug")
                 return

        self.log('Bot initialized. Starting live connection threads...', 'info')
        self.stop_event.clear()
        self.ws_thread = threading.Thread(target=self._initialize_websocket_and_start_main_loop, daemon=True)
        self.ws_thread.start()

        # Start unified management thread (Account Info + Cancellation)
        # Note: In the previous code, this was started in start(), but it should be part of the thread group
        if not getattr(self, 'mgmt_thread', None) or not self.mgmt_thread.is_alive():
            self.mgmt_thread = threading.Thread(target=self._unified_management_loop, daemon=True)
            self.mgmt_thread.start()
    
    def stop(self):
        if not self.is_running:
            self.log('Bot trading is not active', 'warning')
            return
        
        self.is_running = False
        self.log('Bot trading logic paused. Background monitoring remains active.', 'info')
        
        # We no longer set stop_event or close WS here to allow background Auto-Exit to work
        self.emit('bot_status', {'running': False})

    def shutdown(self):
        """Truly stops all threads and connections."""
        self.is_running = False
        self.stop_event.set()
        try:
            if self.ws_public: self.ws_public.close()
            if self.ws_private: self.ws_private.close()
        except:
            pass
        self.log('Bot fully shut down.', 'info')
    
    def _load_config(self):
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                # Ensure new config parameters have default values if not present
                config.setdefault('max_allowed_used', 1000.0)
                config.setdefault('cancel_on_tp_price_below_market', True)
                config.setdefault('cancel_on_entry_price_below_market', True)
                config.setdefault('cancel_on_tp_price_above_market', True)
                config.setdefault('cancel_on_entry_price_above_market', True)
                config.setdefault('websocket_timeframes', ['1m', '5m']) # Add default for websocket_timeframes
                config.setdefault('direction', 'long')
                config.setdefault('mode', 'cross') # Changed default mode to 'cross'
                config.setdefault('tp_amount', 0.5)
                config.setdefault('sl_amount', 1.0)
                config.setdefault('trigger_price', 'last')
                config.setdefault('tp_mode', 'limit')
                config.setdefault('tp_type', 'oco')
                config.setdefault('use_candlestick_conditions', True) # New parameter
                config.setdefault('okx_demo_api_key', '')
                config.setdefault('okx_demo_api_secret', '')
                config.setdefault('okx_demo_api_passphrase', '')
                config.setdefault('use_chg_open_close', False)
                config.setdefault('min_chg_open_close', 0)
                config.setdefault('max_chg_open_close', 0)
                config.setdefault('use_chg_high_low', False)
                config.setdefault('min_chg_high_low', 0)
                config.setdefault('max_chg_high_low', 0)
                config.setdefault('use_chg_high_close', False)
                config.setdefault('min_chg_high_close', 0)
                config.setdefault('max_chg_high_close', 0)
                config.setdefault('candlestick_timeframe', '1m')
                config.setdefault('initial_total_capital', 0.0) # New: Default for initial total capital
                config.setdefault('log_level', 'info') # New: Default for log level
                config.setdefault('use_auto_margin', False)
                config.setdefault('auto_margin_offset', 30.0)
                config.setdefault('use_add_pos_auto_cal', False)
                config.setdefault('add_pos_recovery_percent', 0.6)
                config.setdefault('add_pos_profit_multiplier', 1.5)
                config.setdefault('use_add_pos_above_zero', False)
                config.setdefault('use_add_pos_profit_target', False)
                return config
        except FileNotFoundError:
            self.log(f"Config file not found: {self.config_path}", 'error')
            raise
        except json.JSONDecodeError as e:
            self.log(f"Error decoding config file {self.config_path}: {e}", 'error')
            raise
        except Exception as e:
            self.log(f"An unexpected error occurred while loading config: {e}", 'error')
            raise

    # ================================================================================
    # OKX API Helper Functions (Adapted as methods)
    # ================================================================================

    def _apply_api_credentials(self):
        """Applies configured API credentials to instance variables."""
        self.credentials_invalid = False
        use_dev = self.config.get('use_developer_api', False)
        use_demo = self.config.get('use_testnet', False)

        if use_dev:
            if use_demo:
                self.okx_api_key = self.config.get('dev_demo_api_key', '')
                self.okx_api_secret = self.config.get('dev_demo_api_secret', '')
                self.okx_passphrase = self.config.get('dev_demo_api_passphrase', '')
            else:
                self.okx_api_key = self.config.get('dev_api_key', '')
                self.okx_api_secret = self.config.get('dev_api_secret', '')
                self.okx_passphrase = self.config.get('dev_passphrase', '')
        else:
            if use_demo:
                self.okx_api_key = self.config.get('okx_demo_api_key', '')
                self.okx_api_secret = self.config.get('okx_demo_api_secret', '')
                self.okx_passphrase = self.config.get('okx_demo_api_passphrase', '')
            else:
                self.okx_api_key = self.config.get('okx_api_key', '')
                self.okx_api_secret = self.config.get('okx_api_secret', '')
                self.okx_passphrase = self.config.get('okx_passphrase', '')

        if use_demo:
            self.okx_simulated_trading_header = {'x-simulated-trading': '1'}
        else:
            self.okx_simulated_trading_header = {}
        self.log(f"API Credentials Applied: {'Developer' if use_dev else 'User'} | {'Demo' if use_demo else 'Live'}", level="debug")

    def _save_config(self):
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.config, f, indent=2)
            self.log(f"Config saved to {self.config_path}", level="debug")
        except Exception as e:
            self.log(f"Error saving config: {e}", level="error")

    def _okx_request(self, method, path, params=None, body_dict=None, max_retries=3):
        if self.credentials_invalid:
            return None
            
        local_dt = datetime.now(timezone.utc)
        adjusted_dt = local_dt + timedelta(milliseconds=self.server_time_offset)
        timestamp = adjusted_dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        body_str = ''
        if body_dict:
            body_str = json.dumps(body_dict, separators=(',', ':'), sort_keys=True)

        request_path_for_signing = path
        final_url = f"{self.okx_rest_api_base_url}{path}" 

        if params and method.upper() == 'GET':
            query_string = '?' + '&'.join([f'{k}={v}' for k, v in sorted(params.items())])
            request_path_for_signing += query_string
            final_url += query_string

        signature = generate_okx_signature(self.okx_api_secret, timestamp, method, request_path_for_signing, body_str)

        headers = {
            "OK-ACCESS-KEY": self.okx_api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.okx_passphrase,
            "Content-Type": "application/json"
        }

        headers.update(self.okx_simulated_trading_header)

        for attempt in range(max_retries):
            if self.credentials_invalid:
                return None

            try:
                # Acquire rate limit token before making request
                self.rate_limiter.acquire(path)
                
                req_func = getattr(requests, method.lower(), None)
                if not req_func:
                    self.log(f"Unsupported HTTP method: {method}", level="error")
                    return None

                kwargs = {'headers': headers, 'timeout': 15}

                if body_dict and method.upper() in ['POST', 'PUT', 'DELETE']:
                    kwargs['data'] = body_str

                self.log(f"{method} {path} (Attempt {attempt + 1}/{max_retries})", level="debug")
                response = req_func(final_url, **kwargs)

                if response.status_code != 200:
                    try:
                        error_json = response.json()
                        okx_error_code = error_json.get('code')
                        
                        # Check for invalid credential error codes
                        if okx_error_code in ['50110', '50111', '50113'] or response.status_code == 401:
                            if not self.credentials_invalid:
                                self.log(f"CRITICAL: Invalid API credentials detected (Status={response.status_code}, Code={okx_error_code}). Suppressing further API errors.", level="critical")
                                self.credentials_invalid = True
                            return error_json

                        if not self.credentials_invalid:
                            self.log(f"API Error: Status={response.status_code}, Code={okx_error_code}, Msg={error_json.get('msg')}. Full Response: {error_json}", level="error")
                        
                        if okx_error_code:
                            return error_json
                    except json.JSONDecodeError:
                        if not self.credentials_invalid:
                            self.log(f"API Error: Status={response.status_code}, Response: {response.text}", level="error")

                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None

                try:
                    json_response = response.json()
                    if json_response.get('code') != '0':
                        self.log(f"OKX API returned non-zero code: {json_response.get('code')} Msg: {json_response.get('msg')} for {method} {path}. Full Response: {json_response}", level="debug")
                    self.log(f"DEBUG: Full OKX API Response for {method} {path}: {json_response}", level="debug") # Log full response
                    return json_response
                except json.JSONDecodeError:
                    self.log(f"Failed to decode JSON for {method} {path}. Status: {response.status_code}, Resp: {response.text}", level="error")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None

            except requests.exceptions.Timeout:
                self.log(f"API request timeout (Attempt {attempt + 1}/{max_retries})", level="error")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except requests.exceptions.RequestException as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                err_text = e.response.text[:200] if e.response is not None else 'No response text'
                self.log(f"OKX API HTTP Error ({method} {path}): Status={status_code}, Error={e}. Response: {err_text}", level="error")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except Exception as e:
                self.log(f"Unexpected error during OKX API request ({method} {path}): {e}", level="error")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
        return None

    def _fetch_historical_data_okx(self, symbol, timeframe, start_ts_ms, end_ts_ms):
        try:
            path = "/api/v5/market/history-candles"

            okx_timeframe_map = {
                '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m', '30m': '30m',
                '1h': '1H', '2h': '2H', '4h': '4H', '6h': '6H', '8h': '8H',
                '12h': '12H', '1d': '1D', '1w': '1W', '1M': '1M'
            }
            okx_timeframe = okx_timeframe_map.get(timeframe)

            if not okx_timeframe:
                self.log(f"Invalid timeframe for OKX: {timeframe}", level="error")
                return []

            all_data = []
            max_candles_limit = 100

            current_cursor_ms = end_ts_ms

            self.log(f"Fetching historical data for {symbol} ({timeframe})", level="debug")

            while True:
                params = {
                    "instId": symbol,
                    "bar": okx_timeframe,
                    "limit": str(max_candles_limit),
                    "after": str(current_cursor_ms + 1)
                }

                response = self._okx_request("GET", path, params=params)
                
                if response and response.get('code') == '0':
                    rows = response.get('data', [])
                    if rows:
                        self.log(f"Fetched {len(rows)} candles for {timeframe}", level="debug")
                        parsed_klines = []
                        for kline in rows:
                            try:
                                parsed_klines.append([
                                    int(kline[0]),
                                    float(kline[1]),
                                    float(kline[2]),
                                    float(kline[3]),
                                    float(kline[4]),
                                    float(kline[5])
                                ])
                            except (ValueError, TypeError, IndexError) as e:
                                self.log(f"Error parsing OKX kline: {kline} - {e}", level="error")
                                continue
                        
                        all_data.extend(parsed_klines)
                        
                        oldest_ts = int(rows[-1][0])
                        current_cursor_ms = oldest_ts

                        if oldest_ts <= start_ts_ms or len(rows) < max_candles_limit:
                            break 
                    else:
                        break 

                    time.sleep(0.3)
                else:
                    self.log(f"Error fetching OKX klines: {response}", level="error")
                    return []
            
            final_data = pd.DataFrame(all_data, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
            if not final_data.empty:
                final_data = final_data.drop_duplicates(subset=['Timestamp'])
                final_data = final_data[final_data['Timestamp'] >= start_ts_ms]
                final_data = final_data.sort_values(by='Timestamp', ascending=True)
                return final_data.values.tolist()
            else:
                return []
        except Exception as e:
            self.log(f"Exception in _fetch_historical_data_okx: {e}", level="error")
            return []

    def _sync_server_time(self):
        """Synchronizes server time and updates instance offset."""
        try:
            response = requests.get(f"{self.okx_rest_api_base_url}/api/v5/public/time", timeout=5)
            response.raise_for_status()
            json_response = response.json()
            if json_response.get('code') == '0' and json_response.get('data'):
                server_timestamp_ms = int(json_response['data'][0]['ts'])
                local_timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                self.server_time_offset = server_timestamp_ms - local_timestamp_ms
                self.log(f"OKX server time synchronized. Offset: {self.server_time_offset}ms", level="info")
                return True
            else:
                self.log(f"Failed to get OKX server time: {json_response.get('msg', 'Unknown error')}", level="error")
                return False
        except requests.exceptions.RequestException as e:
            self.log(f"Error fetching OKX server time: {e}", level="error")
            return False
        except Exception as e:
            self.log(f"Unexpected error in _sync_server_time: {e}", level="error")
            return False

    def _fetch_product_info(self, target_symbol):
        try:
            path = "/api/v5/public/instruments"
            params = {"instType": "SWAP", "instId": target_symbol}
            response = self._okx_request("GET", path, params=params)

            if response and response.get('code') == '0':
                product_data = None
                if isinstance(response.get('data'), list):
                    for item in response['data']:
                        if item.get('instId') == target_symbol:
                            product_data = item
                            break
                elif isinstance(response.get('data'), dict) and response.get('data').get('instId') == target_symbol:
                    product_data = response.get('data')

                if not product_data:
                    self.log(f"Product {target_symbol} not found in OKX instruments response.", level="error")
                    return False

                self.product_info['priceTickSize'] = safe_float(product_data.get('tickSz'))
                self.product_info['qtyPrecision'] = int(np.abs(np.log10(safe_float(product_data.get('lotSz'))))) if safe_float(product_data.get('lotSz')) > 0 else 0
                self.product_info['pricePrecision'] = int(np.abs(np.log10(safe_float(product_data.get('tickSz'))))) if safe_float(product_data.get('tickSz')) > 0 else 0
                self.product_info['qtyStepSize'] = safe_float(product_data.get('lotSz'))
                self.product_info['minOrderQty'] = safe_float(product_data.get('minSz'))

                self.product_info['contractSize'] = safe_float(product_data.get('ctVal', '1'), 1.0)

                self.log(f"Product specifications for {target_symbol} initialized.", level="debug")
                return True
            else:
                self.log(f"Failed to fetch product info for {target_symbol} (code: {response.get('code') if response else 'N/A'}, msg: {response.get('msg') if response else 'N/A'})", level="error")
                return False
        except Exception as e:
            self.log(f"Exception in fetch_product_info: {e}", level="error")
            return False

    def _okx_set_leverage(self, symbol, leverage_val, pos_side="net"):
        try:
            path = "/api/v5/account/set-leverage"
            body = {
                "instId": symbol,
                "lever": str(int(leverage_val)),
                "mgnMode": self.config.get('mode', 'cross'), # Use mode from config
                "posSide": pos_side
            }

            self.log(f"Setting leverage to {leverage_val}x for {symbol} ({pos_side})", level="debug")
            response = self._okx_request("POST", path, body_dict=body)

            if response and response.get('code') == '0':
                self.log(f"[OK] Leverage set to {leverage_val}x for {symbol} ({pos_side})", level="info")
                return True
            else:
                self.log(f"Failed to set leverage for {symbol}: {response.get('msg') if response else 'No response'}", level="error")
                return False
        except Exception as e:
            self.log(f"Exception in okx_set_leverage: {e}", level="error")
            return False

    def _okx_set_position_mode(self, mode_val):
        try:
            # 1. First, check CURRENT position mode to avoid unnecessary errors
            path_get = "/api/v5/account/config"
            get_response = self._okx_request("GET", path_get)
            
            if get_response and get_response.get('code') == '0':
                current_mode = get_response['data'][0].get('posMode')
                if current_mode == mode_val:
                    self.log(f"[OK] Position mode already confirmed: {mode_val}", level="debug")
                    return True
                else:
                    self.log(f"Position mode mismatch (Current: {current_mode}, Target: {mode_val}). Attempting update...", level="info")
            
            # 2. Update if needed
            # Mode options: 'net_mode' (One-way) or 'long_short_mode' (Hedge)
            path_set = "/api/v5/account/set-position-mode"
            body = {"posMode": mode_val}
            
            self.log(f"Setting account position mode to {mode_val}... (Requires 0 positions/orders)", level="debug")
            response = self._okx_request("POST", path_set, body_dict=body)
            
            if response and response.get('code') == '0':
                self.log(f"[OK] Position mode set to {mode_val}", level="info")
                return True
            elif response and response.get('code') == '51000': # Already in this mode (backup check)
                self.log(f"[OK] Position mode already confirmed: {mode_val}", level="debug")
                return True
            else:
                self.log(f"Failed to set position mode: {response.get('msg') if response else 'No response'}", level="error")
                return False
        except Exception as e:
            self.log(f"Exception in _okx_set_position_mode: {e}", level="error")
            return False

    def _get_ws_url(self, type='public'):
        # Dynamic URL: Production vs Demo
        base_url = "wss://ws.okx.com:8443"
        if self.config.get('use_testnet'):
            base_url = "wss://wspap.okx.com:8443"
            self.log(f"Using OKX Demo WebSocket ({base_url}) - {type.capitalize()}", level="debug")
        
        if type == 'private':
            return f"{base_url}/ws/v5/private"
        return f"{base_url}/ws/v5/public"

    def _on_websocket_message(self, ws_app, message):
        # Removed raw message logging to reduce clutter
        try:
            msg = json.loads(message)
            # self.log(f"DEBUG: _on_websocket_message received parsed message: {msg}", level="debug")

            # Identify which WS this is
            ws_type = 'private' if ws_app == self.ws_private else 'public'

            # Handle event messages (subscribe, login, error)
            if 'event' in msg:
                event_type = msg.get('event')
                if event_type == 'subscribe':
                    arg = msg.get('arg', {})
                    channel = arg.get('channel')
                    inst_id = arg.get('instId', 'N/A')
                    channel_id = f"{channel}:{inst_id}"
                    self.log(f"Subscription confirmed ({ws_type}): {channel_id}", level="debug")
                    self.confirmed_subscriptions.add(channel_id)
                    if self.pending_subscriptions == self.confirmed_subscriptions:
                        self.log("All WebSocket subscriptions are ready.", level="debug")
                        # Note: We might want a dual-ready check, but for now this is fine.
                        self.ws_subscriptions_ready.set()
                elif event_type == 'login':
                     if msg.get('code') == '0':
                         self.log("WebSocket Login Successful.", level="info")
                         self._on_login_success(ws_app)
                     else:
                         self.log(f"WebSocket Login Failed ({ws_type}): {msg}", level="error")
                elif event_type == 'error':
                    # Log error but don't necessarily crash
                    self.log(f"WebSocket Error Event ({ws_type}): {msg}", level="error")
                else: 
                    # Filter out noisy 'channel-conn-count' messages
                    if event_type != 'channel-conn-count':
                        self.log(f"Received non-subscribe event ({ws_type}): {msg}", level="debug")
                # Do NOT return here, allow further processing if it's a data message that also has an event.
            
            if 'data' in msg:
                channel = msg.get('arg', {}).get('channel', '')
                data = msg.get('data', [])

                if channel == 'trades' and data:
                    with self.trade_data_lock:
                        self.latest_trade_timestamp = int(data[-1].get('ts'))
                        self.latest_trade_price = safe_float(data[-1].get('px'))
                        self.last_price_update_time = time.time()

                elif channel == 'tickers' and data:
                    # Process ticker data to update latest_trade_price
                    # The `last` field from ticker data represents the current price
                    self.latest_trade_price = safe_float(data[0].get('last'))
                    self.last_price_update_time = time.time()
                    # No need to update historical data store from tickers channel

                elif channel == 'positions' and data:
                    self._process_position_push(data)

                elif channel == 'account' and data:
                    # Update Balance & Equity in real-time
                    with self.account_info_lock:
                        acc_data = data[0]
                        self.total_equity = safe_float(acc_data.get('totalEq'))
                        # OKX 'account' push contains a list of details for each currency
                        details = acc_data.get('details', [])
                        for det in details:
                            if det.get('ccy') == 'USDT':
                                self.account_balance = safe_float(det.get('bal'))
                                self.available_balance = safe_float(det.get('availBal'))
                                # Update timestamp to skip REST sync if WS is active
                                self.last_account_sync_time = time.time()
                                self.log(f"WS Balance Updated | Equity: ${self.total_equity:.2f} | Balance: ${self.account_balance:.2f}", level="debug")
                                break

                # ---------------------------------------------------------
                # REAL-TIME UI UPDATES (TRANSITIONAL)
                # ---------------------------------------------------------
                # Triggered on every price tick to keep the dashboard responsive
                if self.latest_trade_price:
                    now = time.time()
                    # Throttle emissions to max 2 per second to prevent UI flooding
                    # However, if we just got a position update, emit immediately?
                    # Let's keep the throttle but maybe force emit on position change in _process_position_push
                    if now - getattr(self, 'last_emit_time', 0) >= 0.5:
                        self.last_emit_time = now
                        self.safe_emit('price_update', {'price': self.latest_trade_price, 'symbol': self.config['symbol']})
                # ---------------------------------------------------------

        except json.JSONDecodeError:
            self.log(f"DEBUG: Non-JSON WebSocket message received: {message[:500]}", level="debug")
        except Exception as e:
            self.log(f"Exception in on_websocket_message: {e}", level="error")

    def _on_websocket_open(self, ws_app, type):
        self.log(f"OKX WebSocket connection opened ({type}).", level="info")
        if type == 'public':
            self._send_websocket_subscriptions(ws_app)
        else:
            self._send_login_request(ws_app)

    def _on_websocket_error(self, ws_app, error):
        ws_type = 'private' if ws_app == self.ws_private else 'public'
        self.log(f"WebSocket Error ({ws_type}): {error}", level="error")

    def _on_websocket_close(self, ws_app, close_status_code, close_msg):
        # Determine type from instance comparison or just log context-free
        ws_type = 'private' if ws_app == self.ws_private else 'public'
        self.log(f"OKX WebSocket connection closed ({ws_type}). Code: {close_status_code}, Msg: {close_msg}", level="info")
        if ws_type == 'public': self.ws_public = None
        else: self.ws_private = None

    def _send_login_request(self, ws_app):
        try:
             timestamp = str(time.time())
             # Use the global generate_okx_signature helper
             # FIX: Use correct attribute names self.okx_api_secret etc.
             sign = generate_okx_signature(self.okx_api_secret, timestamp, 'GET', '/users/self/verify', '')
             
             login_payload = {
                 "op": "login",
                 "args": [
                     {
                         "apiKey": self.okx_api_key,
                         "passphrase": self.okx_passphrase,
                         "timestamp": timestamp,
                         "sign": sign
                     }
                 ]
             }
             ws_app.send(json.dumps(login_payload))
             self.log("Sent WebSocket Login Request (Private).", level="debug")
        except Exception as e:
             self.log(f"Error sending login request: {e}", level="error")

    def _on_login_success(self, ws_app):
        # Subscribe to private channels (positions)
        self.log("Subscribing to private channels...", level="info")
        channels = [
            {"channel": "positions", "instType": "SWAP", "instId": self.config['symbol']},
            {"channel": "account", "ccys": ["USDT"]}, # Live balance and equity updates
            # Add 'orders' channel later if needed for real-time order updates
        ]
        
        subscription_payload = {
            "op": "subscribe",
            "args": channels
        }
        ws_app.send(json.dumps(subscription_payload))
        self.log(f"Sent private subscription request for {len(channels)} channels.", level="debug")
        
        # Add to pending subscriptions so readiness check waits for them too
        new_subs = {f"{arg.get('channel')}:{arg.get('instId', 'N/A')}" for arg in channels}
        self.pending_subscriptions.update(new_subs)

    def _send_websocket_subscriptions(self, ws_app):
        self.subscribed_symbol = self.config['symbol']
        channels = [
            {"channel": "trades", "instId": self.subscribed_symbol},
            {"channel": "tickers", "instId": self.subscribed_symbol}, # Public tickers channel for real-time price
        ]
        
        # Temporarily removed candle subscriptions until correct format for ETH-USDT-SWAP is confirmed
 
        subscription_payload = {
            "op": "subscribe",
            "args": channels
        }
        self.log(f"WS Sending public subscription request: {json.dumps(subscription_payload)}", level="debug")
        ws_app.send(json.dumps(subscription_payload))
        self.log(f"WS Sent public subscription request for {len(channels)} channels.", level="debug")
        
        # Populate pending_subscriptions with the channels we just sent
        new_pending = {f"{arg.get('channel')}:{arg.get('instId', 'N/A')}" for arg in channels}
        self.pending_subscriptions.update(new_pending)

    def _process_position_push(self, positions_data):
        """
        Updates internal position state from WebSocket 'positions' channel data.
        This provides Real-Time PnL and Status updates as requested.
        """
        try:
             with self.position_lock:
                 contract_size = self.product_info.get('contractSize', 1.0)
                 prev_qtys = {k: v for k, v in self.position_qty.items()}
                 
                 for pos in positions_data:
                     # Filter by symbol
                     if pos.get('instId') != self.config['symbol']:
                         continue

                     # Identify side
                     raw_side = pos.get('posSide', 'net')
                     side_key = 'long'
                     if raw_side == 'short': side_key = 'short'
                     elif raw_side == 'net':
                         side_key = self.config.get('direction', 'long')
                         if side_key == 'both': side_key = 'long'
                     
                     # Calculate Size
                     try:
                         pos_sz_raw = safe_float(pos.get('pos'))
                     except:
                         pos_sz_raw = 0.0
                     
                     new_qty = pos_sz_raw * contract_size
                     
                     # Check for update
                     if abs(new_qty - prev_qtys.get(side_key, 0.0)) > 0.000001:
                         self.log(f"WS Position update [{side_key.upper()}]: {prev_qtys.get(side_key, 0.0)} -> {new_qty}. (Live)", level="debug")
                         
                         # Detect Closure (Qty became 0)
                         if new_qty == 0 and abs(prev_qtys.get(side_key, 0.0)) > 0:
                             close_reason = "Exchange Update (Closed)"
                             if self.authoritative_exit_in_progress:
                                 close_reason = "Authoritative Exit"
                             else:
                                 # Try to infer reason
                                 mkt = self.latest_trade_price or safe_float(pos.get('avgPx'))
                                 tp = self.current_take_profit.get(side_key, 0)
                                 sl = self.current_stop_loss.get(side_key, 0)
                                 if tp > 0 and mkt > 0 and abs(mkt - tp) / tp < 0.005: 
                                     close_reason = "Exchange Hit (Take Profit)"
                                 elif sl > 0 and mkt > 0 and abs(mkt - sl) / sl < 0.005:
                                     close_reason = "Exchange Hit (Stop Loss)"
                             
                             self.log("=" * 60, level="info")
                             self.log(f"[DONE] Close Position (WS): {side_key.upper()} | Reason: {close_reason}", level="info")
                             self.log("=" * 60, level="info")
                             self.last_close_time[side_key] = time.time()
                             
                             # [SESSION TRACKING] Reset bot-initiated volume on closure
                             # We reset used notional so the next entry starts fresh at $0.00.
                             with self.position_lock:
                                 self.log(f"Session Tracking: Position closed. Resetting Bot Used Notional ($0.00).", level="info")
                                 self.session_bot_used_notional = 0.0
                     
                     # Update State for this specific position
                     self.in_position[side_key] = (abs(new_qty) > 0)
                     self.position_entry_price[side_key] = safe_float(pos.get('avgPx'))
                     self.position_qty[side_key] = new_qty
                     self.position_liq[side_key] = safe_float(pos.get('liqp', '0'))
                     
                     # Merge with existing details or overwrite
                     self.position_details[side_key] = pos
                     
                     # Update last_add_price if manually added
                     if self.last_add_price == 0 and abs(new_qty) > 0:
                         self.last_add_price = safe_float(pos.get('avgPx'))
                     
                 # ---------------------------------------------------------
                 # RECALCULATE AGGREGATE METRICS FROM STATE
                 # ---------------------------------------------------------
                 # Iterate over ALL known positions to get the total PnL/Size.
                 # This ensures that if we get a partial update, we still sum up correctly.
                 
                 rec_unrealized_pnl = 0.0
                 rec_pos_notional = 0.0
                 rec_active_count = 0
                 
                 for s_key in ['long', 'short']:
                     if s_key in self.position_details:
                         p_det = self.position_details[s_key]
                         
                         sz_raw = safe_float(p_det.get('pos', 0))
                         if abs(sz_raw) > 0:
                             rec_active_count += 1
                             rec_unrealized_pnl += safe_float(p_det.get('upl', 0))
                             
                             # Calculate Notional
                             avg_px = safe_float(p_det.get('avgPx'))
                             price_to_use = self.latest_trade_price if self.latest_trade_price else avg_px
                             rec_pos_notional += abs(sz_raw) * price_to_use * contract_size
                 
                 # UPDATE CACHED METRICS
                 self.cached_unrealized_pnl = rec_unrealized_pnl
                 self.cached_pos_notional = rec_pos_notional
                 self.cached_active_positions_count = rec_active_count
                 # self.cached_used_notional = rec_pos_notional # REMOVED: Managed by session tracking now

                 # Force immediate UI emit to show the new PnL
                 self._emit_socket_updates()

        except Exception as e:
             self.log(f"Error processing WS position push: {e}", level="error")

    def _on_websocket_error(self, ws_app, error):
        self.log(f"OKX WebSocket error: {error}", level="error")

    def _on_websocket_close(self, ws_app, close_status_code, close_msg):
        self.log(f"OKX WebSocket closed. Status: {close_status_code}, Msg: {close_msg}", level="debug")
        # No longer spawning a new thread here. 
        # The reconnection is now handled by the loop in _initialize_websocket_and_start_main_loop.

    def _fetch_initial_historical_data(self, symbol, timeframe, start_date_str, end_date_str):
        with self.data_lock:
            try:
                # Flexible parsing for Date or DateTime
                def parse_dt(dt_str):
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                        try:
                            return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
                        except ValueError:
                            continue
                    raise ValueError(f"Time format not supported: {dt_str}")

                start_dt = parse_dt(start_date_str)
                start_ts_ms = int(start_dt.timestamp() * 1000)
                end_dt = parse_dt(end_date_str)
                end_ts_ms = int(end_dt.timestamp() * 1000)

                raw_data = self._fetch_historical_data_okx(symbol, timeframe, start_ts_ms, end_ts_ms)

                if raw_data:
                    df = pd.DataFrame(raw_data, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                    df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'], inplace=True)

                    if df.empty:
                        self.log(f"No valid data for {timeframe}", level="error")
                        return False

                    invalid_rows = df[(df['Low'] > df['High']) |
                                    (df['Open'] < df['Low']) | (df['Open'] > df['High']) |
                                    (df['Close'] < df['Low']) | (df['Close'] > df['High'])]

                    if not invalid_rows.empty:
                        self.log(f"WARNING: Found {len(invalid_rows)} invalid OHLC rows", level="warning")
                        df = df[(df['Low'] <= df['High'])]

                    df['Datetime'] = pd.to_datetime(df['Timestamp'], unit='ms', utc=True)
                    df = df.set_index('Datetime')
                    df = df[~df.index.duplicated(keep='first')]
                    df = df.sort_index()

                    self.historical_data_store[timeframe] = df

                    self.log(f"Loaded {len(df)} candles for {timeframe}", level="debug")
                    return True
                else:
                    self.log(f"Failed to fetch data for {timeframe}", level="error")
                    return False
            except Exception as e:
                self.log(f"Exception in _fetch_initial_historical_data: {e}", level="error")
                return False

    def _okx_place_order(self, symbol, side, qty, price=None, order_type="Market",
                        time_in_force=None, reduce_only=False,
                        stop_loss_price=None, take_profit_price=None, posSide=None, verbose=True, tdMode=None):
        try:
            path = "/api/v5/trade/order"
            price_precision = self.product_info.get('pricePrecision', 4)
            qty_precision = self.product_info.get('qtyPrecision', 8)

            order_qty_str = f"{qty:.{qty_precision}f}"
            
            # Use provided tdMode or default to config
            trade_mode = tdMode if tdMode else self.config.get('mode', 'cross')

            body = {
                "instId": symbol,
                "tdMode": trade_mode,
                "side": side.lower(),
                "ordType": order_type.lower(),
                "sz": order_qty_str,
            }

            if (self.config.get('hedge_mode', False) or self.config.get('okx_pos_mode') == 'long_short_mode') and posSide:
                body["posSide"] = posSide

            if order_type.lower() == "limit" and price is not None:
                body["px"] = f"{price:.{price_precision}f}"

            if time_in_force:
                if time_in_force == "GoodTillCancel":
                    body["timeInForce"] = "GTC"
                else:
                    body["timeInForce"] = time_in_force

            if reduce_only:
                body["reduceOnly"] = True

            # Attach TP/SL via attachAlgoOrds (Correct V5 Structure)
            attach_algo_list = []
            algo_details = {}
            has_algo = False
            
            # Ensure posSide is passed to algo if present in parent order (Critical for Long/Short mode)
            if "posSide" in body:
                algo_details["posSide"] = body["posSide"]

            if take_profit_price and safe_float(take_profit_price) > 0:
                algo_details["tpTriggerPx"] = str(take_profit_price)
                algo_details["tpOrdPx"] = "-1" # Market TP
                algo_details["tpTriggerPxType"] = "last"
                has_algo = True

            if stop_loss_price and safe_float(stop_loss_price) > 0:
                algo_details["slTriggerPx"] = str(stop_loss_price)
                algo_details["slOrdPx"] = "-1" # Market SL
                algo_details["slTriggerPxType"] = "last"
                has_algo = True
                
            if has_algo:
                attach_algo_list.append(algo_details)
                body["attachAlgoOrds"] = attach_algo_list

            self.log(f"DEBUG: Order placement request body: {body}", level="debug")
            if verbose:
                self.log(f"Placing {order_type} {side} order for {order_qty_str} {symbol} at {price}", level="info")
            
            response = self._okx_request("POST", path, body_dict=body)

            if response and response.get('code') == '0':
                order_data = response.get('data', [])
                if order_data and order_data[0].get('ordId'):
                    if verbose:
                        self.log(f"[OK] Order placed: OrderID={order_data[0]['ordId']}", level="info")
                    
                    # [SESSION TRACKING] Increment bot-initiated volume
                    est_px = price if (price and price > 0) else self.latest_trade_price
                    if est_px and est_px > 0:
                        contract_size = self.product_info.get('contractSize', 1.0)
                        order_notional = qty * est_px * contract_size
                        with self.position_lock:
                            self.session_bot_used_notional += order_notional
                            self.log(f"Session Tracking: Added ${order_notional:.2f} to Bot Used Notional. Total: ${self.session_bot_used_notional:.2f}", level="debug")
                    
                    # Trigger immediate account refresh for UI responsiveness
                    def _update_after_reconnect():
                        try:
                            self._sync_account_data()
                            self._emit_socket_updates()
                        except Exception as e:
                            self.log(f"Error in reconnect update: {e}", level="error")
                    threading.Thread(target=_update_after_reconnect, daemon=True).start()
                    return order_data[0]
                else:
                    self.log(f"[FAIL] Order placement failed: No order ID in response. Response: {response}", level="error")
                    return None
            else:
                error_msg = response.get('msg', 'Unknown error') if response else 'No response'
                self.log(f"[FAIL] Order placement failed: {error_msg}. Response: {response}", level="error")
                return None
        except Exception as e:
            self.log(f"Exception in _okx_place_order: {e}", level="error")
            return None

    def _okx_place_algo_order(self, body, verbose=True):
        try:
            path = "/api/v5/trade/order-algo"
            if verbose:
                self.log(f"Placing algo order", level="info")
            response = self._okx_request("POST", path, body_dict=body)
            if response and response.get('code') == '0':
                data = response.get('data', [])
                if data and (data[0].get('algoId') or data[0].get('ordId')):
                    if verbose:
                        self.log(f"[OK] Algo order placed", level="info")
                    return data[0]
                else:
                    self.log(f"[FAIL] Algo order placed but no algoId/ordId returned: {response}", level="error")
                    return None
            else:
                self.log(f"[FAIL] Algo order failed: {response}", level="debug")
                return None
        except Exception as e:
            self.log(f"Exception in _okx_place_algo_order: {e}", level="debug")
            return None

    def _okx_cancel_order(self, symbol, order_id, reason=None):
        try:
            path = "/api/v5/trade/cancel-order"
            body = {
                "instId": symbol,
                "ordId": order_id,
            }

            log_msg = f"Cancelling OKX order {order_id[:12]}..."
            if reason:
                log_msg = f"Cancelling OKX order {order_id[:12]} ({reason})..."
            self.log(log_msg, level="info")
            response = self._okx_request("POST", path, body_dict=body)

            if response and response.get('code') == '0':
                self.log(f"[OK] Order cancelled", level="info")
                return True
            elif response and response.get('code') == '51001':
                self.log(f"Order already filled/cancelled (OK)", level="info")
                return True
            else:
                self.log(f"Failed to cancel order (OK, continuing): {response.get('msg') if response else 'No response'}", level="debug")
                return False
        except Exception as e:
            self.log(f"Exception in _okx_cancel_order: {e}", level="debug")
            return False

    def _okx_cancel_algo_order(self, symbol, algo_id):
        try:
            # Use the modern plural endpoint for canceling algo orders
            path = "/api/v5/trade/cancel-algos"
            body = [{
                "instId": symbol,
                "algoId": algo_id,
            }]

            self.log(f"Cancelling OKX algo order {str(algo_id)[:12]}...", level="debug")
            response = self._okx_request("POST", path, body_dict=body)

            if response and response.get('code') == '0':
                self.log(f"[OK] Algo order cancelled", level="debug")
                return True
            elif response and response.get('code') == '51001':
                self.log(f"Algo order already filled/cancelled (OK)", level="debug")
                return True
            else:
                self.log(f"Failed to cancel algo order (OK, continuing): {response.get('msg') if response else 'No response'}", level="debug")
                return False
        except Exception as e:
            self.log(f"Exception in _okx_cancel_algo_order: {e}", level="error")
            return False

    def _close_all_entry_orders(self):
        try:
            self.log("Attempting to close unfilled linear entry orders...", level="info")

            path = "/api/v5/trade/orders-pending"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)

            if not response or response.get('code') == '0':
                self.log("No orders found or API error (OK if no orders)", level="info")
                return True

            orders = response.get('data', [])
            cancelled_count = 0

            for order in orders:
                try:
                    order_id = order.get('ordId')
                    status = order.get('state')
                    side = order.get('side')
                    if side == 'buy' and status not in ['filled', 'canceled', 'rejected']:
                        if self._okx_cancel_order(self.config['symbol'], order_id):
                            cancelled_count += 1
                            time.sleep(0.1)
                except Exception as e:
                    self.log(f"Error processing OKX order: {e}", level="error")

            if cancelled_count > 0:
                self.log(f"[OK] Closed {cancelled_count} unfilled linear entry orders", level="info")
            else:
                self.log(f"No unfilled linear entry orders to close (OK)", level="info")

            return True
        except Exception as e:
            self.log(f"Exception in _close_all_entry_orders: {e} (continuing)", level="error")
            return True

    def _handle_tp_hit(self, side='long'):
        with self.tp_hit_lock:
            self.tp_hit_triggered = True # Set the flag immediately

        try:
            self.log("=" * 80, level="info")
            self.log(f"[TARGET] TP HIT ({side.upper()}) - EXECUTING PROTOCOL", level="info")
            self.log("=" * 80, level="info")

            self.log("Step 1: Closing unfilled entry orders...", level="info")
            self._close_all_entry_orders()

            time.sleep(1)

            self.log(f"Step 2: Checking {side.upper()} OKX position status...", level="info")
            path = "/api/v5/account/positions"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)

            position_still_open = False
            open_qty = 0.0

            if response and response.get('code') == '0':
                positions = response.get('data', [])
                for pos in positions:
                    if pos.get('instId') == self.config['symbol'] and pos.get('posSide', 'net') == side:
                        pos_qty_raw = safe_float(pos.get('pos', '0'))
                        if abs(pos_qty_raw) > 0:
                            position_still_open = True
                            open_qty = abs(pos_qty_raw)
                            self.log(f"OKX {side.upper()} position still open: {open_qty} (partial fill)", level="info")
                            break

            if position_still_open and open_qty > 0:
                self.log("Step 3: Waiting 3 seconds for liquidity...", level="info")
                time.sleep(3)

                self.log(f"Step 4: Market closing remaining {side.upper()} position...", level="info")
                close_side = "Sell" if side == 'long' else "Buy"
                exit_order_response = self._okx_place_order(self.config['symbol'], close_side, open_qty, order_type="Market", reduce_only=True, posSide=side)

                if exit_order_response and exit_order_response.get('ordId'):
                    self.log(f"[OK] Market close order placed for {open_qty} {self.config['symbol']} ({side})", level="info")
                    self.log(f"[DONE] Close Position (TP Partial): {side.upper()} {self.config['symbol']} | Qty: {open_qty}", level="info")
                
                time.sleep(1)
                self._cancel_all_exit_orders_and_reset(f"TP hit - {side} closed", side=side)
            else:
                self.log(f"OKX {side.upper()} position fully closed or not found. No market close needed.", level="info")
                self.log(f"[DONE] Close Position (TP): {side.upper()} {self.config['symbol']}", level="info")
                self._cancel_all_exit_orders_and_reset(f"TP hit - {side} fully closed", side=side)

            with self.tp_hit_lock:
                self.tp_hit_triggered = False

            self.log(f"[OK] {side.upper()} TP HIT PROTOCOL COMPLETE", level="info")

        except Exception as e:
            self.log(f"Exception in _handle_tp_hit ({side}): {e}", level="error")
            with self.tp_hit_lock:
                self.tp_hit_triggered = False

    def _handle_eod_exit(self):
        try:
            self.log("=" * 80, level="info")
            self.log("🕐 EOD EXIT TRIGGERED (OKX)", level="info")
            self.log("=" * 80, level="info")

            self.log("Step 1: Closing all open OKX positions...", level="info")
            try:
                path = "/api/v5/account/positions"
                params = {"instType": "SWAP", "instId": self.config['symbol']}
                response = self._okx_request("GET", path, params=params)

                if response and response.get('code') == '0':
                    positions = response.get('data', [])
                    for pos in positions:
                        if pos.get('instId') == self.config['symbol']:
                            size_rv = safe_float(pos.get('pos', 0))
                            if abs(size_rv) > 0:
                                pos_side = pos.get('posSide', 'net')
                                close_side = "Sell" if size_rv > 0 else "Buy"
                                
                                self.log(f"Found active {pos_side} position: {size_rv} - closing...", level="info")
                                exit_order_response = self._okx_place_order(self.config['symbol'], close_side, abs(size_rv), order_type="Market", reduce_only=True, posSide=pos_side)
                                if exit_order_response and exit_order_response.get('ordId'):
                                    self.log(f"[OK] {pos_side.upper()} close order placed", level="info")
                                else:
                                    self.log(f"⚠ {pos_side.upper()} close failed (OK if closed)", level="warning")
                                time.sleep(0.5)
                else:
                    self.log("No OKX positions found or API error (OK)", level="info")
            except Exception as e:
                self.log(f"Error closing OKX positions: {e} (OK, continuing)", level="warning")

            self.log("Step 2: Closing unfilled entry orders...", level="info")
            try:
                self._close_all_entry_orders()
            except Exception as e:
                self.log(f"Error closing entry orders: {e} (OK, continuing)", level="warning")

            time.sleep(0.5)

            self.log("Step 3: Force cancelling all remaining OKX orders...", level="info")
            try:
                path = "/api/v5/trade/cancel-all-after"
                body = {"timeOut": "0", "instType": "SWAP"}
                response = self._okx_request("POST", path, body_dict=body)
                if response and response.get('code') == '0':
                    self.log(f"[OK] All OKX orders cancelled", level="info")
                else:
                    self.log(f"⚠ All OKX orders cancel response: {response} (OK)", level="warning")
            except Exception as e:
                self.log(f"Error force cancelling OKX orders: {e} (OK, continuing)", level="error")

            self.log("=" * 80, level="info")
            self.log("[OK] EOD EXIT COMPLETE (OKX)", level="info")
            self.log("=" * 80, level="info")

            self._cancel_all_exit_orders_and_reset("EOD Exit")

        except Exception as e:
            self.log(f"Exception in _handle_eod_exit (OKX): {e} (continuing)", level="error")
            self._cancel_all_exit_orders_and_reset("EOD Exit - forced")


    def _handle_order_update(self, orders_data):
        with self.position_lock:
             # Snapshot current states for directional mapping 
             active_exit_ids = {
                 'long': self.position_exit_orders.get('long', {}),
                 'short': self.position_exit_orders.get('short', {})
             }
             pending_entry_ids = list(self.pending_entry_ids)
             
        for order in orders_data:
            if not isinstance(order, dict): continue

            order_id = order.get('ordId') or order.get('algoId')
            status = order.get('state')
            symbol = order.get('instId')
            pos_side = order.get('posSide', 'net')
            order_side = order.get('side', '').lower()
            
            if symbol != self.config['symbol']: continue

            # Map side for processing - More robust for net mode
            side_key = None
            if pos_side == 'long': 
                side_key = 'long'
            elif pos_side == 'short': 
                side_key = 'short'
            else: # net mode
                # Try to determine side from ID mapping instead of config default
                if order_id == active_exit_ids['long'].get('sl') or order_id == active_exit_ids['long'].get('tp'):
                    side_key = 'long'
                elif order_id == active_exit_ids['short'].get('sl') or order_id == active_exit_ids['short'].get('tp'):
                    side_key = 'short'
                else:
                    # For entries or if not matched yet, fallback to config but check order side
                    config_dir = self.config.get('direction', 'long')
                    if config_dir != 'both':
                        side_key = config_dir
                    else:
                        # Ambiguous 'both' + 'net' - check if order ID is in pending entries for specific logic
                        side_key = 'long' # Default fallback

            # 1. SL HIT
            if side_key and order_id == active_exit_ids[side_key].get('sl') and status in ['filled', 'partially_filled']:
                with self.sl_hit_lock:
                    if not self.sl_hit_triggered:
                        self.sl_hit_triggered = True
                        threading.Timer(0.1, lambda s=side_key: self._handle_sl_hit(side=s)).start()
                return

            # 2. ENTRY FILLED
            if order_id in pending_entry_ids:
                # If we are in 'both' + 'net', we should try to figure out which entry was filled
                # The pending_entry_order_details should have the side info
                entry_side = 'long'
                with self.position_lock:
                    if order_id in self.pending_entry_order_details:
                        detail_side = self.pending_entry_order_details[order_id].get('side', '').lower()
                        entry_side = 'long' if detail_side == 'buy' else 'short'

                cum_qty = safe_float(order.get('accFillSz', 0))
                with self.position_lock:
                    if order_id in self.pending_entry_order_details:
                        self.pending_entry_order_details[order_id]['status'] = status
                        self.pending_entry_order_details[order_id]['cum_qty'] = cum_qty

                if status in ['filled', 'partially_filled'] or cum_qty > 0:
                    self.log(f"🎉 ENTRY FILLED [{entry_side.upper()}]: {cum_qty} {self.config['symbol']}", level="info")
                    if status == 'filled':
                        threading.Timer(2.0, lambda oid=order_id: self._confirm_and_set_active_position(oid)).start()
                    else:
                        threading.Timer(5.0, lambda oid=order_id: self._confirm_and_set_active_position(oid)).start()
                    return
                elif status in ['canceled', 'failed']:
                    self._reset_entry_state(f"Entry order {status}")
                    return

            # 3. TP HIT
            if side_key and order_id == active_exit_ids[side_key].get('tp') and status in ['filled', 'partially_filled']:
                with self.tp_hit_lock:
                    if not self.tp_hit_triggered:
                        self.tp_hit_triggered = True
                        threading.Timer(0.1, lambda s=side_key: self._handle_tp_hit(side=s)).start()
                return

    def _detect_sl_from_position_update(self, positions_msg):
        # Scan positions message for closures
        for pos in positions_msg:
            if pos.get('instId') == self.config['symbol']:
                pos_side = pos.get('posSide', 'net')
                side_key = 'long'
                if pos_side == 'short': side_key = 'short'
                elif pos_side == 'net':
                    side_key = self.config.get('direction', 'long')
                    if side_key == 'both': side_key = 'long'

                size_rv = safe_float(pos.get('pos', 0))
                
                with self.position_lock:
                    was_in = self.in_position[side_key]
                    exp_qty = self.position_qty[side_key]

                if was_in and size_rv == 0 and abs(exp_qty) > 0:
                    self.log(f"🛑 SL DETECTED [{side_key.upper()}] via WebSocket Position Update!", level="info")
                    with self.sl_hit_lock:
                        if not self.sl_hit_triggered:
                            self.sl_hit_triggered = True
                            threading.Timer(0.1, lambda s=side_key: self._handle_sl_hit(side=s)).start()


    def _handle_sl_hit(self, side='long'):
        with self.sl_hit_lock:
            self.sl_hit_triggered = True # Set the flag immediately

        try:
            self.log("=" * 80, level="info")
            self.log(f"🛑 STOP LOSS HIT ({side.upper()}) - EXECUTING CLEANUP", level="info")
            self.log("=" * 80, level="info")

            self.log(f"{side.upper()} position already closed by exchange SL", level="info")

            try:
                self._close_all_entry_orders()
            except: pass

            time.sleep(0.5)

            self.log(f"Cancelling {side.upper()} TP order and resetting state...", level="info")
            self.log(f"[DONE] Close Position (SL): {side.upper()} {self.config['symbol']}", level="info")
            self._cancel_all_exit_orders_and_reset(f"SL hit - {side} closed by exchange", side=side)
            
            # Trigger immediate account refresh for UI responsiveness
            def _update_after_init():
                try:
                    self._sync_account_data()
                    self._emit_socket_updates()
                except Exception as e:
                    self.log(f"Error in init update: {e}", level="error")
            threading.Thread(target=_update_after_init, daemon=True).start()

            with self.sl_hit_lock:
                self.sl_hit_triggered = False
            self.log(f"[OK] {side.upper()} SL CLEANUP COMPLETE", level="info")
        except Exception as e:
            self.log(f"Exception in _handle_sl_hit ({side}): {e}", level="error")
            self._cancel_all_exit_orders_and_reset(f"SL hit - {side} forced reset", side=side)
            with self.sl_hit_lock:
                self.sl_hit_triggered = False

    def _confirm_and_set_active_position(self, filled_order_id):
        try:
            self.log(f"Confirming OKX position for filled order ID: {filled_order_id}", level="debug")

            path = "/api/v5/account/positions"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)
            self.log(f"DEBUG: Response from /api/v5/account/positions: {response}", level="debug")

            entry_confirmed = False
            actual_entry_price = 0.0
            actual_qty = 0.0
            actual_side = None
            found_pos_side = None

            if response and response.get('code') == '0':
                positions = response.get('data', [])
                self.log(f"DEBUG: Positions data from OKX: {positions}", level="debug")
                for pos in positions:
                    if pos.get('instId') == self.config['symbol']:
                        pos_qty_str = pos.get('pos', '0')
                        size_val = safe_float(pos_qty_str)
                        if abs(size_val) > 0:
                            avg_entry_price_rv = safe_float(pos.get('avgPx', 0))
                            actual_entry_price = avg_entry_price_rv
                            actual_qty = size_val
                            entry_confirmed = True
                            # Determine actual side if 'net'
                            found_pos_side = pos.get('posSide')
                            if found_pos_side == 'net' or not found_pos_side:
                                actual_side = 'short' if size_val < 0 else 'long'
                            else:
                                actual_side = found_pos_side
                            
                            self.log(f"DEBUG: Confirmed active {actual_side} position - Entry Price: {actual_entry_price}, Quantity: {actual_qty}", level="debug")
                            break

            if not entry_confirmed or actual_entry_price <= 0:
                self.log("CRITICAL: Could not confirm OKX position or invalid entry price!", level="error")
                self.log(f"DEBUG: entry_confirmed: {entry_confirmed}, actual_entry_price: {actual_entry_price}", level="debug")
                return


            tp_price = 0.0
            sl_price = 0.0
            tp_off = self.config.get('tp_price_offset', 0)
            sl_off = self.config.get('sl_price_offset', 0)

            if actual_side == 'long':
                if tp_off and safe_float(tp_off) > 0:
                    tp_price = actual_entry_price + safe_float(tp_off)
                else:
                    self.log(f"Confirm Pos: TP offset is null or 0 for {actual_side.upper()}. Skipping TP calc.", level="info")

                if sl_off and safe_float(sl_off) > 0:
                    sl_price = actual_entry_price - safe_float(sl_off)
                else:
                    self.log(f"Confirm Pos: SL offset is null or 0 for {actual_side.upper()}. Skipping SL calc.", level="info")
                exit_order_side = "sell"
            else: # short
                if tp_off and safe_float(tp_off) > 0:
                    tp_price = actual_entry_price - safe_float(tp_off)
                else:
                    self.log(f"Confirm Pos: TP offset is null or 0 for {actual_side.upper()}. Skipping TP calc.", level="info")

                if sl_off and safe_float(sl_off) > 0:
                    sl_price = actual_entry_price + safe_float(sl_off)
                else:
                    self.log(f"Confirm Pos: SL offset is null or 0 for {actual_side.upper()}. Skipping SL calc.", level="info")
                exit_order_side = "buy"
            
            with self.position_lock:
                self.in_position[actual_side] = True
                self.position_entry_price[actual_side] = actual_entry_price
                self.position_qty[actual_side] = actual_qty
                self.current_take_profit[actual_side] = tp_price
                self.current_stop_loss[actual_side] = sl_price
                self.pending_entry_order_id = None
                self.position_exit_orders[actual_side] = {}

                # Emit position update for this side
                self.emit('position_update', {
                    'in_position': self.in_position[actual_side],
                    'position_entry_price': self.position_entry_price[actual_side],
                    'position_qty': self.position_qty[actual_side],
                    'current_take_profit': self.current_take_profit[actual_side],
                    'current_stop_loss': self.current_stop_loss[actual_side],
                    'side': actual_side 
                })

            self.log(f"OKX {actual_side.upper()} POSITION OPENED", level="info")
            self.log(f"Entry: ${actual_entry_price:.2f} | Qty: {actual_qty}", level="info")
            self.log(f"TP: ${tp_price:.2f} | SL: ${sl_price:.2f}", level="info")

            # Check for existing TP/SL orders (Atomic Fallback Check)
            existing_tp = False
            existing_sl = False
            try:
                algo_path = "/api/v5/trade/orders-algo-pending"
                algo_params = {"instId": self.config['symbol'], "ordType": "conditional"}
                algo_res = self._okx_request("GET", algo_path, params=algo_params)
                if algo_res and algo_res.get('code') == '0':
                    for ord in algo_res.get('data', []):
                         # Check if order is for this position side (Long/Short)
                         # OKX 'posSide' in algo order details usually matches 'long'/'short' or 'net'
                         # We check direction: Long Pos -> Sell Order, Short Pos -> Buy Order
                         if actual_side == 'long' and ord['side'] == 'sell':
                             if ord.get('slTriggerPx') and safe_float(ord['slTriggerPx']) > 0: existing_sl = True
                             if ord.get('tpTriggerPx') and safe_float(ord['tpTriggerPx']) > 0: existing_tp = True
                         elif actual_side == 'short' and ord['side'] == 'buy':
                             if ord.get('slTriggerPx') and safe_float(ord['slTriggerPx']) > 0: existing_sl = True
                             if ord.get('tpTriggerPx') and safe_float(ord['tpTriggerPx']) > 0: existing_tp = True
                
                self.log(f"Atomic TP/SL Check: TP={'Found' if existing_tp else 'Missing'}, SL={'Found' if existing_sl else 'Missing'}", level="debug")

            except Exception as e:
                 self.log(f"Failed to check existing algo orders: {e}", level="warning")

            price_precision = self.product_info.get('pricePrecision', 4)
            qty_precision = self.product_info.get('qtyPrecision', 8)

            # Place TP and SL as algo (conditional) orders via /api/v5/trade/order-algo
            # ONLY IF MISSING (Smart Fallback) and IF OFFSET IS CONFIGURED
            if not existing_tp:
                if tp_off and safe_float(tp_off) > 0:
                    tp_body = {
                        "instId": self.config['symbol'],
                        "tdMode": self.config.get('mode', 'cross'),
                        "side": exit_order_side,
                        "posSide": actual_side, 
                        "ordType": "conditional",
                        "sz": f"{(abs(actual_qty) * (self.config.get('tp_amount', 100) / 100)):.{qty_precision}f}",
                        "tpTriggerPx": f"{tp_price:.{price_precision}f}",
                        "tpOrdPx": "-1" if self.config.get('tp_mode', 'market') == 'market' else f"{tp_price:.{price_precision}f}",
                        "reduceOnly": "true"
                    }

                    tp_order = self._okx_place_algo_order(tp_body)
                    if tp_order and (tp_order.get('algoId') or tp_order.get('ordId')):
                        algo_id = tp_order.get('algoId') or tp_order.get('ordId')
                        with self.position_lock:
                            self.position_exit_orders[actual_side]['tp'] = algo_id
                        self.log(f"[OK] TP algo order placed for {actual_side.upper()} at ${tp_price:.2f}", level="info")
                    else:
                        self.log(f"❌ Failed to place TP algo order: {tp_order}", level="error")
                        self._execute_trade_exit(f"Failed to place TP for {actual_side}", side=actual_side)
                        return
                else:
                    self.log(f"Skipping TP placement for {actual_side.upper()} (No offset configured)", level="info")
            else:
                self.log("TP algo order already exists (Atomic). Skipping redundant placement.", level="info")
                algo_id = tp_order.get('ordId') # Handle the fake/atomic id
                with self.position_lock:
                    self.position_exit_orders[actual_side]['tp'] = algo_id

            if not existing_sl:
                if sl_off and safe_float(sl_off) > 0:
                    sl_body = {
                        "instId": self.config['symbol'],
                        "tdMode": self.config.get('mode', 'cross'),
                        "side": exit_order_side,
                        "posSide": actual_side,
                        "ordType": "conditional",
                        "sz": f"{(abs(actual_qty) * (self.config.get('sl_amount', 100) / 100)):.{qty_precision}f}",
                        "slTriggerPx": f"{sl_price:.{price_precision}f}",
                        "slOrdPx": "-1", # market
                        "reduceOnly": "true"
                    }

                    sl_order = self._okx_place_algo_order(sl_body)
                    if sl_order and (sl_order.get('algoId') or sl_order.get('ordId')):
                        algo_id = sl_order.get('algoId') or sl_order.get('ordId')
                        with self.position_lock:
                            self.position_exit_orders[actual_side]['sl'] = algo_id
                        self.log(f"[OK] SL algo order placed for {actual_side.upper()} at ${sl_price:.2f}", level="info")
                    else:
                        self.log(f"❌ Failed to place SL algo order: {sl_order}", level="error")
                        self._execute_trade_exit(f"Failed to place SL for {actual_side}", side=actual_side)
                        return
                else:
                    self.log(f"Skipping SL placement for {actual_side.upper()} (No offset configured)", level="info")
            else:
                 self.log("SL algo order already exists (Atomic). Skipping redundant placement.", level="info")
                 algo_id = sl_order.get('ordId')
                 with self.position_lock:
                     self.position_exit_orders[actual_side]['sl'] = algo_id

        except Exception as e:
            self.log(f"Exception in _confirm_and_set_active_position (OKX): {e}", level="error")


    def _execute_trade_exit(self, reason, side=None):
        """
        Authoritative Account Reset: Fetches ALL positions and orders directly from OKX
        and closes/cancels everything to leave NOTHING behind.
        """
        with self.exit_lock:
            if self.authoritative_exit_in_progress:
                self.log(f"Join: Authoritative exit already in progress. Ignoring trigger: {reason}", level="debug")
                return
            self.authoritative_exit_in_progress = True

        try:
            target_symbol = self.config['symbol']
            self.log(f"=== EMERGENCY EXIT === Reason: {reason} | Symbol: {target_symbol}", level="info")

            # 1. Fetch CURRENT positions directly from exchange
            path = "/api/v5/account/positions"
            params = {"instType": "SWAP", "instId": target_symbol}
            response = self._okx_request("GET", path, params=params)

            if response and response.get('code') == '0':
                positions_data = response.get('data', [])
                for pos in positions_data:
                    # Filter for our symbol just in case, though OKX params should handle it
                    if pos.get('instId') == target_symbol:
                        pos_qty = safe_float(pos.get('pos', '0'))
                        pos_side_raw = pos.get('posSide', 'net')
                        mgn_mode = pos.get('mgnMode') # Extract margin mode (cross/isolated)
                        
                        if abs(pos_qty) > 0:
                            # Record realized PnL before closing
                            unrealized_pnl = safe_float(pos.get('upl', '0'))
                            if unrealized_pnl > 0:
                                self.total_trade_profit += unrealized_pnl
                            else:
                                self.total_trade_loss += abs(unrealized_pnl)
                            self.net_trade_profit = self.total_trade_profit - self.total_trade_loss
                            
                            # Determine close side (If qty > 0 [Long], Sell. If qty < 0 [Short], Buy)
                            close_side = "Sell" if pos_qty > 0 else "Buy"
                            
                            self.log(f"Force closing {pos_side_raw.upper()} position: {abs(pos_qty)} {target_symbol} @ Market (Mode: {mgn_mode})", level="info")
                            # Pass mgn_mode as tdMode to ensure we address the position in the correct margin account
                            exit_order = self._okx_place_order(target_symbol, close_side, abs(pos_qty), order_type="Market", reduce_only=True, posSide=pos_side_raw, tdMode=mgn_mode)
                            
                            if exit_order and exit_order.get('ordId'):
                                self.log(f"[OK] Position closed. Order ID: {exit_order.get('ordId')}", level="info")
                                self.log(f"[DONE] Close Position (Auth): {pos_side_raw.upper()} {target_symbol} | Reason: {reason}", level="info")
                                self.log(f"[PROFIT] Snapshot PnL (Pre-Close): ${unrealized_pnl:.2f} | Note: Actual PnL will update from fills shortly.", level="info")
                            else:
                                self.log(f"⚠️ Market exit for {pos_side_raw.upper()} failed or rejected.", level="warning")


            # 2. Batch Cancel ALL pending orders for this symbol (Limit & Algo)
            # We call batch_cancel_orders which performs the exchange-wide sweep
            self.batch_cancel_orders()

            # 3. Synchronize internal state to avoid ghost tracking
            with self.position_lock:
                self.in_position = {'long': False, 'short': False}
                self.position_qty = {'long': 0.0, 'short': 0.0}
                self.position_entry_price = {'long': 0.0, 'short': 0.0}
                self.position_exit_orders = {'long': {}, 'short': {}}
                self.pending_entry_ids = []
                self.pending_entry_order_details = {}

        except Exception as e:
            self.log(f"CRITICAL ERROR in _execute_trade_exit: {e}", level="error")
        finally:
            with self.exit_lock:
                self.authoritative_exit_in_progress = False
            self.log("=== EMERGENCY EXIT COMPLETE === Account cleared for symbol.", level="info")
            
            # Immediately reconcile PnL from fills to capture actual fees/slippage
            time.sleep(1) # Wait for fills to index
            self._calculate_net_profit_from_fills()

    def _check_auto_add_position_step(self, current_price, current_side, remaining_budget):
        # Log condition check (User Request)
        # Only log if monitoring tick allows to avoid spam, but client wants VISIBILITY.
        if self.monitoring_tick % 6 == 0:
            self.log("Check Add Position Condition")

        # GAP-BASED AUTO-ADD LOGIC
        # Trigger: Market Price is [GAP] more than Average Entry Price.
        # Sizing: Step 1 = 1x Price, Step 2 = 2x Price (if scaling enabled).
        
        gap_threshold = self.config.get('add_pos_gap_threshold', 5.0)
        
        # 1. Get Average Entry Price
        avg_entry = 0.0
        with self.position_lock:
            if current_side == 'long':
                avg_entry = self.position_entry_price.get('long', 0.0)
            else:
                avg_entry = self.position_entry_price.get('short', 0.0)
        
        if avg_entry <= 0: return # No position to add to

        gap_threshold = self.config.get('add_pos_gap_threshold', 5.0)
        size_pct = self.config.get('add_pos_size_pct', 30.0) / 100.0
        
        # Sequential Offset Logic (Step 2+): Apply custom offsets from the second add onwards
        if self.auto_add_step_count >= 1:
            gap_threshold = self.config.get('add_pos_gap_threshold_2', gap_threshold)
            size_pct = self.config.get('add_pos_size_pct_2', self.config.get('add_pos_size_pct', 30.0)) / 100.0
        # User Requirement: "For short: When PnL is loss... if market > entry gap 5... add orders"
        # This means we ONLY trigger if price moves AGAINST the position.

        gap_magnitude = 0.0
        is_loss_gap = False

        if current_side == 'long':
            # Long: Loss if Market < Entry. Gap = Entry - Market
            if current_price < avg_entry:
                gap_magnitude = avg_entry - current_price
                is_loss_gap = True
        else: # short
            # Short: Loss if Market > Entry. Gap = Market - Entry
            if current_price > avg_entry:
                gap_magnitude = current_price - avg_entry
                is_loss_gap = True

        # If it's a profit gap (or flat), we do NOT add.
        if not is_loss_gap:
            # self.log(f"[DEBUG] Position in Profit or Zero Gap. No Averaging Down.", level="debug")
            return

        if gap_magnitude < gap_threshold:
             # self.log(f"[DEBUG] Gap {gap_magnitude:.2f} < {gap_threshold}. No Add.", level="debug")
             return # No gap trigger

        # 3. Mode 1 Specific Check: Stop adding if PnL already near zero
        # Requirement: "Run max 6 loops to make PnL near 0" - stop when goal achieved
        if self.config.get('use_add_pos_above_zero', False):
            trade_fee_pct = self.config.get('trade_fee_percentage', 0.07)
            
            # Calculate current position size for fee calculation
            current_total_notional = 0.0
            with self.position_lock:
                if current_side == 'long':
                    current_total_notional = self.okx_position_notional.get('long', 0)
                else:
                    current_total_notional = self.okx_position_notional.get('short', 0)
            
            if current_total_notional > 0:
                # Audit Fix: Use round-trip fees for threshold calculation
                current_size_fee = current_total_notional * (trade_fee_pct / 100.0)
                near_zero_threshold = max(1.0, (current_size_fee * 2.0) * 0.1)
                
                # If PnL is already near zero (accounting for both fees), don't add more
                if self.net_profit >= -near_zero_threshold:
                    self.log(f"Mode 1 Check PnL near zero (${self.net_profit:.2f}). No more adding needed.", level="info")
                    return

        # 4. Check Max Loops
        max_loops = self.config.get('add_pos_max_count', 10)
        if self.auto_add_step_count >= max_loops:
             self.log(f"Auto-Add Check SKIPPED: Max Loops Reached ({self.auto_add_step_count} >= {max_loops})", level="warning")
             return

        # 5. Calculate Add Amount (Percentage of Current Size)
        # Sizing already calculated above in sequential logic
        
        # We need CURRENT TOTAL SIZE (Notional)
        current_total_notional = 0.0
        with self.position_lock:
             if current_side == 'long':
                 # Calculate from currently tracked position
                 current_total_notional = abs(safe_float(self.position_details.get('long', {}).get('notionalUsd', 0))) 
                 # Or use okx_pos_notional passed in?
             else:
                 current_total_notional = abs(safe_float(self.position_details.get('short', {}).get('notionalUsd', 0)))
        
        # Fallback if position detail is missing but we are here (shouldn't happen much)
        if current_total_notional == 0:
             # Try getting from open trades? No, this is triggered when we HAVE a position.
             return

        add_notional = current_total_notional * size_pct
        
        step_count = self.auto_add_step_count + 1 # 1-based current step
        
        # LOGGING CRITICAL STEPS FOR USER VISIBILITY
        self.log(f"=== AUTO-ADD TRIGGERED (Step {step_count}) ===", level="warning")
        self.log(f"1. Gap Check: Market {current_price:.2f} vs AvgEntry {avg_entry:.2f} | Gap {gap_magnitude:.2f} > Threshold {gap_threshold}", level="info")
        self.log(f"2. Sizing: Current Size ${current_total_notional:.2f} x {size_pct*100:.1f}% = ${add_notional:.2f}", level="info")
        
        # 5. Calculate Margin to Deduct from Capital 2nd
        # "Order amount is the AFTER leverage... minus amount from total capital 2nd"
        # Be careful: "Order amount" usually means Notional. 
        # But user says: "divide leverage 100=4.5, minus 4.5 from total capital 2nd".
        # So Capital 2nd tracks MARGIN ("Real Money Used"), not Notional.
        
        # Get leverage
        current_leverage = 1.0
        with self.position_lock:
            if current_side == 'long':
                current_leverage = self.position_details.get('long', {}).get('lever', 1.0)
            else:
                current_leverage = self.position_details.get('short', {}).get('lever', 1.0)
        current_lever_float = safe_float(current_leverage, 1.0)
        if current_lever_float <= 0: current_lever_float = 1.0

        margin_cost = add_notional / current_lever_float
        
        # CLIENT REQUIREMENT: NO budget check for Auto-Add
        # "All order amount is NOT minus from remaining but from total capital"
        # Orders should place as long as gap condition is met
        # if margin_cost > remaining_budget:
        #      self.log(f"Auto-Add Check SKIPPED: Required Margin ${margin_cost:.2f} > Budget ${remaining_budget:.2f}", level="warning")
        #      return

        # Execute Market Order
        target_side = "Buy" if current_side == 'long' else "Sell"
        
        # Qty = Notional / Price / ContractSize
        contract_size = safe_float(self.product_info.get('contractSize', 1.0))
        if contract_size <= 0: contract_size = 1.0
        
        qty_contracts = add_notional / (current_price * contract_size)
        
        # Format Qty
        qty_precision = safe_int(self.product_info.get('lotSzPrecision', '0'))
        is_integer_qty = self.product_info.get('lotSz', '1') == '1' and '.' not in self.product_info.get('lotSz', '1')
        
        if is_integer_qty:
            qty_str = str(max(1, int(qty_contracts)))
        else:
            min_sz = safe_float(self.product_info.get('minSz', 0.001))
            qty_str = f"{max(min_sz, qty_contracts):.{qty_precision}f}"
            
        self.log(f"Auto-Add Check Triggering Gap Add (Step {step_count}). Gap: {gap_magnitude:.2f} > {gap_threshold}. Cost: ${margin_cost:.2f} (Notional: ${add_notional:.2f}, Qty: {qty_str})", level="warning")
        
        order_response = self._okx_place_order(
            self.config['symbol'],
            target_side,
            safe_float(qty_str),
            order_type="Market",
            verbose=True
        )

        if order_response and order_response.get('ordId'):
            self.log(f"[OK] Auto-Add Step {step_count} Executed. Order: {order_response.get('ordId')}", level="info")
            # Update State
            self.last_add_price = current_price
            self.cumulative_margin_used += margin_cost # Track Margin
            self.auto_add_step_count += 1
            
            # Cooldown
            time.sleep(1)
            
            # Step 2: Ensure Exit Orders are updated immediately
            threading.Thread(target=self._update_exit_orders, args=(current_side,), daemon=True).start()
        else:
            self.log(f"[FAIL] Auto-Add Step {step_count} Failed: {order_response}", level="error")

    def _update_exit_orders(self, side):
        """
        Step 2 of Auto-Add: Calculate new avg price and place Limit Exit (TP).
        """
        try:
            time.sleep(2) # Wait for fill
            
            # 1. Fetch latest position data
            path = "/api/v5/account/positions"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)
             
            if response and response.get('code') == '0':
                positions_data = response.get('data', [])
                target_pos = None
                for pos in positions_data:
                    pos_side = pos.get('posSide', 'net')
                    if side == 'long' and (pos_side == 'long' or (pos_side == 'net' and float(pos['pos']) > 0)):
                        target_pos = pos
                        break
                    elif side == 'short' and (pos_side == 'short' or (pos_side == 'net' and float(pos['pos']) < 0)):
                        target_pos = pos
                        break
                
                if target_pos:
                    avg_px = safe_float(target_pos.get('avgPx'))
                    # Calculate TP Price
                    # Mode 1: Break Even (AvP + Fee/Size?) -> Just AvP for now or small profit
                    # Mode 2: Profit Multiplier
                    
                    tp_price = 0.0
                    if side == 'long':
                        # Mode 1: Fixed Offset (Priority)
                        # Target = Avg + Offset
                        step2_offset = safe_float(self.config.get('add_pos_step2_offset', 0.0))
                        
                        if step2_offset > 0:
                            tp_price = avg_px + step2_offset
                        else:
                            # Mode 2: Profit Multiplier
                            profit_mult = self.config.get('add_pos_profit_multiplier', 1.5)
                            trade_fee_pct = self.config.get('trade_fee_percentage', 0.07)
                            size_notional = safe_float(target_pos.get('notionalUsd'))
                            
                            # FIX: Multiplying fee pct by 2.0 to cover both OPEN and CLOSE fees.
                            # target_profit = (size_notional * (double_fees/100.0)) * profit_mult
                            target_profit = (size_notional * (trade_fee_pct * 2.0 / 100.0)) * profit_mult
                            
                            pos_contracts = safe_float(target_pos.get('pos'))
                            delta = target_profit / (pos_contracts * 1.0) # Approx
                            tp_price = avg_px + delta
                        
                    else: # Short
                        step2_offset = safe_float(self.config.get('add_pos_step2_offset', 0.0))
                        
                        if step2_offset > 0:
                            tp_price = avg_px - step2_offset
                        else:
                            profit_mult = self.config.get('add_pos_profit_multiplier', 1.5)
                            trade_fee_pct = self.config.get('trade_fee_percentage', 0.08)
                            size_notional = safe_float(target_pos.get('notionalUsd'))
                            
                            # FIX: Multiplying fee pct by 2.0 to cover both OPEN and CLOSE fees.
                            target_profit = (size_notional * (trade_fee_pct * 2.0 / 100.0)) * profit_mult
                            
                            pos_contracts = abs(safe_float(target_pos.get('pos')))
                            delta = target_profit / (pos_contracts * 1.0)
                            tp_price = avg_px - delta # Lower for short

                # Sanity check
                if tp_price > 0:
                    self.log(f"=== AUTO-ADD STEP 2 (CLOSE) ===", level="info")
                    if safe_float(self.config.get('add_pos_step2_offset', 0.0)) > 0:
                         self.log(f"Logic used: Fixed Offset (${safe_float(self.config.get('add_pos_step2_offset', 0.0))})", level="info")
                    else:
                         self.log(f"Logic used: Profit Multiplier ({self.config.get('add_pos_profit_multiplier', 1.5)}x Fees)", level="info")
                    
                    self.log(f"New Avg Entry: {avg_px} -> Setting Limit Exit at {tp_price:.4f}", level="info")
                    
                    # Sync internal state so closure detection knows this is a Mode 2 exit
                    with self.position_lock:
                        self.current_take_profit[side] = tp_price

                    # Place Limit Close
                    close_side = "Sell" if side == 'long' else "Buy"
                    qty = abs(safe_float(target_pos.get('pos')))
                    
                    # Reduce Only to strictly close
                    self._okx_place_order(
                        self.config['symbol'],
                        close_side,
                        qty,
                        price=tp_price,
                        order_type="Limit",
                        reduce_only=True,
                        verbose=True
                    )
                else:
                     self.log(f"Auto-Add Check Calculated TP Price Invalid: {tp_price}", level="error")

        except Exception as e:
            self.log(f"Error in _update_exit_orders: {e}", level="error")

    def _cancel_all_exit_orders_and_reset(self, reason, side=None):
        # Determine sides to reset
        sides_to_reset = [side] if side else ['long', 'short']
        
        with self.position_lock:
            for s in sides_to_reset:
                orders_to_cancel = list(self.position_exit_orders[s].values())

                self.in_position[s] = False
                self.position_entry_price[s] = 0.0
                self.position_qty[s] = 0.0
                self.current_take_profit[s] = 0.0
                self.current_stop_loss[s] = 0.0
                self.position_exit_orders[s] = {}
                self.entry_reduced_tp_flag[s] = False

                for order_id in orders_to_cancel:
                    if order_id:
                        try:
                            # Note: Usually already cancelled by execute_trade_exit, but safe to retry
                            self._okx_cancel_algo_order(self.config['symbol'], order_id)
                        except: pass

        with self.entry_order_sl_lock:
            self.entry_order_with_sl = None

        self.log("=" * 80, level="info")
        self.log(f"STATE RESET [{side.upper() if side else 'ALL'}] - Reason: {reason}", level="info")
        self.log("=" * 80, level="info")
    def _check_and_close_any_open_position(self):
        try:
            self.log("Checking for any open OKX positions to close...", level="debug")
            path = "/api/v5/account/positions"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)

            any_closed = False
            if response and response.get('code') == '0':
                positions = response.get('data', [])
                for pos in positions:
                    if pos.get('instId') == self.config['symbol']:
                        size_rv = safe_float(pos.get('pos', 0))
                        if abs(size_rv) > 0:
                            # Detect posSide and margin mode. TRUST THE EXCHANGE DATA.
                            pos_side = pos.get('posSide')
                            if not pos_side:
                                pos_side = 'net'
                                
                            mgn_mode = pos.get('mgnMode')

                            self.log(f"⚠️ Found open {pos_side} position: {size_rv} {self.config['symbol']} (Mode: {mgn_mode})", level="warning")
                            
                            # If size_rv is negative (short), we must BUY to close. This applies to Net mode too (negative size = short).
                            close_side = "Buy" if size_rv < 0 else "Sell"
                            
                            self.log(f"Closing {abs(size_rv)} {self.config['symbol']} with market {close_side} order (posSide: {pos_side})", level="info")
                            # Use explicit tdMode and posSide from the position data
                            close_order = self._okx_place_order(self.config['symbol'], close_side, abs(size_rv), order_type="Market", reduce_only=True, posSide=pos_side, tdMode=mgn_mode)
                            if close_order and close_order.get('ordId'):
                                self.log(f"[OK] Position close order placed: {close_order.get('ordId')}", level="info")
                                self.log(f"[DONE] Close Position (Manual): {pos_side.upper()} {self.config['symbol']} | Qty: {abs(size_rv)}", level="info")
                                any_closed = True
                            else:
                                self.log(f"❌ Failed to place close order for {pos_side} position", level="error")

            if not any_closed:
                self.log("No open OKX positions found to close.", level="info")
            return any_closed
        except Exception as e:
            self.log(f"Exception in _check_and_close_any_open_position (OKX): {e}", level="error")
            return False

    def _reset_entry_state(self, reason):
        with self.position_lock:
            self.pending_entry_order_id = None
            self.entry_reduced_tp_flag = False
            self.pending_entry_order_details = {}
        with self.entry_order_sl_lock:
            self.entry_order_with_sl = None
        self.log(f"Entry state reset. Reason: {reason}", level="info")


        self.log("=" * 80, level="info")
        self.log(f"POSITION CLOSED - Reason: {reason}", level="info")
        self.log("=" * 80, level="info")

        for order_id in orders_to_cancel:
            if order_id:
                try:
                    self._okx_cancel_algo_order(self.config['symbol'], order_id)
                    time.sleep(0.1)
                except Exception as e:
                    self.log(f"Error cancelling order: {e} (OK, continuing)", level="error")

        # Account information is no longer updated in real-time via private WebSocket.

    def _get_latest_data_and_indicators(self):
        try:
            with self.trade_data_lock: # Use trade_data_lock for latest_trade_price
                current_price = self.latest_trade_price
                if current_price is None:
                    if self.is_running:
                        self.log(f"Could not get current market price from WebSocket. Waiting for data.", level="warning")
                    return None
                
                # Check price age for logging/diagnostics
                price_age = time.time() - self.last_price_update_time
                if price_age > 1.0:
                    self.log(f"Price data is {price_age:.1f}s old. Checking connection...", level="debug")

            return {
                'current_price': current_price,
                'price_age': price_age
            }

        except Exception as e:
            self.log(f"Exception in _get_latest_data_and_indicators: {e}", level="error")
            return None

    def _check_candlestick_conditions(self, market_data):
        # Fetch the latest completed candle for the primary timeframe (e.g., '1m')
        # This assumes you have historical data being updated.
        timeframe = self.config.get('candlestick_timeframe', '1m')
        with self.data_lock:
            df = self.historical_data_store.get(timeframe)
            if df is None or df.empty:
                self.log(f"No historical data for {timeframe} to check candlestick conditions.", "warning")
                return True, "No Data (Default Pass)" # Default to true if data is not available to not block trades

            latest_candle = df.iloc[-1]
            o = latest_candle['Open']
            h = latest_candle['High']
            l = latest_candle['Low']
            c = latest_candle['Close']

        status_parts = []
        
        # Check Open-Close Change
        oc_pass = True
        if self.config.get('use_chg_open_close'):
            chg_open_close = abs(o - c)
            min_chg = self.config.get('min_chg_open_close', 0)
            max_chg = self.config.get('max_chg_open_close', 0)
            if not (min_chg <= chg_open_close <= max_chg):
                 oc_pass = False
            status_parts.append(f"open-close={'Passed' if oc_pass else 'Fail'}")

        # Check High-Low Change
        hl_pass = True
        if self.config.get('use_chg_high_low'):
            chg_high_low = h - l
            min_chg = self.config.get('min_chg_high_low', 0)
            max_chg = self.config.get('max_chg_high_low', 0)
            if not (min_chg <= chg_high_low <= max_chg):
                hl_pass = False
            status_parts.append(f"High-Low={'Passed' if hl_pass else 'Fail'}")

        # Check High-Close Change
        hc_pass = True
        if self.config.get('use_chg_high_close'):
            chg_high_close = abs(h - c)
            min_chg = self.config.get('min_chg_high_close', 0)
            max_chg = self.config.get('max_chg_high_close', 0)
            if not (min_chg <= chg_high_close <= max_chg):
                hc_pass = False
            status_parts.append(f"High-Close={'Passed' if hc_pass else 'Fail'}")

        all_passed = oc_pass and hl_pass and hc_pass
        status_str = "; ".join(status_parts) if status_parts else "Skipped"
        
        return all_passed, status_str

    def _okx_adjust_margin(self, symbol, posSide, amount, type='add'):
        """
        Adjust margin for isolated position.
        """
        path = "/api/v5/account/adj-margin"
        params = {
            "instId": symbol,
            "posSide": posSide,
            "type": type,
            "amt": str(amount)
        }
        res = self._okx_request("POST", path, body=params)
        if res and res.get('code') == '0':
            self.log(f"Successfully {type}ed {amount} margin to {posSide} {symbol}", level="info")
            return True
        else:
            self.log(f"Failed to move margin: {res}", level="error")
            return False

    def _check_entry_conditions(self, market_data, log_prefix=""):
        # Max Amount = Max Allowed Used (USDT)
        # Remaining = (Max Amount * Leverage) - Used Notional
        leverage = float(self.config.get('leverage', 1))
        if leverage <= 0: leverage = 1.0
        
        # Safety Clamp: max_allowed_used must be capped by total_equity (Total Capital)
        max_allowed_config = float(self.config.get('max_allowed_used', 1000.0))
        with self.account_info_lock:
            equity = self.total_equity
        
        max_amount_usdt = max_allowed_config
        if equity > 0 and max_allowed_config > equity:
            max_amount_usdt = equity
            if not getattr(self, '_max_allowed_clamped_logged', False):
                self.log(f"Safety Clamp: Max Allowed Used (${max_allowed_config:.2f}) capped by Total Capital (${equity:.2f})", level="warning")
                self._max_allowed_clamped_logged = True
        elif equity > 0 and max_allowed_config <= equity:
            self._max_allowed_clamped_logged = False

        rate_divisor = self.config.get('rate_divisor', 1)
        if rate_divisor <= 0: rate_divisor = 1
        max_amount_per_loop = max_amount_usdt / rate_divisor
        max_notional_capacity = max_amount_per_loop * leverage
        
        min_notional_per_order = self.config.get('min_order_amount', 100)
        
        with self.position_lock:
            # High-Precision Remaining Calculation
            remaining_notional = max_notional_capacity - self.used_amount_notional
            
            if remaining_notional < min_notional_per_order:
                display_remaining = max(0.0, remaining_notional)
                self.log(f"{log_prefix}Entry-3:Remaining Capacity: ${display_remaining:.2f} (Actual: {remaining_notional:.2f}) < Min ${min_notional_per_order}: NOT Passed. (Check 'Max Allowed Used' vs 'Total Capital')", level="info")
                return []

        target_amount = self.config.get('target_order_amount', 100)

        # User is responsible for setting Max Allowed within their balance limits
        # Bot focuses only on remaining capacity
        current_price = market_data['current_price']
        direction_mode = self.config.get('direction', 'long')
        long_safety = self.config.get('long_safety_line_price', 0)
        short_safety = self.config.get('short_safety_line_price', float('inf'))
        entry_price_offset = self.config.get('entry_price_offset', 0)

        valid_entries = []
        
        # Possible directions to check
        directions_to_eval = []
        if direction_mode == 'both':
            directions_to_eval = ['long', 'short']
        else:
            directions_to_eval = [direction_mode]

        # 0. Cooldown Check
        cooldown_sec = self.config.get('reentry_cooldown_seconds', 60)
        now = time.time()
        filtered_directions = []
        for d in directions_to_eval:
            last_close = self.last_close_time.get(d, 0)
            if now - last_close < cooldown_sec:
                 self.log(f"{log_prefix}Entry-0: {d.upper()} in cooldown ({int(cooldown_sec - (now - last_close))}s remaining). SKIPPED.", level="info")
                 continue
            filtered_directions.append(d)
        
        directions_to_eval = filtered_directions
        if not directions_to_eval:
             return []

        # Shared Candlestick check (if enabled)
        candlestick_passed = True
        candlestick_msg = "Skipped"
        if self.config.get('use_candlestick_conditions', False):
            candlestick_passed, candlestick_msg = self._check_candlestick_conditions(market_data)

        for d in directions_to_eval:
            passed = False
            signal = 0
            safety_p = 0.0
            limit_p = 0.0
            
            if d == 'long':
                safety_p = long_safety
                passed = (current_price < long_safety)
                signal = 1
                limit_p = current_price - entry_price_offset
            else: # short
                safety_p = short_safety
                passed = (current_price > short_safety)
                signal = -1
                limit_p = current_price + entry_price_offset

            self.log(f"{log_prefix}Entry-1:{d.upper()} Market {current_price:.2f}, Safety:{safety_p}, {'Passed' if passed else 'NOT Passed'}", level="info")
            
            if passed:
                if candlestick_passed:
                    valid_entries.append({'signal': signal, 'limit_price': limit_p, 'side': d})
                    if candlestick_msg != "Skipped":
                         self.log(f"{log_prefix}Entry-2:{candlestick_msg}", level="info")
                else:
                    self.log(f"{log_prefix}Entry-2:Candlestick {candlestick_msg}: NOT Passed", level="info")
        
        # Log final verification for consistency if nothing passed
        if not valid_entries:
             return []

        # Check explicit target/min logs for the first valid one to keep user dashboard tidy
        # Correct Log: Only log "Passed" if it actually PASSED.
        if remaining_notional >= target_amount:
            self.log(f"{log_prefix}Entry-3:Remaining: {remaining_notional:.2f} >= Target {target_amount}: Passed", level="info")
        else:
            self.log(f"{log_prefix}Entry-3:Remaining: {remaining_notional:.2f} < Target {target_amount}: NOT Passed", level="info")
            return []
        self.log(f"{log_prefix}Entry-4:Remaining: {remaining_notional:.2f} > Min {min_notional_per_order}: Passed", level="info")

        return valid_entries

    def _initiate_entry_sequence(self, initial_limit_price, signal, batch_size):
        # NOTE: This function places the batch. It does NOT handle the loop logic. 
        # The loop logic is now in _main_trading_logic.
        
        # We perform a double-check on balance but primary check is in _check_entry_conditions
        with self.account_info_lock:
            current_available_balance = self.available_balance

        batch_offset = self.config['batch_offset']
        self.batch_counter += 1
        
        self.log(f"Place Order Batch {self.batch_counter}", level="info")
        
        for i in range(batch_size):
            current_limit_price = initial_limit_price
            if i > 0: 
                if signal == 1: # Long
                    current_limit_price -= (batch_offset * i)
                else: # Short
                    current_limit_price += (batch_offset * i)

            if current_limit_price <= 0:
                continue

            # Recalculate room for EVERY order to be precise (though less critical if Target is small)
            leverage = float(self.config.get('leverage', 1))
            if leverage <= 0: leverage = 1.0
            
            # Safety Clamp: max_allowed_used must be capped by total_equity (Total Capital)
            max_allowed_config = float(self.config.get('max_allowed_used', 1000.0))
            with self.account_info_lock:
                equity = self.total_equity
            
            max_amount_usdt = max_allowed_config
            if equity > 0 and max_allowed_config > equity:
                max_amount_usdt = equity

            rate_divisor = self.config.get('rate_divisor', 1)
            if rate_divisor <= 0: rate_divisor = 1
            max_amount_per_loop = max_amount_usdt / rate_divisor
            max_notional_capacity = max_amount_per_loop * leverage
            
            with self.position_lock:
                remaining_notional = max_notional_capacity - self.used_amount_notional
            
            target_notional = self.config.get('target_order_amount', 100)
            min_notional = self.config.get('min_order_amount', 100)
            
            if remaining_notional < min_notional:
                self.log(f"Batch {self.batch_counter}-{i+1} skipped: Remaining ({remaining_notional:.2f}) < Min ({min_notional})", level="info")
                break
                
            trade_amount_usdt = min(target_notional, remaining_notional)
        
            # Target contracts based on exact trade_amount_usdt (removed 0.5% buffer)
            qty_base_asset = trade_amount_usdt / current_limit_price
            contract_size = safe_float(self.product_info.get('contractSize', 1.0))
            if contract_size <= 0: contract_size = 1.0

            # Use lot size (qtyStepSize) for precise rounding
            lot_size = safe_float(self.product_info.get('qtyStepSize', 1.0))
            if lot_size <= 0: lot_size = 1.0

            qty_contracts = math.floor((qty_base_asset / contract_size) / lot_size) * lot_size
            
            min_order_qty = safe_float(self.product_info.get('minOrderQty', 1.0))
            if qty_contracts < min_order_qty:
                 if (min_order_qty * contract_size * current_limit_price) <= remaining_notional:
                     qty_contracts = min_order_qty
                 else:
                     continue

            qty_precision = self.product_info.get('qtyPrecision', 0)
            qty_contracts = round(qty_contracts, qty_precision)
            
            # Calculate TP/SL for Display
            tp_px = 0.0
            sl_px = 0.0
            tp_offset_val = self.config.get('tp_price_offset', 0)
            sl_offset_val = self.config.get('sl_price_offset', 0)
            
            if signal == 1: # LONG
                if tp_offset_val and safe_float(tp_offset_val) > 0:
                    tp_px = current_limit_price + safe_float(tp_offset_val)
                if sl_offset_val and safe_float(sl_offset_val) > 0:
                    sl_px = current_limit_price - safe_float(sl_offset_val)
            else: # SHORT
                if tp_offset_val and safe_float(tp_offset_val) > 0:
                    tp_px = current_limit_price - safe_float(tp_offset_val)
                if sl_offset_val and safe_float(sl_offset_val) > 0:
                    sl_px = current_limit_price + safe_float(sl_offset_val)

            # Log Format: Batch1-1:M:2980|En:2982|TP:2976|SL:3010|1000|Short|Isolated|20x
            market_p = self.latest_trade_price if self.latest_trade_price else 0.0
            side_str = 'Long' if signal == 1 else 'Short'
            mode_str = self.config.get('mode', 'cross').capitalize()
            # M:{market}|En:{entry}|Tp:{tp}|SL:{sl}|{amt}|{side}|{mode}
            # Note: User requested "Tp" (capital T, lowercase p case matching handwritten note usually has TP or Tp, using Tp as per log request "Tp:2976") 
            log_str = f"Batch{self.batch_counter}-{i+1}:M:{market_p:.2f}|En:{current_limit_price:.2f}|Tp:{tp_px:.2f}|SL:{sl_px:.2f}|{target_notional}|{side_str}|{mode_str}"
            self.log(log_str, level="info")
            
            p_side_entry = "long" if signal == 1 else "short"
            # Pass TP/SL params for atomic placement
            entry_order_response = self._okx_place_order(self.config['symbol'], "Buy" if signal == 1 else "Sell", qty_contracts, price=current_limit_price, order_type="Limit", time_in_force="GoodTillCancel", posSide=p_side_entry, take_profit_price=tp_px, stop_loss_price=sl_px, verbose=False)

            if entry_order_response and entry_order_response.get('ordId'):
                order_id = entry_order_response['ordId']
                with self.position_lock:
                    self.pending_entry_ids.append(order_id)
                    self.pending_entry_order_id = order_id 
                    self.pending_entry_order_details[order_id] = {
                        'order_id': order_id,
                        'side': "Buy" if signal == 1 else "Sell",
                        'qty': qty_contracts * contract_size,
                        'limit_price': current_limit_price,
                        'signal': signal,
                        'order_type': 'Limit',
                        'status': 'New',
                        'placed_at': datetime.now(timezone.utc)
                    }
                
                # Trigger an immediate account info update to refresh values
                def _update_after_close():
                    try:
                        self._sync_account_data()
                        self._emit_socket_updates()
                    except Exception as e:
                        self.log(f"Error in close update: {e}", level="error")
                threading.Thread(target=_update_after_close, daemon=True).start()
                
                # Small delay between batch orders to prevent rate limiting
                if i < batch_size - 1:  # Don't delay after last order
                    time.sleep(0.2)
            else:
                self.log(f"Order placement failed", level="error")

    def _check_cancel_conditions(self):
        # Explicit check for cancel conditions as per nested loop logic
        
        loop_time = self.config.get('loop_time_seconds', 10) # Using existing param or maybe hardcode 90s check?
        # User diagram says: "Check Cancel Condition"
        # 1. More than 90 seconds (cancel_unfilled_seconds)
        # 2. TP < Market (for short) / TP > Market (for long) [Inverted Logic]
         #    User says: "TP < Market" mean TP price is lower than market.
         #    For SHORT: Entry is high. TP is low.
         #    If TP < Market, that's NORMAL for Short.
         #    User Correction: "Correct is tp price below market price but not market below tp"
         #    ... "For short safety line price and market price, bot also do reverse running"
         #    Let's stick to the Text Description in Logic:
         #    "Cancel-2: TP < Market: None"
         #    This implies checking if TP is < Market.
         #    For SHORT: TP < Entry. Market should be near Entry.
         #    If TP < Market (Market is higher than TP), that is normal state (Not reached TP yet).
         #    Maybe user means "Cancel if Market goes *beyond* TP"? i.e. Market < TP?
         #    Wait, "Cancel-2: TP < Market: None". If it was "Yes", it would cancel?
         #    If "TP < Market" is BAD for Short? No, TP < Market is GOOD (we are above TP).
         #    Maybe for LONG? For Long, TP > Entry. Market near Entry.
         #    If Market < TP is normal.
         #    If "TP < Market" (Market > TP). That means we missed it?
         #    Let's look at previous code: "cancel_on_tp_price_below_market".
         #    The standard logic: If Market moves such that the TP is no longer valid or "unfavorable"?
         #    Actually, for pending entry, we don't have a TP yet?
         #    Ah, we calculate "potential TP".
         #    If Potential TP is already "passed" by current market?
         #    Short: Entry=3000, TP=2900. Market=2800. We are already below TP. "Market < TP".
         #    User says "TP < Market". 2900 < 2800? False.
         #    If Market=2950. 2900 < 2950. True.
         #    So for Short, "TP < Market" is the NORMAL state.
         #    If "TP < Market" is the Cancel Condition, then it would always be true?
         #    Unless user means "Market < TP"? (Price dropped below target).
         #    "Correct is tp price below market price but not market below tp"
         #    This implies user WANTS to check "TP < Market".
         #    But if that cancels, it cancels everything normal.
         #    Maybe "TP > Market" for Short? (Price below TP).
         #    Let's assume the user meant "Price passed TP".
         #    Short: Cancel if Market < TP.
         #    Long: Cancel if Market > TP.
         
         #    However, implementing strictly as user described in log:
         #    "Cancel-2: TP<Market"
         #    I will code the log check.

        # self.log("Check Cancel Condition")
        
        # Log condition check (User Request)
        if self.monitoring_tick % 6 == 0:
             self.log("Check Cancel Condition")

        cancel_unfilled_seconds = self.config.get('cancel_unfilled_seconds', 90)
        
        with self.position_lock:
             active_ids = list(self.pending_entry_ids)
             details = dict(self.pending_entry_order_details)

        if not active_ids:
            self.log("No Orders to cancel", level="debug")
            return

        current_market_price = self._get_latest_data_and_indicators().get('current_price')
        if not current_market_price: return

        for order_id in active_ids:
            if order_id not in details: continue
            d = details[order_id]
            
            placed_at = d.get('placed_at')
            signal = d.get('signal') # 1 Long, -1 Short
            limit_price = d.get('limit_price')
            
            # 1. Time Check
            time_passed = False
            if placed_at and (datetime.now(timezone.utc) - placed_at).total_seconds() > cancel_unfilled_seconds:
                time_passed = True
            
            # self.log(f"Cancel-1:More than {cancel_unfilled_seconds} seconds: {'Yes' if time_passed else 'None'}")
            
            if time_passed:
                reason = f"Time Limit ({cancel_unfilled_seconds}s) reached"
                if self._okx_cancel_order(self.config['symbol'], order_id, reason=reason):
                    with self.position_lock:
                        if order_id in self.pending_entry_ids:
                             self.pending_entry_ids.remove(order_id)
                        if order_id in self.pending_entry_order_details:
                             del self.pending_entry_order_details[order_id]
                else:
                    # Robustness: If regular cancel failed, retry as Algo (standard for 19-digit IDs)
                    if self._okx_cancel_algo_order(self.config['symbol'], order_id):
                        with self.position_lock:
                            if order_id in self.pending_entry_ids:
                                 self.pending_entry_ids.remove(order_id)
                            if order_id in self.pending_entry_order_details:
                                 del self.pending_entry_order_details[order_id]
                continue

            # 2. TP Check (Missed Opportunity)
            tp_offset = self.config.get('tp_price_offset', 0)
            is_target_passed = False
            pending_tp = 0.0
            
            if tp_offset and safe_float(tp_offset) > 0:
                if signal == 1: # Long
                    pending_tp = limit_price + tp_offset
                    if current_market_price > pending_tp:
                        is_target_passed = True
                else: # Short
                    pending_tp = limit_price - tp_offset
                    if current_market_price < pending_tp:
                        is_target_passed = True

            # 3. Entry Check (Taker Avoidance / Directional Move)
            is_entry_unfavorable = False
            if signal == 1: # Long
                # Cancel if Entry < Market (Price moved up, making order a taker or too high)
                if current_market_price > limit_price:
                    is_entry_unfavorable = True
            else: # Short
                # Cancel if Entry > Market (Price moved down, making order a taker or too low)
                if current_market_price < limit_price:
                    is_entry_unfavorable = True

            # Execute Cancellation based on priority
            should_cancel = False
            cancel_msg = ""
            
            # Execute Cancellation based on literal config settings (Step 300)
            should_cancel = False
            cancel_msg = ""
            
            if time_passed:
                should_cancel = True
                cancel_msg = f"Time Limit ({cancel_unfilled_seconds}s) reached"
            
            # Short Specific (Literal Checks)
            elif signal == -1: 
                # Cancel if Entry price is below market price (Literal config)
                if self.config.get('cancel_on_entry_price_below_market') and limit_price < current_market_price:
                    should_cancel = True
                    cancel_msg = f"Short: Entry price below market (Entry {limit_price:.2f} < Market {current_market_price:.2f})"
                
                # Cancel if TP price is below market price (Literal config for Missed Opportunity)
                # Cancel if TP price is below market price (Literal config for Missed Opportunity)
                # Client REQUEST: Remove this logic/log as it is confusing.
                # elif self.config.get('cancel_on_tp_price_below_market') and is_target_passed:
                #    should_cancel = True
                #    cancel_msg = f"Short: TP price reached/passed before fill (TP {pending_tp:.2f} > Market {current_market_price:.2f})"
                pass
            
            # Long Specific (Literal Checks)
            elif signal == 1:
                # Cancel if Entry price is above market price
                if self.config.get('cancel_on_entry_price_above_market') and limit_price > current_market_price:
                    should_cancel = True
                    cancel_msg = f"Long: Entry price above market (Entry {limit_price:.2f} > Market {current_market_price:.2f})"
                
                # Cancel if TP price is above market price
                # Cancel if TP price is above market price
                # Client REQUEST: Remove this logic/log as it is confusing.
                # elif self.config.get('cancel_on_tp_price_above_market') and is_target_passed:
                #    should_cancel = True
                #    cancel_msg = f"Long: TP price reached/passed before fill (TP {pending_tp:.2f} < Market {current_market_price:.2f})"
                pass

            if should_cancel:
                # self.log(f"Cancel Order {order_id} ({cancel_msg})") # Already logged in _okx_cancel_order now
                if self._okx_cancel_order(self.config['symbol'], order_id, reason=cancel_msg):
                    with self.position_lock:
                        if order_id in self.pending_entry_ids:
                             self.pending_entry_ids.remove(order_id)
                        if order_id in self.pending_entry_order_details:
                             del self.pending_entry_order_details[order_id]
                continue
                 
         # Clean up local tracking
        with self.position_lock:
             # Basic cleanup of IDs that are gone happens in account update, but we can fast track here if needed
             pass


    def _unified_management_loop(self):
        # High-reliability background management
        self.log("Unified management thread started.", level="debug")
        last_account_sync = 0
        last_fills_sync = 0
        while not self.stop_event.is_set():
            now = time.time()
            try:
                # 1. High Frequency: Cancellation Check (every ~1s)
                # Note: Only cancel if trading is active or we still have tracked pending orders
                self._check_cancel_conditions()

                # 2. PnL-Based Auto-Exit Check
                # Removed: This is now handled authoritatively in _fetch_and_emit_account_info
                # to ensure atomic execution and correct 'Used Amount' calculation.

                
                # 3. Connection Health: Stale Price Monitor
                price_age = now - self.last_price_update_time
                if price_age > 30:
                     self.log(f"WARNING: Market price is STALE ({price_age:.1f}s). Re-initializing WebSocket...", level="warning")
                     # Reset update time to avoid spamming reconnects
                     self.last_price_update_time = now 
                     # Trigger reconnect by closing the current WebSocket
                     # We close BOTH to ensure any sync state is flushed and re-synced correctly.
                     self._close_websockets()
                
                # 3. Lower Frequency: Account Info & Emitting (every ~3s)
                # 3. Lower Frequency: Account Info & Emitting (every ~3s)
                if now - last_account_sync >= 3:
                    # NEW ARCHITECTURE: Split God Method
                    self._sync_account_data()
                    self._execute_position_management()
                    self._emit_socket_updates()
                    last_account_sync = now

                # 4. Low Frequency: Reconcile PnL from Fills (every ~60s)
                # This ensures "Net Trade Profit" includes actual fees as observed/requested by user.
                if now - last_fills_sync >= 60:
                    self._calculate_net_profit_from_fills()
                    last_fills_sync = now
                    
            except Exception as e:
                self.log(f"Error in unified mgmt loop: {e}", level="debug")
            
            time.sleep(1) # Base tick rate
        self.log("Unified management thread stopped.", level="debug")

    def _main_trading_logic(self):
        try:
            self.log("Trading loop started.", level="debug")

            while not self.stop_event.is_set():
                # Reconnection Trigger: Exit if WS is closed/changing
                # Reconnection Trigger: Exit if EITHER WS is closed/changing
                if not self.ws_public or not getattr(self.ws_public, 'sock', None) or not getattr(self.ws_public.sock, 'connected', False) or \
                   not self.ws_private or not getattr(self.ws_private, 'sock', None) or not getattr(self.ws_private.sock, 'connected', False):
                     self.log("WebSocket connection(s) lost or closed. Exiting trading loop for reconnect.", level="debug")
                     return

                if not self.is_running:
                    time.sleep(1)
                    continue

                # 1. Entry Loop
                while self.is_running and not self.stop_event.is_set():
                    self.log("-" * 90)
                    self.log("-" * 90)
                    self.log("Check Entry Condition")
                    market_data = self._get_latest_data_and_indicators()
                    if not market_data:
                        self.log("No market data", level="warning")
                        time.sleep(5)
                        continue
                        
                    valid_signals = self._check_entry_conditions(market_data)
                    
                    if valid_signals:
                        # Process all valid signals (e.g. could be both Long and Short)
                        for entry_info in valid_signals:
                             self._initiate_entry_sequence(entry_info['limit_price'], entry_info['signal'], self.config['batch_size_per_loop'])
                        
                        # Wait Loop Time
                        loop_time = self.config.get('loop_time_seconds', 10)
                        self.log(f"Wait {loop_time} seconds (Post-Entry)")
                        time.sleep(loop_time)
                    else:
                        self.log("Stop Orders (No passing signals in this cycle)")
                        break
                
                # 2. Cancel Check - Now handled by background thread
                # NO-OP here to prevent blocking main loop
                pass
                
                # 3. Delay before restarting cycle
                # Use standard loop_time for consistent heartbeat
                loop_time = self.config.get('loop_time_seconds', 10)
                self.log(f"Wait {loop_time} seconds before meta-loop restart")
                time.sleep(loop_time)

        except Exception as e:
            self.log(f"CRITICAL ERROR in _main_trading_logic: {e}", level="error")

        except Exception as e:
            self.log(f"CRITICAL ERROR in _main_trading_logic: {e}", level="error")

    def _initialize_websocket(self, type='public'):
        ws_url = self._get_ws_url(type)
        try:
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=lambda w: self._on_websocket_open(w, type),
                on_message=self._on_websocket_message,
                on_error=self._on_websocket_error,
                on_close=self._on_websocket_close
            )
            return ws
        except Exception as e:
            self.log(f"Exception initializing WebSocket ({type}): {e}", level="error")
            return None

    def _initialize_websocket_and_start_main_loop(self):
        self.log("OKX BOT STARTING (Dual WebSocket)", level="info")
        try:
            while not self.stop_event.is_set():
                try:
                    # 1. Initialize Public WS
                    if not self.ws_public:
                        self.ws_public = self._initialize_websocket('public')
                        self.ws_public_thread = threading.Thread(target=self.ws_public.run_forever, daemon=True)
                        self.ws_public_thread.start()
                        self.log("Public WebSocket initiated.", level="info")

                    # 2. Initialize Private WS
                    if not self.ws_private:
                        self.ws_private = self._initialize_websocket('private')
                        self.ws_private_thread = threading.Thread(target=self.ws_private.run_forever, daemon=True)
                        self.ws_private_thread.start()
                        self.log("Private WebSocket initiated.", level="info")

                    # Wait for subscriptions
                    self.log("Syncing with market data and account...", level="debug")
                    if not self.ws_subscriptions_ready.wait(timeout=30):
                        self.log("WebSocket subscriptions timed out. Retrying...", level="error")
                        self._close_websockets()
                        time.sleep(5)
                        continue

                    # Bot logic remains same...
                    # Fetch historical data...
                    timeframe = self.config.get('candlestick_timeframe', '1m')
                    interval_sec = self.intervals.get(timeframe, 60)
                    start_dt = datetime.now(timezone.utc) - timedelta(seconds=interval_sec * 300)
                    end_dt = datetime.now(timezone.utc)
                    start_ts_ms = int(start_dt.timestamp() * 1000)
                    end_ts_ms = int(end_dt.timestamp() * 1000)
                    
                    # Call definition updated to support MS timestamps directly or we update method to handle it
                    # For now, let's keep the date strings but fix the 'today' issue by ensuring end_dt is inclusive.
                    self._fetch_initial_historical_data(self.config['symbol'], timeframe, start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S'))
                    
                    self.bot_startup_complete = True
                    self.log("Bot startup sequence complete.", level="info")

                    self._periodic_account_info_update(initial_fetch=True)
                    self._calculate_net_profit_from_fills()
        
                    if not getattr(self, 'account_info_updater_thread', None) or not self.account_info_updater_thread.is_alive():
                        self.account_info_updater_thread = threading.Thread(target=self._periodic_account_info_update, args=(False,), daemon=True)
                        self.account_info_updater_thread.start()
                    
                    if not getattr(self, 'mgmt_thread', None) or not self.mgmt_thread.is_alive():
                        self.mgmt_thread = threading.Thread(target=self._unified_management_loop, daemon=True)
                        self.mgmt_thread.start()

                    self._main_trading_logic()
                    
                    if self.stop_event.is_set(): break
                    self.log("Main trading logic returned. Reconnecting in 5s...", level="info")
                    self._close_websockets()
                    time.sleep(5)

                except Exception as loop_e:
                    self.log(f"Error in Connection Loop: {loop_e}", level="error")
                    self._close_websockets()
                    time.sleep(5)

        except Exception as e:
            self.log(f"CRITICAL ERROR: {e}", level="error")
        finally:
            self.stop_event.set()
            self._close_websockets()
            self.log("OKX BOT SHUTDOWN COMPLETE", level="info")

    def _close_websockets(self):
        for ws in [self.ws_public, self.ws_private]:
            if ws:
                try: ws.close()
                except: pass
        self.ws_public = None
        self.ws_private = None
 
    def _calculate_net_profit_from_fills(self):
        # Fetch recent fills to calculate actual PnL
        try:
            params = {
                "instType": "SWAP",
                "instId": self.config['symbol'],
                "limit": "100"
            }
            # Use /trade/fills for recent activity (last 3 days)
            path_recent = "/api/v5/trade/fills"
            response = self._okx_request("GET", path_recent, params=params)
            
            # Local session PnL (resets every start)
            session_pnl = 0.0
            
            if response and response.get('code') == '0':
                fills = response.get('data', [])
                
                # [USER REQUEST] "remove handling position using state alway use live data"
                # We calculate PnL from the last 100 fills provided by the exchange, regardless of start time.
                # This ensures the metric IS live data and persists across restarts.
                start_time_limit = 0 

                # Reset persistent trade analytics before re-calculating from the limit window (Simple approach)
                # Note: This makes "Session PnL" effectively "Recent History PnL" (last 100 trades).
                temp_total_profit = 0.0
                temp_total_loss = 0.0
                temp_actual_fees = 0.0
                fill_count = 0
                
                for fill in fills:
                     fill_ts = int(fill.get('ts', 0))
                     if fill_ts >= start_time_limit:
                         fill_count += 1
                         pnl = safe_float(fill.get('pnl', 0))
                         fee = safe_float(fill.get('fee', 0))
                         fill_net = pnl + fee
                         session_pnl += fill_net
                         
                         # Accumulate actual fees for ALL trades (Entry + Exit)
                         temp_actual_fees += abs(fee)

                         # Realized Analytics: Only count fills where a position was actually reduced or closed (pnl != 0)
                         # This prevents entry fees from showing up as "Trade Loss" before any trades are closed.
                         if pnl != 0:
                             if fill_net > 0:
                                 temp_total_profit += fill_net
                             else:
                                 temp_total_loss += abs(fill_net)
                
                if fill_count > 0:
                    self.log(f"Synced PnL from Last {fill_count} Fills (Live History): Net ${temp_total_profit - temp_total_loss:.2f} | Fees: ${temp_actual_fees:.4f}", level="debug")

                self.total_trade_profit = temp_total_profit
                self.total_trade_loss = temp_total_loss
                self.net_trade_profit = temp_total_profit - temp_total_loss
                self.total_realized_fees = temp_actual_fees
                
                self._save_analytics()
                
                # ---------------------------------------------------------
                # LIVE STATE RECONSTRUCTION (User Request)
                # ---------------------------------------------------------
                # Reconstruct 'Step Count' and 'Last Add Price' directly from recent fills
                # to avoid relying on stale disk state.
                
                # 1. Determine current active side (if any)
                current_side = None
                with self.position_lock:
                    if self.in_position['long']: current_side = 'long'
                    elif self.in_position['short']: current_side = 'short'
                
                rec_step_count = 0
                rec_last_add = 0.0
                
                if current_side:
                    # Filter fills for current side (Buy=Long, Sell=Short)
                    target_side_str = 'buy' if current_side == 'long' else 'sell'
                    
                    # Sort fills by time DESC (Newest First)
                    # OKX fills are usually returned newest first, but ensure it.
                    sorted_fills = sorted(fills, key=lambda x: int(x.get('ts', 0)), reverse=True)
                    
                    found_fills_count = 0
                    
                    for fill in sorted_fills:
                        f_side = fill.get('side').lower()
                        f_pnl = safe_float(fill.get('pnl', 0))
                        f_sz = safe_float(fill.get('sz', 0))
                        
                        if f_side == target_side_str:
                            # It's an Entry/Add or a Close/Reduce?
                            # OKX 'side' is the direction of the trade relative to the BOOK.
                            # For Long: Buy is Entry, Sell is Exit.
                            # For Short: Sell is Entry, Buy is Exit.
                            
                            # But wait, 'pnl' field usually indicates if it closed a position?
                            # If pnl != 0, it was a closing trade.
                            # If pnl == 0, it was an opening trade.
                            
                            if f_pnl == 0:
                                # This is an ADD/ENTRY
                                # Capture the price of the NEWEST entry (last add).
                                # Only set if currently 0 (meaning we haven't found the newest valid one yet)
                                if rec_last_add == 0.0:
                                    # Try multiple possible price fields for robustness (Prioritize fillPx as authoritative)
                                    raw_px = fill.get('fillPx') or fill.get('px') or fill.get('avgPx')
                                    rec_last_add = safe_float(raw_px)
                                    if rec_last_add > 0:
                                        self.log(f"DEBUG: Found First Add Fill. raw: {fill} | px: {raw_px} | rec_last_add: {rec_last_add}", level="debug")

                                found_fills_count += 1
                                rec_step_count = found_fills_count # Update step count as we find valid adds
                            else:
                                # This is a Reduce/Close. The contiguous block of "Adds" ends here.
                                # (Or technically, started after this reduce).
                                break
                        else:
                            # Opposite side trade (e.g. Sell for Long)
                            # This breaks the contiguous Add chain.
                            break
                    
                    if found_fills_count > 0:
                        rec_step_count = found_fills_count - 1
                        self.log(f"Live State Reconstructed: Step {rec_step_count} | Last Add: {rec_last_add}", level="debug")
                        
                        # Apply to State
                        self.auto_add_step_count = rec_step_count
                        self.last_add_price = rec_last_add
                    else:
                        # Position exists but no recent fills found?
                        # Maybe legacy position. Default to Step 0.
                        self.log("Live State: Position active but no recent entry fills found. Defaulting to Step 0.", level="warning")
                        self.auto_add_step_count = 0
                        self.last_add_price = self.latest_trade_price or 0.0 # Safety fallback
                else:
                    # No position active -> Reset state
                    self.auto_add_step_count = 0
                    self.last_add_price = 0.0

            return session_pnl

        except Exception as e:
            self.log(f"Exception in _calculate_net_profit_from_fills: {e}", level="error")
            return 0.0

    def _load_analytics(self):
        try:
            import os
            import json
            if os.path.exists(self.analytics_path):
                with open(self.analytics_path, 'r') as f:
                    data = json.load(f)
                    self.daily_reports = data.get('daily_reports', [])
            else:
                self.daily_reports = []
        except Exception as e:
            self.log(f"Error loading analytics: {e}", level="error")

    def _save_analytics(self):
        try:
            import json
            data = {
                'daily_reports': self.daily_reports
            }
            with open(self.analytics_path, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.log(f"Error saving analytics: {e}", level="error")

    def _check_and_save_daily_report(self):
        """Snapshots daily performance at UTC midnight."""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')
        
        # Check if already saved for today
        if self.daily_reports and self.daily_reports[-1].get('date') == today_str:
            return

        # Prepare new report
        prev_capital = self.daily_reports[-1].get('total_capital', self.total_equity) if self.daily_reports else self.total_equity
        compound_interest = (self.total_equity / prev_capital) if prev_capital > 0 else 1.0

        report = {
            'date': today_str,
            'total_capital': self.total_equity,
            'net_trade_profit': self.net_trade_profit,
            'compound_interest': round(compound_interest, 4)
        }
        
        self.daily_reports.append(report)
        self.log(f"📅 Daily Report Saved for {today_str}: Capital ${self.total_equity:.2f}, Net Profit ${self.net_trade_profit:.2f}", level="info")
        self._save_analytics()

    # Removed _save_position_state and _load_position_state to enforce Live Data Only logic.


    def _periodic_account_info_update(self, initial_fetch=False):
        if initial_fetch:
            # Perform a single fetch and return
            self._sync_account_data()
            self._emit_socket_updates()
            return

        while not self.stop_event.is_set():
            try:
                # Audit Fix: Yield to the management loop if the bot is running
                # to avoid redundant sync calls and emission spam.
                if not self.is_running:
                    self._sync_account_data()
                    self._emit_socket_updates()
                else:
                    # Management loop is already syncing; just sleep
                    pass
            except Exception as e:
                self.log(f"Error in periodic account info update: {e}", level="error")
            finally:
                time.sleep(self.config.get('account_update_interval_seconds', 10))

    def safe_emit(self, event, data, throttle_seconds=0):
        """
        Sends data to the frontend via Socket.IO with optional throttling.
        Ensures the payload is JSON serializable and catches emission errors.
        """
        now = time.time()
        
        # Throttling logic (optional per event)
        if throttle_seconds > 0:
            last_time = getattr(self, f'_last_emit_{event}', 0.0)
            if now - last_time < throttle_seconds:
                return
            setattr(self, f'_last_emit_{event}', now)

        try:
            # Force serializability check / cleanup
            # We can use a custom approach or just trust the standard emit
            # if we ensure objects like datetime are strings.
            self.emit(event, data)
        except Exception as e:
            # self.log(f"Socket.IO Emit Error ({event}): {e}", level="debug")
            pass

    def _sync_account_data(self):
        """
        Phase 1 of Unified Loop: Read-Only Data Sync.
        Fetches Balance, Pending Orders, and Positions.
        Updates internal state via API but triggers NO trades.
        """
        self.monitoring_tick += 1
        
        # 1. Fetch account balance (Skip if WebSocket is providing fresh data)
        ws_freshness_threshold = 30 # seconds
        is_ws_fresh = (time.time() - getattr(self, 'last_account_sync_time', 0)) < ws_freshness_threshold
        
        response_balance = None
        if not is_ws_fresh:
            path_balance = "/api/v5/account/balance"
            params_balance = {"ccy": "USDT"} 
            response_balance = self._okx_request("GET", path_balance, params=params_balance)
        # else:
            # self.log(f"Skipping REST balance sync (WS fresh: {int(time.time() - self.last_account_sync_time)}s ago)", level="debug")

        if response_balance and response_balance.get('code') == '0':
            with self.account_info_lock:
                data = response_balance.get('data', [])
                if data and len(data) > 0:
                    account_details = data[0]
                    self.total_equity = safe_float(account_details.get('totalEq', '0'))
                    for detail in account_details.get('details', []):
                        if detail.get('ccy') == 'USDT':
                            self.account_balance = safe_float(detail.get('availBal', '0'))
                            self.available_balance = safe_float(detail.get('availBal', '0'))
                            self.log(f"REST Balance Sync | Equity: ${self.total_equity:.2f} | Avail: ${self.account_balance:.2f}", level="debug")
                            break
                else:
                    self.log("REST Balance Sync | Success but no data found.", level="debug")
        elif response_balance:
            self.log(f"REST Balance Sync | API Error: {response_balance.get('msg')} (Code: {response_balance.get('code')})", level="error")

        # 2. Fetch open orders (pending orders)
        path_pending_orders = "/api/v5/trade/orders-pending"
        params_pending_orders = {"instType": "SWAP", "instId": self.config['symbol']}
        response_pending_orders = self._okx_request("GET", path_pending_orders, params=params_pending_orders)
        
        formatted_open_trades = []
        if response_pending_orders and response_pending_orders.get('code') == '0':
            pending_orders = response_pending_orders.get('data', [])
            contract_size = self.product_info.get('contractSize', 1.0)
            if contract_size <= 0: contract_size = 1.0

            for order in pending_orders:
                ord_id = order.get('ordId') or order.get('algoId')
                
                # Exclude Reduce-Only orders (Exits) from being adopted as Pending Entries
                if order.get('reduceOnly') == 'true':
                    continue
                
                # Adoption Logic
                with self.position_lock:
                    if ord_id not in self.pending_entry_ids:
                        self.pending_entry_ids.append(ord_id)
                        c_time_ms = int(order.get('cTime', time.time() * 1000))
                        placed_at_dt = datetime.fromtimestamp(c_time_ms / 1000.0, tz=timezone.utc)
                        self.pending_entry_order_details[ord_id] = {
                            'order_id': ord_id,
                            'side': order.get('side').capitalize(),
                            'qty': safe_float(order.get('sz')) * contract_size,
                            'limit_price': safe_float(order.get('px')),
                            'signal': 1 if order.get('side') == 'buy' else -1,
                            'order_type': order.get('ordType', 'Limit'),
                            'status': order.get('state'),
                            'placed_at': placed_at_dt
                        }

                # Time left logic
                time_left = None
                cancel_unfilled_seconds = self.config.get('cancel_unfilled_seconds', 90)
                current_placed_at = None
                with self.position_lock:
                    if ord_id in self.pending_entry_order_details:
                         current_placed_at = self.pending_entry_order_details[ord_id].get('placed_at')

                if current_placed_at:
                    seconds_passed = (datetime.now(timezone.utc) - current_placed_at).total_seconds()
                    time_left = max(0, int(cancel_unfilled_seconds - seconds_passed))

                formatted_open_trades.append({
                    'type': order.get('side').capitalize(),
                    'id': ord_id,
                    'entry_spot_price': safe_float(order.get('px')),
                    'stake': safe_float(order.get('sz')) * safe_float(order.get('px')) * contract_size,
                    'tp_price': None,
                    'sl_price': None,
                    'status': order.get('state'),
                    'instId': order.get('instId'),
                    'time_left': time_left
                })
        
        with self.position_lock:
            for s_key in ['long', 'short']:
                if self.in_position[s_key]:
                    formatted_open_trades.append({
                        'type': s_key.capitalize(),
                        'id': f"POS-{s_key.upper()}",
                        'entry_spot_price': self.position_entry_price[s_key],
                        'stake': abs(self.position_qty[s_key]) * self.position_entry_price[s_key],
                        'tp_price': self.current_take_profit.get(s_key),
                        'sl_price': self.current_stop_loss.get(s_key),
                        'status': 'FILLED',
                        'instId': self.config['symbol'],
                        'time_left': None
                    })

        with self.trade_data_lock:
            self.open_trades = formatted_open_trades
            
        # 3. Fetch open positions (POLLING REMOVED - User Request: Pure WS)
        # WS now handles self.position_details, self.in_position, self.cached_* metrics
        # via _process_position_push and _on_websocket_message.
        # This prevents "flickering" from stale REST data.
        
        # We assume self.cached_pos_notional is kept up to date by WS.
        pass


        # Sync pending_entry_ids
        active_okx_ids = [t['id'] for t in formatted_open_trades]
        with self.position_lock:
            existing_pending = list(self.pending_entry_ids)
            for p_id in existing_pending:
                if p_id not in active_okx_ids:
                    self.pending_entry_ids.remove(p_id)
                    if p_id in self.pending_entry_order_details:
                        del self.pending_entry_order_details[p_id]
                    self.log(f"Pending order {p_id} cleared from tracking.", level="debug")
                    self._should_update_tpsl = True

        if getattr(self, '_should_update_tpsl', False) and any(self.in_position.values()) and self.is_running:
            self._should_update_tpsl = False
            # Call TP/SL modification to sync with new average price
            threading.Thread(target=self.batch_modify_tpsl, daemon=True).start()
        else:
             # Logic to capture initial capital if needed
             if self.initial_total_capital <= 0 and self.account_balance > 0:
                  self.initial_total_capital = self.account_balance
        
        # Calculate Need Add metrics (Viz) - Moved here to ensure update during UI-only loops
        self._calculate_need_add_metrics(getattr(self, 'cached_pos_notional', 0.0))

    def _check_auto_add_position_step(self, current_price, current_side, remaining_margin_budget):
        """
        Executes an Auto-Add order with Step 2+ logic.
        """
        # 1. Check Max Count
        max_count = self.config.get('add_pos_max_count', 3)
        if self.auto_add_step_count >= max_count:
            # self.log(f"Auto-Add: Max count reached ({self.auto_add_step_count}/{max_count}). Skipping.", level="debug")
            return

        # 2. Determine Size % based on Step
        # Step 0 (First Add, displayed as Step 2 in UI logic usually) -> Use 'add_pos_size_pct'
        # Step 1+ (Second Add+) -> Use 'add_pos_size_pct_2'
        size_pct = 0.0
        if self.auto_add_step_count == 0:
            size_pct = self.config.get('add_pos_size_pct', 100.0)
        else:
            size_pct = self.config.get('add_pos_size_pct_2', 30.0)

        okx_pos_notional = getattr(self, 'cached_pos_notional', 0.0)
        if okx_pos_notional <= 0: return

        # Amount to add = Position Size * (Pct / 100)
        amount_to_add_usdt = okx_pos_notional * (size_pct / 100.0)

        # 4. Safety Check against Budget
        # Logic: We use Total Capital 2nd as the budget.
        if amount_to_add_usdt > remaining_margin_budget:
             self.log(f"Auto-Add: Insufficient Capital 2nd. Needed {amount_to_add_usdt:.2f}, Has {remaining_margin_budget:.2f}. Capping.", level="warning")
             amount_to_add_usdt = remaining_margin_budget
        
        if amount_to_add_usdt < 2.0: # Min order size safety (loose check, exchange strict check happens later)
             # self.log(f"Auto-Add: Amount {amount_to_add_usdt:.2f} too small. Skipping.", level="debug")
             return

        # 5. Execute Order (Market)
        contract_size = self.product_info.get('contractSize', 1.0)
        if current_price <= 0: return
        qty_contracts = amount_to_add_usdt / current_price / contract_size
        
        lot_size = safe_float(self.product_info.get('qtyStepSize', 1.0))
        qty_contracts = math.floor(qty_contracts / lot_size) * lot_size
        
        if qty_contracts <= 0: return

        self.log(f"AUTO-ADD EXECUTING: Step {self.auto_add_step_count + 1} | Size: {size_pct}% (${amount_to_add_usdt:.2f}) | Price: {current_price}", level="WARNING")
        
        order_side = "Buy" if current_side == 'long' else "Sell"
        res = self._okx_place_order(
            self.config['symbol'], 
            order_side, 
            qty_contracts, 
            order_type="Market", 
            reduce_only=False,
            verbose=True
        )

        if res and res.get('ordId'):
            self.auto_add_step_count += 1
            self.cumulative_margin_used += amount_to_add_usdt
            self._save_position_state()
            self.log(f"Auto-Add Order Placed. New Step Count: {self.auto_add_step_count}", level="info")
        else:
            self.log("Auto-Add Order Failed.", level="error")

    def _execute_position_management(self):
        """
        Phase 2 of Unified Loop: Trading Logic & Metrics.
        Calculates Margins, PnL, and triggers Auto-Add/Auto-Exit.
        CONTAINS CRITICAL FIX FOR AUTO-ADD GATING.
        """
        # Recover metrics from Sync Phase
        # Bot-initiated volume for current session
        used_amount_notional = self.session_bot_used_notional
        okx_pos_notional = getattr(self, 'cached_pos_notional', 0.0)
        total_unrealized_pnl = getattr(self, 'cached_unrealized_pnl', 0.0)
        active_positions_count = getattr(self, 'cached_active_positions_count', 0)
        
        # Add pending orders to Used Amount
        with self.trade_data_lock:
            for trade in self.open_trades:
                 used_amount_notional += trade['stake']

        # Metric Calculations
        max_allowed_config = float(self.config.get('max_allowed_used', 1000.0))
        max_allowed_margin = max_allowed_config
        if self.total_equity > 0 and max_allowed_config > self.total_equity:
            max_allowed_margin = self.total_equity
            
        rate_divisor = self.config['rate_divisor']
        max_amount_margin = max_allowed_margin / rate_divisor
        self.max_allowed_display = max_allowed_margin
        self.max_amount_display = max_amount_margin
        
        leverage = float(self.config.get('leverage', 1))
        if leverage <= 0: leverage = 1
        
        self.remaining_amount_notional = max(0.0, (max_amount_margin * leverage) - used_amount_notional)
        
        with self.position_lock:
            self.used_amount_notional = used_amount_notional

        # Net Profit & Fee Calculation (CENTRALIZED)
        trade_fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0
        
        # 1. Size-Based Fees (Active Position only)
        self.size_fees = okx_pos_notional * trade_fee_pct
        
        # 2. Used-Based Fees (Active + Pending)
        self.used_fees = used_amount_notional * trade_fee_pct
        
        # 3. Total Fee (Alias for used_fees or historical session fees depending on context)
        # For the dashboard, trade_fees usually refers to session-wide or current exposure fees.
        self.trade_fees = self.used_fees
        
        # Real-time Net Profit (Floating)
        # Audit Fix: Subtract round-trip fees (2.0x) to show TRUE net profit
        self.net_profit = total_unrealized_pnl - (self.size_fees * 2.0)

        # Total Capital 2nd Logic
        if active_positions_count == 0:
            # Correct base_capital for reset if needed? No, user wants it to follow Total Capital.
            self.total_capital_2nd = self.total_equity
            self.cumulative_margin_used = 0.0
            self.auto_add_step_count = 0 
            self.last_add_price = 0.0
            # self._save_position_state() # REMOVED: Live Data Only
        else:
            # Sync last_add_price
            with self.position_lock:
                current_side = 'long' if self.in_position['long'] else 'short'
                current_avg = self.position_entry_price.get(current_side, 0.0)
                if self.last_add_price == 0:
                    self.last_add_price = current_avg
            
            base_capital = self.total_equity
            self.total_capital_2nd = max(0.0, base_capital - self.cumulative_margin_used)

        # ---------------------------------------------------------
        # AUTO-MARGIN LOGIC (Iterate active sides)
        # ---------------------------------------------------------
        if self.config.get('use_auto_margin', False):
            for side_key in ['long', 'short']:
                if self.in_position[side_key]:
                    pos = self.position_details.get(side_key, {})
                    liqp = self.position_liq[side_key]
                    mgn_mode = pos.get('mgnMode', 'cross')
                    
                    if mgn_mode == 'isolated' and liqp > 0:
                        sl_price = self.current_stop_loss[side_key]
                        should_add = False
                        
                        if side_key == 'long':
                            if sl_price > 0 and liqp >= sl_price: should_add = True
                        elif side_key == 'short':
                            if sl_price > 0 and liqp <= sl_price: should_add = True
                        
                        if should_add:
                            offset = self.config.get('auto_margin_offset', 30.0)
                            diff = abs(sl_price - liqp)
                            add_amt = diff + offset
                            raw_side = pos.get('posSide', 'net')
                            self.log(f"AUTO-MARGIN TRIGGERED [{side_key.upper()}]: Liq:{liqp} | SL:{sl_price} | Adding:{add_amt:.2f}", level="warning")
                            self._okx_adjust_margin(self.config['symbol'], raw_side, add_amt)

        # ---------------------------------------------------------
        # AUTO-ADD TRADING LOGIC (CRITICAL FIX APPLIED)
        # ---------------------------------------------------------
        # Only trigger if NOT in authoritative exit
        if not self.authoritative_exit_in_progress and okx_pos_notional > 0:
            
            # [CRITICAL FIX] Gate the logic with configuration check
            if self.config.get('use_add_pos_auto_cal', False):
                
                # ... (Existing Auto-Add Gap Logic) ...
                current_side = None
                with self.position_lock:
                     if self.in_position['long']: current_side = 'long'
                     elif self.in_position['short']: current_side = 'short'
                
                if current_side:
                     current_price = self.latest_trade_price
                     if self.last_add_price == 0:
                         self.last_add_price = self.position_entry_price[current_side]

                     if current_price:
                           # Step 2+ Gap Logic
                           current_step = getattr(self, 'auto_add_step_count', 0)
                           gap_threshold = 0.0
                           if current_step == 0:
                               gap_threshold = self.config.get('add_pos_gap_threshold', 5.0)
                           else:
                               gap_threshold = self.config.get('add_pos_gap_threshold_2', 5.0)

                           price_diff = 0.0
                           if current_side == 'long':
                               price_diff = self.last_add_price - current_price
                           else:
                               price_diff = current_price - self.last_add_price
                               
                           if price_diff >= gap_threshold:
                               self.log(f"Auto-Add Check (Step {current_step+1}): Gap Triggered: Diff {price_diff:.2f} >= {gap_threshold}. Current Avg: {self.last_add_price:.2f}", level="warning")
                               self.last_add_price = current_price 
                               remaining_margin_budget = self.total_capital_2nd
                               self._check_auto_add_position_step(current_price, current_side, remaining_margin_budget)

        # ---------------------------------------------------------
        # ---------------------------------------------------------
        # AUTO-EXIT LOGIC (Multiple Modes)
        # ---------------------------------------------------------
        # All modes now operate independently based on their enabled status.
        # ---------------------------------------------------------
        auto_exit_triggered = False
        exit_reason = ""
        current_size_fee = okx_pos_notional * trade_fee_pct if okx_pos_notional > 0 else 0.0

        # Check Auto-Manual Profit
        if self.config.get('use_pnl_auto_manual', False):
             manual_threshold = self.config.get('pnl_auto_manual_threshold', 100.0)
             if self.net_profit >= manual_threshold:
                 auto_exit_triggered = True
                 exit_reason = f"Auto-Manual Profit Target: ${self.net_profit:.2f} >= ${manual_threshold:.2f}"

        # Check Auto-Cal Profit
        if not auto_exit_triggered and self.config.get('use_pnl_auto_cal', False) and okx_pos_notional > 0:
            cal_times = self.config.get('pnl_auto_cal_times', 4)
            current_size_fee = okx_pos_notional * trade_fee_pct
            # Audit Fix: Use round-trip fees (2.0)
            cal_threshold = cal_times * (current_size_fee * 2.0)
            if self.net_profit >= cal_threshold:
                auto_exit_triggered = True
                exit_reason = f"Auto-Cal Profit Target: ${self.net_profit:.2f} >= ${cal_threshold:.2f} ({cal_times}x Round-Trip Fee)"

        # Check Auto-Cal Loss (Close All)
        if not auto_exit_triggered and self.config.get('use_pnl_auto_cal_loss', False) and okx_pos_notional > 0:
            loss_times = self.config.get('pnl_auto_cal_loss_times', 1.5)
            current_size_fee = okx_pos_notional * trade_fee_pct
            # Audit Fix: Use round-trip fees (2.0)
            loss_threshold = -((current_size_fee * 2.0) * loss_times)
            if self.net_profit <= loss_threshold:
                auto_exit_triggered = True
                exit_reason = f"Auto-Cal Loss Target: ${self.net_profit:.2f} <= ${loss_threshold:.2f} ({loss_times}x Round-Trip Fee)"

        # Check Auto-Cal Size (Profit)
        if not auto_exit_triggered and self.config.get('use_size_auto_cal', False) and okx_pos_notional > 0:
            size_times = self.config.get('size_auto_cal_times', 2.0)
            current_size_fee = okx_pos_notional * trade_fee_pct
            # Audit Fix: Use round-trip fees (2.0)
            size_target = (current_size_fee * 2.0) * size_times
            if self.net_profit >= size_target:
                auto_exit_triggered = True
                exit_reason = f"Auto-Cal Size Target: ${self.net_profit:.2f} >= ${size_target:.2f} ({size_times}x Round-Trip Size Fee)"

        # Check Auto-Cal Size (Loss)
        if not auto_exit_triggered and self.config.get('use_size_auto_cal_loss', False) and okx_pos_notional > 0:
            size_loss_times = self.config.get('size_auto_cal_loss_times', 1.5)
            current_size_fee = okx_pos_notional * trade_fee_pct
            # Audit Fix: Use round-trip fees (2.0)
            size_loss_threshold = -((current_size_fee * 2.0) * size_loss_times)
            if self.net_profit <= size_loss_threshold:
                auto_exit_triggered = True
                exit_reason = f"Auto-Cal Size Loss Target: ${self.net_profit:.2f} <= ${size_loss_threshold:.2f} ({size_loss_times}x Round-Trip Size Fee)"

        # MODE 2: Profit Target Exit (Unrealized PnL >= Size × Fee% × Multiplier)
        if not auto_exit_triggered and self.config.get('use_add_pos_profit_target', False) and okx_pos_notional > 0:
            profit_mult = self.config.get('add_pos_profit_multiplier', 1.5)
            # current_size_fee calculated above in line 3373
            # FIX: Use round-trip fees (2.0) to match the Limit Exit logic in _update_exit_orders
            target_pnl = (current_size_fee * 2.0) * profit_mult
            
            # Detailed logging every few ticks for debugging
            if self.monitoring_tick % 5 == 0:
                self.log(f"[Mode 2 Check] Size: ${okx_pos_notional:.2f} | Fee%: {trade_fee_pct * 100:.3f}% | Round-Trip Fee: ${current_size_fee * 2.0:.4f} | Target: ${target_pnl:.4f} | Unrealized PnL: ${total_unrealized_pnl:.4f}", level="debug")
            
            # SAFETY CHECK: Only exit if PnL is POSITIVE and >= target
            if total_unrealized_pnl > 0 and total_unrealized_pnl >= target_pnl:
                auto_exit_triggered = True
                exit_reason = f"Mode 2 Profit Target: Unrealized ${total_unrealized_pnl:.2f} >= ${target_pnl:.2f} ({profit_mult}x Round-Trip Fee)"

        # MODE 1: Break-Even Exit (PnL Above Zero)
        if not auto_exit_triggered and self.config.get('use_add_pos_above_zero', False) and okx_pos_notional > 0:
             # Audit Fix: Use round-trip fees (2.0) for threshold
             near_zero_threshold = max(1.0, (current_size_fee * 2.0) * 0.1)
             if self.net_profit >= -near_zero_threshold:
                 auto_exit_triggered = True
                 exit_reason = f"Mode 1 PnL Above Zero: Net ${self.net_profit:.2f} ≈ $0 (Fee Adjusted)"

        # ---------------------------------------------------------
        # Execute Authoritative Auto-Exit
        # ---------------------------------------------------------
        if auto_exit_triggered:
             with self.exit_lock:
                 if not self.authoritative_exit_in_progress:
                     self.log(f"[TARGET] AUTHORITATIVE AUTO-EXIT TRIGGERED: {exit_reason}", level="WARNING")
                     # Special logging for Mode 2 if it was the reason
                     if "Mode 2" in exit_reason:
                         self.log(f"[Mode 2 TRIGGER] Size: ${okx_pos_notional:.2f} | Target: ${target_pnl:.4f} | Unrealized: ${total_unrealized_pnl:.4f}", level="WARNING")
                     
                     threading.Thread(target=self._execute_trade_exit, args=(exit_reason,), daemon=True).start()

        # Need Add Calculation - Moved to _sync_account_data
        # self._calculate_need_add_metrics(okx_pos_notional)

        # Store metrics for Emitter
        # Note: self.max_allowed_display, self.max_amount_display, self.remaining_amount_notional are already set at the top of this function
        self.trade_fees = okx_pos_notional * trade_fee_pct # Corrected: trade_fee_pct already decimal

    def _calculate_need_add_metrics(self, okx_pos_notional):
        """Helper to calculate Need Add values."""
        self.need_add_usdt_profit_target = 0.0
        self.need_add_usdt_above_zero = 0.0
        
        if okx_pos_notional > 0:
            try:
                avg_entry = 0.0
                pos_side = 'long'
                with self.position_lock:
                    if self.in_position['long']: 
                        avg_entry = self.position_entry_price.get('long', 0)
                        pos_side = 'long'
                    elif self.in_position['short']:
                        avg_entry = self.position_entry_price.get('short', 0)
                        pos_side = 'short'
                
                if avg_entry > 0:
                    # Fallback chain: WS Price -> Cached Detail Price -> Entry (as last resort to avoid 0)
                    current_price = self.latest_trade_price
                    if not current_price or current_price <= 0:
                        # Try to get from cached position details if available
                        details = self.position_details.get(pos_side, {})
                        current_price = safe_float(details.get('lastPx'))
                        if not current_price or current_price <= 0:
                             current_price = avg_entry # Fallback to entry so denom calculation doesn't crash but metrics stay near 0

                    if current_price and current_price > 0:
                         recovery_pct = self.config.get('add_pos_recovery_percent', 0.6) / 100.0
                         
                         # Sensitivity Fix: Always show if price is against us
                         is_against = (pos_side == 'long' and current_price < avg_entry) or \
                                      (pos_side == 'short' and current_price > avg_entry)
                         
                         if is_against:
                             target_price_be = 0.0
                             if pos_side == 'long':
                                 target_price_be = current_price * (1 + recovery_pct)
                                 # Limit target to entry price if it would overshoot (stays sensitive)
                                 target_price_be = min(target_price_be, avg_entry - 0.00000001)
                                 
                                 denom = target_price_be - current_price
                                 if denom > 0:
                                     self.need_add_usdt_above_zero = okx_pos_notional * (avg_entry - target_price_be) / denom
                             else: # Short
                                 target_price_be = current_price * (1 - recovery_pct)
                                 # Limit target to entry price if it would overshoot
                                 target_price_be = max(target_price_be, avg_entry + 0.00000001)
                                 
                                 denom = current_price - target_price_be
                                 if denom > 0:
                                      self.need_add_usdt_above_zero = okx_pos_notional * (target_price_be - avg_entry) / denom
                             
                             # Mode 2: Profit Target
                             profit_mult = self.config.get('add_pos_profit_multiplier', 1.5)
                             trade_fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0
                             needed_gain_pct = trade_fee_pct * profit_mult
                             
                             if pos_side == 'long':
                                 target_avg_for_profit = target_price_be / (1 + needed_gain_pct)
                                 denom_profit = target_avg_for_profit - current_price
                                 if denom_profit > 0:
                                     self.need_add_usdt_profit_target = okx_pos_notional * (avg_entry - target_avg_for_profit) / denom_profit
                             else: # Short
                                 target_avg_for_profit = target_price_be / (1 - needed_gain_pct)
                                 denom_profit = current_price - target_avg_for_profit
                                 if denom_profit > 0:
                                     self.need_add_usdt_profit_target = okx_pos_notional * (target_avg_for_profit - avg_entry) / denom_profit

            except Exception as e:
                self.log(f"Error calculating Need Add: {e}", level="debug")

    def _emit_socket_updates(self):
        """
        Phase 3 of Unified Loop: Emitter.
        Sends calculated data to the frontend.
        """
        with self.trade_data_lock:
            current_trades = self.open_trades
            
        self.safe_emit('trades_update', {'trades': current_trades})

        # Emit 'position_update' for the central Position tab
        with self.position_lock:
            positions_info = {
                'long': {
                    'in': self.in_position.get('long', False),
                    'price': self.position_entry_price.get('long', 0.0),
                    'qty': self.position_qty.get('long', 0.0),
                    'tp': self.current_take_profit.get('long', 0.0),
                    'sl': self.current_stop_loss.get('long', 0.0),
                    'liq': self.position_liq.get('long', 0.0),
                    'upl': safe_float(self.position_details.get('long', {}).get('upl', 0.0))
                },
                'short': {
                    'in': self.in_position.get('short', False),
                    'price': self.position_entry_price.get('short', 0.0),
                    'qty': self.position_qty.get('short', 0.0),
                    'tp': self.current_take_profit.get('short', 0.0),
                    'sl': self.current_stop_loss.get('short', 0.0),
                    'liq': self.position_liq.get('short', 0.0),
                    'upl': safe_float(self.position_details.get('short', {}).get('upl', 0.0))
                }
            }
        
        self.safe_emit('position_update', {
            'positions': positions_info,
            'current_take_profit': self.current_take_profit,
            'current_stop_loss': self.current_stop_loss
        })

        # Emit 'account_update' with calculated targets for real-time sync
        # Calculate current auto-exit targets based on position size
        # Standardize fee multiplier (0.08 / 100 = 0.0008)
        trade_fee_pct_raw = self.config.get('trade_fee_percentage', 0.08)
        trade_fee_dec = trade_fee_pct_raw / 100.0
        
        okx_pos_notional = getattr(self, 'cached_pos_notional', 0.0)
        current_size_fee = okx_pos_notional * trade_fee_dec if okx_pos_notional > 0 else 0.0
        
        self.safe_emit('account_update', {
            'total_trades': getattr(self, 'cached_active_positions_count', 0) + self.total_trades_count,
            'total_capital': self.total_equity, 
            'total_capital_2nd': getattr(self, 'total_capital_2nd', self.total_equity),
            'total_balance': self.account_balance,
            'max_allowed_used_display': getattr(self, 'max_allowed_display', 0.0), 
            'max_amount_display': getattr(self, 'max_amount_display', 0.0),
            'used_amount': getattr(self, 'used_amount_notional', 0.0), 
            'remaining_amount': getattr(self, 'remaining_amount_notional', 0.0),
            'size_amount': okx_pos_notional,
            'available_balance': self.available_balance,
            'trade_fees': getattr(self, 'total_realized_fees', 0.0),
            # [USER FIX] map 'net_profit' to Gross Unrealized PnL to match Exchange Dashboard
            'net_profit': getattr(self, 'cached_unrealized_pnl', 0.0),
            # Send internal fee-adjusted metric as separate key if needed later
            'net_profit_inclusive': getattr(self, 'net_profit', 0.0),
            'total_trade_profit': self.total_trade_profit,
            'total_trade_loss': self.total_trade_loss,
            'net_trade_profit': self.net_trade_profit,
            'daily_reports': self.daily_reports,
            'need_add_usdt': getattr(self, 'need_add_usdt_profit_target', 0.0),
            'need_add_above_zero': getattr(self, 'need_add_usdt_above_zero', 0.0),
            # Real-time calculated targets (update when fee% or multiplier changes)
            'auto_cal_profit_target': self.config.get('pnl_auto_cal_times', 4) * current_size_fee,
            'auto_cal_loss_target': -self.config.get('pnl_auto_cal_loss_times', 1.5) * current_size_fee,
            'size_profit_target': self.config.get('size_auto_cal_times', 2.0) * current_size_fee,
            'size_loss_target': -self.config.get('size_auto_cal_loss_times', 1.5) * current_size_fee,
            'mode_2_profit_target': self.config.get('add_pos_profit_multiplier', 1.5) * current_size_fee
        })
        
        self._check_and_save_daily_report()
        
        # Debug Log
        if self.monitoring_tick % 10 == 0:
             used = getattr(self, 'used_amount_notional', 0.0)
             size = getattr(self, 'cached_pos_notional', 0.0)
             self.log(f"Account Update | Used: ${used:.2f} | Size: ${size:.2f}", level="debug")

    def fetch_account_data_sync(self):
        """Fetches account data synchronously and updates dashboard before start."""
        self._sync_server_time()
        self._fetch_product_info(self.config['symbol'])
        self._sync_account_data()
        self._execute_position_management()
        self._emit_socket_updates()

        # Sync pending_entry_ids with active orders from OKX
        active_okx_ids = [t['id'] for t in self.open_trades]
        with self.position_lock:
            existing_pending = list(self.pending_entry_ids)
            for p_id in existing_pending:
                if p_id not in active_okx_ids:
                    # Order is no longer on books (filled or cancelled)
                    self.pending_entry_ids.remove(p_id)
                    if p_id in self.pending_entry_order_details:
                        del self.pending_entry_order_details[p_id]
                    self.log(f"Pending order {p_id} cleared from tracking (Filled or Cancelled).", level="debug")
                    # We might want to trigger TP/SL update here too.
                    self._should_update_tpsl = True # Flag to update TP/SL if needed

        if getattr(self, '_should_update_tpsl', False) and any(self.in_position.values()) and self.is_running:
            self._should_update_tpsl = False
            # Call TP/SL modification to sync with new average price
            threading.Thread(target=self.batch_modify_tpsl, daemon=True).start()
            
            # REMOVED: self.initial_total_capital = total_balance reset. 
            # We want to keep the original capital to track net profit correctly.
        else:
            # Capture starting capital for the session if not set
            if self.initial_total_capital <= 0 and self.account_balance > 0:
                 self.initial_total_capital = self.account_balance
                 self.log(f"Session Started. Capture Total Capital: ${self.account_balance:.2f}", level="debug")
            else:
                 # Otherwise respect what's in config (or memory)
                 pass

        # Trade Fee Calculation: (Used + Remaining) * Fee_Percentage
        trade_fee_pct = self.config.get('trade_fee_percentage', 0.07)
        used_fee = self.used_amount_notional * (trade_fee_pct / 100.0)
        remaining_fee = self.remaining_amount_notional * (trade_fee_pct / 100.0)
        trade_fees = used_fee + remaining_fee

        # Update persistent attributes for status retrieval
        # Note: self.max_allowed_display etc are already set in _execute_position_management
        self.trade_fees = trade_fees

        # Note: Auto-Add and Auto-Exit logic is handled in _execute_position_management
        # This method is only for initial sync on startup

        # Explicit Auto-Exit for Mode 1 (PnL Above Zero / Break-Even)
        # Mode 1 takes priority over Mode 2 (exits at break-even before waiting for profit)
        if self.config.get('use_add_pos_above_zero', False) and okx_pos_notional > 0:
             trade_fee_pct = self.config.get('trade_fee_percentage', 0.07)
             current_size_fee = okx_pos_notional * (trade_fee_pct / 100.0)
             
             # Define "near zero" threshold: $1 or 10% of size fee (whichever is larger)
             # This prevents premature exit while ensuring we exit close to break-even
             near_zero_threshold = max(1.0, current_size_fee * 0.1)
             
             # Check if PnL is above the negative threshold (approaching break-even)
             # Example: If threshold is $2, exit when PnL >= -$2 (i.e., loss is $2 or less)
             if self.net_profit >= -near_zero_threshold:
                 exit_reason = f"Mode 1 PnL Above Zero: ${self.net_profit:.2f} ≈ $0 (threshold: ${near_zero_threshold:.2f})"
                 with self.exit_lock:
                     if not self.authoritative_exit_in_progress:
                         self.log(f"Mode 1 Check {exit_reason}. Auto-Closing Position...", level="WARNING")
                         threading.Thread(target=self._execute_trade_exit, args=(exit_reason,), daemon=True).start()

    def test_api_credentials(self):
        # Store current global API settings
        
        original_okx_api_key = self.okx_api_key
        original_okx_api_secret = self.okx_api_secret
        original_okx_passphrase = self.okx_passphrase
        original_okx_simulated_trading_header = self.okx_simulated_trading_header

        try:
            # Set global API settings for testing based on self.config (which was modified by app.py)
            use_dev = self.config.get('use_developer_api', False)
            use_demo = self.config.get('use_testnet', False)

            if use_dev:
                if use_demo:
                    self.okx_api_key = self.config.get('dev_demo_api_key', '')
                    self.okx_api_secret = self.config.get('dev_demo_api_secret', '')
                    self.okx_passphrase = self.config.get('dev_demo_api_passphrase', '')
                else:
                    self.okx_api_key = self.config.get('dev_api_key', '')
                    self.okx_api_secret = self.config.get('dev_api_secret', '')
                    self.okx_passphrase = self.config.get('dev_passphrase', '')
            else:
                if use_demo:
                    self.okx_api_key = self.config.get('okx_demo_api_key', '')
                    self.okx_api_secret = self.config.get('okx_demo_api_secret', '')
                    self.okx_passphrase = self.config.get('okx_demo_api_passphrase', '')
                else:
                    self.okx_api_key = self.config.get('okx_api_key', '')
                    self.okx_api_secret = self.config.get('okx_api_secret', '')
                    self.okx_passphrase = self.config.get('okx_passphrase', '')

            if use_demo:
                self.okx_simulated_trading_header = {'x-simulated-trading': '1'}
            else:
                self.okx_simulated_trading_header = {}

            # Attempt a simple API call, e.g., get account balance
            path_balance = "/api/v5/account/balance"
            params_balance = {"ccy": "USDT"}
            response_balance = self._okx_request("GET", path_balance, params=params_balance, max_retries=1) # Only 1 retry for test

            if response_balance and response_balance.get('code') == '0':
                return True
            else:
                return False
        except Exception as e:
            self.log(f"Error during API credential test: {e}", level="error")
            return False
        finally:
            # Restore original global API settings
            self.okx_api_key = original_okx_api_key
            self.okx_api_secret = original_okx_api_secret
            self.okx_passphrase = original_okx_passphrase
            self.okx_simulated_trading_header = original_okx_simulated_trading_header

    def batch_modify_tpsl(self):
        # Audit Fix: Prevent concurrent batch updates to save rate limits
        with self.batch_update_lock:
            if self.batch_update_in_progress:
                self.log("Batch TP/SL update already in progress. Skipping.", level="debug")
                return
            self.batch_update_in_progress = True

        self.log("Initiating batch TP/SL modification...", level="debug")
        try:
            latest_data = self._get_latest_data_and_indicators()
            if not latest_data:
                self.log("Could not get current market price for batch TP/SL modification.", level="debug")
                return
                
            current_market_price = latest_data.get('current_price')
            if current_market_price is None:
                self.log("Current market price is None for batch TP/SL modification.", level="debug")
                return

            path = "/api/v5/account/positions"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)

            if not response or response.get('code') != '0':
                self.log(f"Failed to fetch open positions for batch TP/SL modification: {response}", level="error")
                self.emit('error', {'message': f'Failed to batch modify TP/SL: Could not fetch open positions.'})
                return

            positions = response.get('data', [])
            modified_count = 0
            tp_price_offset = self.config['tp_price_offset']
            sl_price_offset = self.config['sl_price_offset']
            price_precision = self.product_info.get('pricePrecision', 4)
            qty_precision = self.product_info.get('qtyPrecision', 8)

            # Group positions by side for batch processing if needed, but here we loop
            for pos in positions:
                if pos.get('instId') == self.config['symbol']:
                    pos_qty = safe_float(pos.get('pos', '0'))
                    pos_side_raw = pos.get('posSide', 'net')
                    avg_px = safe_float(pos.get('avgPx', '0'))

                    if abs(pos_qty) > 0 and avg_px > 0:
                        # Map to our internal side key using exchange data
                        # Map to our internal side key using exchange data
                        if pos_side_raw == 'short':
                            side_key = 'short'
                        elif pos_side_raw == 'long':
                            side_key = 'long'
                        else: # 'net' mode
                             side_key = 'long' if pos_qty > 0 else 'short'

                        order_side = "sell" if side_key == 'long' else "buy"
                        new_tp = 0.0
                        new_sl = 0.0

                        # Safely calculate targets if offsets are provided
                        if tp_price_offset and safe_float(tp_price_offset) > 0:
                            if side_key == 'long':
                                new_tp = avg_px + safe_float(tp_price_offset)
                            else:
                                new_tp = avg_px - safe_float(tp_price_offset)
                        else:
                            self.log(f"Batch Sync: TP offset is null or 0 for {side_key.upper()}. Skipping TP calc.", level="debug")

                        if sl_price_offset and safe_float(sl_price_offset) > 0:
                            if side_key == 'long':
                                new_sl = avg_px - safe_float(sl_price_offset)
                            else:
                                new_sl = avg_px + safe_float(sl_price_offset)
                        else:
                            self.log(f"Batch Sync: SL offset is null or 0 for {side_key.upper()}. Skipping SL calc.", level="debug")

                        self.log(f"Syncing TP/SL for {side_key.upper()} position. Avg Price: {avg_px:.{price_precision}f}", level="debug")



                        with self.position_lock:
                            # 1. Fetch and cancel existing algo orders for this SYMBOL + SIDE
                            # Note: OKX allows filtering by posSide in some cases, but here we check all and filter locally
                            path_algo = "/api/v5/trade/orders-algo-pending"
                            params_algo = {"instType": "SWAP", "instId": self.config['symbol'], "ordType": "conditional"}
                            resp_algo = self._okx_request("GET", path_algo, params=params_algo)
                            
                            if resp_algo and resp_algo.get('code') == '0':
                                for algo_order in resp_algo.get('data', []):
                                    if algo_order.get('posSide') == pos_side_raw:
                                        self._okx_cancel_algo_order(self.config['symbol'], algo_order.get('algoId'))
                            
                            self.position_exit_orders[side_key] = {}
                            time.sleep(0.2) 

                            # Place new TP and SL
                            trig_px_type = self.config.get('trigger_price', 'last')
                            
                            if tp_price_offset and safe_float(tp_price_offset) > 0:
                                tp_body = {
                                    "instId": self.config['symbol'],
                                    "tdMode": self.config.get('mode', 'cross'),
                                    "side": order_side,
                                    "posSide": pos_side_raw,
                                    "ordType": "conditional",
                                    "sz": f"{abs(pos_qty):.{qty_precision}f}",
                                    "tpTriggerPx": f"{new_tp:.{price_precision}f}",
                                    "tpTriggerPxType": trig_px_type,
                                    "tpOrdPx": "-1",
                                    "reduceOnly": "true"
                                }

                                tp_order = self._okx_place_algo_order(tp_body, verbose=False)
                                if tp_order and (tp_order.get('algoId') or tp_order.get('ordId')):
                                    self.position_exit_orders[side_key]['tp'] = tp_order.get('algoId') or tp_order.get('ordId')
                                    self.log(f"[TARGET] {side_key.upper()} TP Set: {new_tp:.{price_precision}f}", level="info")
                            else:
                                self.log(f"Skipping TP batch modify for {side_key.upper()} (No offset)", level="debug")
                            
                            if sl_price_offset and safe_float(sl_price_offset) > 0:
                                sl_body = {
                                    "instId": self.config['symbol'],
                                    "tdMode": self.config.get('mode', 'cross'),
                                    "side": order_side,
                                    "posSide": pos_side_raw,
                                    "ordType": "conditional",
                                    "sz": f"{abs(pos_qty):.{qty_precision}f}",
                                    "slTriggerPx": f"{new_sl:.{price_precision}f}",
                                    "slTriggerPxType": trig_px_type,
                                    "slOrdPx": "-1",
                                    "reduceOnly": "true"
                                }

                                sl_order = self._okx_place_algo_order(sl_body, verbose=False)
                                if sl_order and (sl_order.get('algoId') or sl_order.get('ordId')):
                                    self.position_exit_orders[side_key]['sl'] = sl_order.get('algoId') or sl_order.get('ordId')
                                    self.log(f"[TARGET] {side_key.upper()} SL Set: {new_sl:.{price_precision}f}", level="info")
                            else:
                                self.log(f"Skipping SL batch modify for {side_key.upper()} (No offset)", level="debug")
                            
                            # Only count as modified if at least one order was placed
                            if (tp_price_offset and safe_float(tp_price_offset) > 0) or (sl_price_offset and safe_float(sl_price_offset) > 0):
                                self.current_take_profit[side_key] = new_tp
                                self.current_stop_loss[side_key] = new_sl
                                modified_count += 1
                                
                                # Emit side-specific update
                                self.emit('position_update', {
                                    'in_position': self.in_position[side_key],
                                    'position_entry_price': self.position_entry_price[side_key],
                                    'position_qty': self.position_qty[side_key],
                                    'current_take_profit': self.current_take_profit[side_key],
                                    'current_stop_loss': self.current_stop_loss[side_key],
                                    'side': side_key
                                })

            if modified_count > 0:
                self.log(f"Successfully modified TP/SL for {modified_count} sides.", level="info")
            else:
                self.log("No active positions found (or matched criteria) to modify TP/SL.", level="debug")
        
        except Exception as e:
            self.log(f"Exception in batch_modify_tpsl: {e}", level="error")
            self.emit('error', {'message': f'Failed to batch modify TP/SL: {str(e)}'})
        finally:
            with self.batch_update_lock:
                self.batch_update_in_progress = False
        self.log("Batch TP/SL modification complete.", level="debug")



    def batch_cancel_orders(self):
        self.log("Initiating batch order cancellation...", level="info")
        try:
            cancelled_count = 0
            
            # 1. Cancel Limit Orders
            path = "/api/v5/trade/orders-pending"
            params = {"instType": "SWAP", "instId": self.config['symbol']}
            response = self._okx_request("GET", path, params=params)

            if response and response.get('code') == '0':
                orders = response.get('data', [])
                for order in orders:
                    order_id = order.get('ordId')
                    if order_id:
                        if self._okx_cancel_order(self.config['symbol'], order_id):
                            cancelled_count += 1
                            time.sleep(0.1)

            # 2. Cancel Algo Orders (TP/SL/Conditional)
            path_algo = "/api/v5/trade/orders-algo-pending"
            params_algo = {
                "instType": "SWAP", 
                "instId": self.config['symbol'],
                "ordType": "conditional" # RESTORED: Required by OKX
            }
            response_algo = self._okx_request("GET", path_algo, params=params_algo)

            if response_algo and response_algo.get('code') == '0':
                algo_orders = response_algo.get('data', [])
                for algo_order in algo_orders:
                    algo_id = algo_order.get('algoId')
                    if algo_id:
                        if self._okx_cancel_algo_order(self.config['symbol'], algo_id):
                            cancelled_count += 1
                            time.sleep(0.1)

            if cancelled_count > 0:
                self.log(f"[DONE] Cancelled {cancelled_count} pending orders.", level="info")
            else:
                self.log("No orders to cancel.", level="warning")
                self.emit('warning', {'message': 'No pending orders found to cancel.'})

        except Exception as e:
            self.log(f"Exception in batch_cancel_orders: {e}", level="error")
            self.emit('error', {'message': f'Failed to batch cancel orders: {str(e)}'})
            self.log("Batch order cancellation complete.", level="info")

    def emergency_sl(self):
        self.log("🚨 EMERGENCY STOP LOSS TRIGGERED: Closing all positions and orders...", level="warning")
        try:
            # We use the exchange-authoritative exit logic to ensure EVERYTHING is closed
            self._execute_trade_exit("Manual Dashboard Trigger")
            self.emit('success', {'message': 'Emergency SL complete. All positions/orders cleared.'})
        except Exception as e:
            self.log(f"Error during Emergency SL: {e}", level="error")
            self.emit('error', {'message': f'Emergency SL failed: {e}'})
    def apply_live_config_update(self, new_config):
        """
        Dynamically applies certain config updates while the bot is running.
        Returns a dictionary with status and warning messages.
        """
        warnings = []
        old_symbol = self.config.get('symbol')
        new_symbol = new_config.get('symbol')
        old_lev = self.config.get('leverage')
        new_lev = new_config.get('leverage')
        old_pos_mode = self.config.get('okx_pos_mode')
        new_pos_mode = new_config.get('okx_pos_mode')

        # 1. Update internal config object
        self.config = new_config
        self.log("Applying live configuration updates (including new Auto-Add parameters)...", level="info")

        # 2. Handle Leverage Change
        if new_lev != old_lev:
            self.log(f"Leverage change detected: {old_lev} -> {new_lev}. Updating on exchange...", level="info")
            lev_success = False
            if new_pos_mode == 'long_short_mode':
                l_ok = self._okx_set_leverage(new_symbol, new_lev, pos_side="long")
                s_ok = self._okx_set_leverage(new_symbol, new_lev, pos_side="short")
                lev_success = l_ok and s_ok
            else:
                lev_success = self._okx_set_leverage(new_symbol, new_lev, pos_side="net")
            
            if lev_success:
                self.log(f"[DONE] Leverage successfully updated to {new_lev}x", level="info")
            else:
                warnings.append(f"Failed to update leverage to {new_lev}x on exchange.")

        # 3. Handle Symbol Change (Sensitive)
        if new_symbol != old_symbol:
            # Check for open positions
            in_pos = False
            with self.position_lock:
                # We check the authoritative state in self.in_position which is synced with the exchange
                in_pos = any(self.in_position.values())
            
            if in_pos:
                self.log(f"⚠️ Cannot change symbol to {new_symbol} while positions are open for {old_symbol}. Reverting symbol config.", level="warning")
                self.config['symbol'] = old_symbol
                warnings.append(f"Symbol change to {new_symbol} blocked: Please close existing positions for {old_symbol} first.")
            else:
                self.log(f"🔄 Switching symbol from {old_symbol} to {new_symbol}...", level="info")
                
                # Update subscription target
                self.subscribed_instrument = new_symbol
                
                # Stop WebSockets to clear old subscriptions
                self._close_websockets()
                
                # Fetch new product info
                if self._fetch_product_info(new_symbol):
                    # Set leverage for the new symbol
                    if new_pos_mode == 'long_short_mode':
                        self._okx_set_leverage(new_symbol, new_lev, pos_side="long")
                        self._okx_set_leverage(new_symbol, new_lev, pos_side="short")
                    else:
                        self._okx_set_leverage(new_symbol, new_lev, pos_side="net")
                    
                    self.log(f"[DONE] Successfully swapped to {new_symbol}.", level="info")
                else:
                    self.log(f"❌ Failed to fetch info for {new_symbol}. Reverting to {old_symbol}.", level="error")
                    self.config['symbol'] = old_symbol
                    warnings.append(f"Failed to switch to {new_symbol}: could not fetch product info.")
                    # Restart WS with old symbol if needed (it will restart automatically in the loop)

        return {"success": True, "warnings": warnings}
