from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
import json
import logging
import os
import threading
from bot_engine import TradingBotEngine

logging.basicConfig(
    level=logging.DEBUG, # Changed to DEBUG for more verbose logging
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET', 'dev-secret-key-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

config_file = 'config.json'
bot_engine = None

def load_config():
    with open(config_file, 'r') as f:
        return json.load(f)

def save_config(config):
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)

def emit_to_client(event, data):
    socketio.emit(event, data)

@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')


@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    config = load_config()
    return jsonify(config)

@app.route('/api/config', methods=['POST'])
def update_config():
    global bot_engine

    try:
        new_config = request.json
        current_config = load_config()

        # Whitelist of all valid parameters
        allowed_params = [
            'deriv_api_token', 'deriv_app_id', 'symbols',
            'use_fixed_balance', 'balance_value', 'max_daily_loss_pct',
            'entry_type', 'is_demo', 'log_level',
            'tp_enabled', 'tp_value', 'sl_enabled', 'sl_value',
            'force_close_enabled', 'force_close_duration',
            'active_strategy', 'contract_type', 'multiplier_value', 'custom_expiry'
        ]

        # Update current_config with only allowed and present keys from new_config
        updates_made = False
        for key, value in new_config.items():
            if key in allowed_params:
                # Type conversion safety could be added here if needed, but JSON usually handles it well enough for basic types
                if current_config.get(key) != value:
                    current_config[key] = value
                    updates_made = True

        if bot_engine and bot_engine.is_running:
             # Relaxed restrictions: Let the engine handle sensitive swaps dynamically
             # We only block things that absolutely cannot be changed (none currently identified as engine handles them)
             pass
        
        if updates_made:
            save_config(current_config)

            warning_msg = None
            if bot_engine:
                # Update the bot's internal config object and trigger dynamic updates
                result = bot_engine.apply_live_config_update(current_config)
                if result.get('warnings'):
                    warning_msg = " | ".join(result['warnings'])
                bot_engine.log("Configuration updated live from dashboard.", level="info")

            def background_init():
                global bot_engine
                # Ensure bot engine exists
                if not bot_engine:
                    bot_engine = TradingBotEngine(config_file, emit_to_client)

                # Start passive monitoring for balance if not trading
                if not bot_engine.is_running:
                    bot_engine.start(passive_monitoring=True)
                else:
                    bot_engine._apply_api_credentials()
                
                # Check if the currently selected credentials are valid
                valid, msg = bot_engine.check_credentials()
                if not valid:
                    emit_to_client('error', {'message': f'API Credentials Error: {msg}'})
            
            import threading
            threading.Thread(target=background_init, daemon=True).start()
            
            final_msg = 'Configuration updated successfully'
            if warning_msg:
                final_msg += f" (Note: {warning_msg})"
            
            return jsonify({'success': True, 'message': final_msg})
        else:
            return jsonify({'success': True, 'message': 'No changes detected'})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    global bot_engine
    if bot_engine:
        bot_engine.stop_bot()
    
    # Save config before shutdown
    config = load_config()
    save_config(config)
    
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        return jsonify({'success': False, 'message': 'Not running with the Werkzeug Server'})
    
    func()
    return jsonify({'success': True, 'message': 'Server shutting down...'})

@app.route('/api/download_logs')
def download_logs():
    try:
        log_file = 'debug.log'
        if not os.path.exists(log_file):
             return jsonify({'error': 'Log file not found'}), 404
        
        # Flush handlers to ensure latest logs are written
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.FileHandler): # Only flush file handlers
                handler.flush()
            
        return send_file(
            log_file,
            mimetype='text/plain',
            as_attachment=True,
            download_name='bot_log.log'
        )
    except Exception as e:
        logging.error(f'Error downloading logs: {str(e)}', exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/test_api_key', methods=['POST'])
def test_api_key_route():
    try:
        data = request.json
        api_token = data.get('api_token')

        if not api_token:
            return jsonify({'success': False, 'message': 'API Token is required.'}), 400

        # Temporarily create a bot_engine instance to test credentials
        temp_bot_engine = TradingBotEngine(config_file, emit_to_client)
        temp_bot_engine.config['deriv_api_token'] = api_token
        
        if temp_bot_engine.test_api_credentials():
            return jsonify({'success': True, 'message': 'API token is valid.'})
        else:
            return jsonify({'success': False, 'message': 'Invalid API token or connection error.'}), 401

    except Exception as e:
        logging.error(f'Error testing API token: {str(e)}', exc_info=True)
        return jsonify({'success': False, 'message': f'An unexpected error occurred: {str(e)}'}), 500


@app.route('/api/status', methods=['GET'])
def get_status():
    global bot_engine
    if not bot_engine:
        try:
            bot_engine = TradingBotEngine(config_file, emit_to_client)
            bot_engine.start(passive_monitoring=True)
        except Exception as e:
            logging.error(f"Error initializing bot engine for status: {e}")
            return jsonify({'running': False, 'error': str(e)}), 500

    # Trigger a sync if not running to get fresh data for dashboard
    if not bot_engine.is_running:
        try:
            bot_engine.fetch_account_data_sync()
        except Exception as e:
            logging.error(f"Error fetching sync account data: {e}")

    # Calculate trades and fees for emission
    total_active_trades_count = bot_engine.total_trades_count + len(bot_engine.open_trades)
    trade_fee_pct = bot_engine.config.get('trade_fee_percentage', 0.08)
    trade_fees = bot_engine.used_amount_notional * (trade_fee_pct / 100.0)

    return jsonify({
        'running': bot_engine.is_running,
        'total_balance': bot_engine.account_balance,
        'open_trades': bot_engine.open_trades,
        'net_profit': bot_engine.net_profit,
        'total_trades': total_active_trades_count,
        'trade_fees': bot_engine.trade_fees,
        'total_capital': bot_engine.total_equity,
        'used_amount': bot_engine.used_amount_notional,
        'remaining_amount': bot_engine.remaining_amount_notional,
        'max_allowed_used_display': bot_engine.max_allowed_display,
        'max_amount_display': bot_engine.max_amount_display,
        'in_position': bot_engine.in_position,
        'position_entry_price': bot_engine.position_entry_price,
        'position_qty': bot_engine.position_qty,
        'current_take_profit': bot_engine.current_take_profit,
        'current_stop_loss': bot_engine.current_stop_loss,
        # Standardize for the frontend if it expects dual positions
        'positions': {
            'long': {
                'in': bot_engine.in_position.get('long', False),
                'qty': bot_engine.position_qty.get('long', 0.0),
                'price': bot_engine.position_entry_price.get('long', 0.0)
            },
            'short': {
                'in': bot_engine.in_position.get('short', False),
                'qty': bot_engine.position_qty.get('short', 0.0),
                'price': bot_engine.position_entry_price.get('short', 0.0)
            }
        },
        'primary_in_position': any(bot_engine.in_position.values()),
        'total_capital_2nd': bot_engine.total_capital_2nd,
        'size_amount': bot_engine.used_amount_notional,
        'need_add_usdt': bot_engine.config.get('need_add_usdt', 0.0), # Temporary placeholder if not sync'd
        'need_add_above_zero': 0.0, # Will be updated by engine broadcast
        # Realized profit tracking
        'net_trade_profit': getattr(bot_engine, 'net_trade_profit', 0.0),
        'total_trade_profit': getattr(bot_engine, 'total_trade_profit', 0.0),
        'total_trade_loss': getattr(bot_engine, 'total_trade_loss', 0.0),
        'win_rate': (getattr(bot_engine, 'wins_count', 0) / bot_engine.total_trades_count * 100) if bot_engine.total_trades_count > 0 else 0,
        'avg_pnl': (getattr(bot_engine, 'net_trade_profit', 0) / bot_engine.total_trades_count) if bot_engine.total_trades_count > 0 else 0
    })
 
@socketio.on('connect')
def handle_connect(auth=None):
    global bot_engine
    sid = request.sid
    logging.info(f'Client connected: {sid}')
    emit('connection_status', {'connected': True}, room=sid)
 
    if not bot_engine:
        try:
            bot_engine = TradingBotEngine(config_file, emit_to_client)
            bot_engine.start(passive_monitoring=True)
        except Exception as e:
            logging.error(f"Error auto-initializing bot engine on connect: {e}")

    if bot_engine:
        emit('bot_status', {'running': bot_engine.is_running}, room=sid)
        # Trigger a sync to ensure metrics are fresh
        bot_engine.fetch_account_data_sync()

        payload = {
            'is_demo': bot_engine.config.get('is_demo', True),
            'total_capital': bot_engine.total_equity,
            'total_capital_2nd': bot_engine.total_capital_2nd,
            'max_allowed_used_display': bot_engine.max_allowed_display,
            'max_amount_display': bot_engine.max_amount_display,
            'used_amount': bot_engine.used_amount_notional,
            'size_amount': getattr(bot_engine, 'cached_pos_notional', 0.0),
            'trade_fees': bot_engine.trade_fees,
            'used_fees': getattr(bot_engine, 'used_fees', 0.0),
            'size_fees': getattr(bot_engine, 'size_fees', 0.0),
            'remaining_amount': bot_engine.remaining_amount_notional,
            'total_balance': bot_engine.account_balance,
            'available_balance': bot_engine.available_balance,
            'net_profit': bot_engine.net_profit,
            'total_trades': len(bot_engine.open_trades) + bot_engine.total_trades_count,
            'net_trade_profit': bot_engine.net_trade_profit,
            'total_trade_profit': bot_engine.total_trade_profit,
            'total_trade_loss': bot_engine.total_trade_loss,
            'win_rate': (bot_engine.wins_count / bot_engine.total_trades_count * 100) if bot_engine.total_trades_count > 0 else 0,
            'avg_pnl': (bot_engine.net_trade_profit / bot_engine.total_trades_count) if bot_engine.total_trades_count > 0 else 0
        }
        emit('account_update', payload, room=sid)
        
        emit('trades_update', {'trades': bot_engine.open_trades}, room=sid)
        # Emit current position data
        emit('position_update', {
            'in_position': bot_engine.in_position,
            'position_entry_price': bot_engine.position_entry_price,
            'position_qty': bot_engine.position_qty,
            'current_take_profit': bot_engine.current_take_profit,
            'current_stop_loss': bot_engine.current_stop_loss
        }, room=sid)
 
        for log in list(bot_engine.console_logs):
            emit('console_log', log, room=sid)

@socketio.on('disconnect')
def handle_disconnect():
    logging.info('Client disconnected')

@socketio.on('start_bot')
def handle_start_bot(data=None):
    global bot_engine

    try:
        if bot_engine and bot_engine.is_running:
            emit('error', {'message': 'Bot is already running'})
            return

        if not bot_engine:
             bot_engine = TradingBotEngine(config_file, emit_to_client)

        # 1. Check Credentials before starting
        valid, msg = bot_engine.check_credentials()
        if not valid:
            emit('error', {'message': f'API Credentials Error: {msg}'})
            return

        try:
            bot_engine.start()
            if bot_engine.is_running:
                socketio.emit('bot_status', {'running': True}) # Broadcast status to all
                emit('success', {'message': 'Bot started successfully'})
            else:
                # If bot_engine.start() returned False internally (e.g. position mode error),
                # it already emitted its own error log and 'bot_status': False.
                # However, we'll re-sync just in case.
                socketio.emit('bot_status', {'running': False})
        except Exception as e:
            logging.error(f'Error during bot_engine instantiation or start: {str(e)}', exc_info=True)
            emit('error', {'message': f'Failed to start bot: {str(e)}'})
    except Exception as e: # Catch errors from load_config()
        logging.error(f'Error loading configuration in handle_start_bot: {str(e)}', exc_info=True)
        emit('error', {'message': f'Failed to start bot due to config error: {str(e)}'})

@socketio.on('stop_bot')
def handle_stop_bot(data=None):
    global bot_engine

    try:
        if not bot_engine or not bot_engine.is_running:
            emit('error', {'message': 'Bot is not running'})
            return

        bot_engine.stop()
        socketio.emit('bot_status', {'running': False}) # Broadcast status to all
        emit('success', {'message': 'Bot stopped successfully'})

    except Exception as e:
        logging.error(f'Error stopping bot: {str(e)}')
        emit('error', {'message': f'Failed to stop bot: {str(e)}'})

@socketio.on('clear_console')
def handle_clear_console(data=None):
    if bot_engine:
        bot_engine.console_logs.clear()
    emit('console_cleared', {})

@socketio.on('batch_modify_tpsl')
def handle_batch_modify_tpsl(data=None):
    global bot_engine
    if not bot_engine:
         bot_engine = TradingBotEngine(config_file, emit_to_client)
         bot_engine.start(passive_monitoring=True)
    
    bot_engine.batch_modify_tpsl()

@socketio.on('batch_cancel_orders')
def handle_batch_cancel_orders(data=None):
    global bot_engine
    if not bot_engine:
         bot_engine = TradingBotEngine(config_file, emit_to_client)
         bot_engine.start(passive_monitoring=True)
    
    bot_engine.batch_cancel_orders()

@socketio.on('emergency_sl')
def handle_emergency_sl(data=None):
    global bot_engine
    if not bot_engine:
         bot_engine = TradingBotEngine(config_file, emit_to_client)
         bot_engine.start(passive_monitoring=True)
    
    bot_engine.emergency_sl()

@socketio.on('close_trade')
def handle_close_trade(data):
    global bot_engine
    if bot_engine:
        contract_id = data.get('contract_id')
        if contract_id:
            bot_engine.log(f"Manual close requested for trade {contract_id}")
            bot_engine._close_contract(contract_id)


if __name__ == '__main__':
    if not bot_engine:
        bot_engine = TradingBotEngine(config_file, emit_to_client)
        bot_engine.start(passive_monitoring=True)

    port = int(os.environ.get('PORT', 3000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False, log_output=True, allow_unsafe_werkzeug=True)
