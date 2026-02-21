import unittest
from unittest.mock import MagicMock, patch
import json
import time
from bot_engine import TradingBotEngine

class TestPositionHandling(unittest.TestCase):
    def setUp(self):
        self.config = {
            "deriv_api_token": "test_token",
            "deriv_app_id": "12345",
            "symbols": ["R_100"],
            "use_fixed_balance": True,
            "balance_value": 10,
            "max_daily_loss_pct": 5,
            "active_strategy": "strategy_1"
        }
        with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(self.config))):
            self.bot = TradingBotEngine("config.json", MagicMock())
        self.bot.ws = MagicMock()
        self.bot.ws.sock.connected = True

    def test_one_trade_per_symbol_cancel_opposite(self):
        # Simulate an existing LONG trade
        self.bot.contracts['c1'] = {
            'id': 'c1', 'symbol': 'R_100', 'side': 'long',
            'entry_price': 100, 'pnl': 0, 'stake': 10,
            'is_closing': False
        }

        # Try to open a SHORT trade on same symbol
        # _execute_trade(symbol, side) where side is 'buy' or 'sell'
        self.bot._execute_trade('R_100', 'sell')

        # Verify that 'c1' close request was sent
        # The bot sends {"sell": "c1", "price": 0}
        calls = [json.loads(c.args[0]) for c in self.bot.ws.send.call_args_list]
        sell_calls = [c for c in calls if 'sell' in c]
        buy_calls = [c for c in calls if 'buy' in c]

        self.assertTrue(any(c['sell'] == 'c1' for c in sell_calls))
        self.assertTrue(any(c['parameters']['contract_type'] == 'PUT' for c in buy_calls))

    def test_do_not_open_same_direction(self):
        # Simulate an existing LONG trade
        self.bot.contracts['c1'] = {
            'id': 'c1', 'symbol': 'R_100', 'side': 'long',
            'entry_price': 100, 'pnl': 0, 'stake': 10,
            'is_closing': False
        }

        # Try to open another LONG trade
        self.bot._execute_trade('R_100', 'buy')

        # Verify no new BUY request was sent
        calls = [json.loads(c.args[0]) for c in self.bot.ws.send.call_args_list]
        buy_calls = [c for c in calls if 'buy' in c and isinstance(c['buy'], dict) is False] # buy: 1 is the request

        # In my code, buy request starts with {"buy": 1, ...}
        real_buy_requests = [c for c in calls if c.get('buy') == 1]
        self.assertEqual(len(real_buy_requests), 0)

    def test_tp_sl_closing(self):
        self.bot.config['tp_enabled'] = True
        self.bot.config['tp_value'] = 2.0
        self.bot.config['use_fixed_balance'] = True

        # Mock contract reaching TP
        contract = {
            'contract_id': 'c2',
            'underlying': 'R_100',
            'is_sold': False,
            'contract_type': 'CALL',
            'buy_price': 10,
            'profit': 2.5 # > 2.0
        }

        self.bot._handle_contract_update(contract)

        # Verify sell request sent
        calls = [json.loads(c.args[0]) for c in self.bot.ws.send.call_args_list]
        self.assertTrue(any(c.get('sell') == 'c2' for c in calls))

    def test_breakout_strategy_logic(self):
        symbol = 'R_100'
        self.bot._init_symbol_data(symbol)
        sd = self.bot.symbol_data[symbol]

        # Set HTF (Daily) Open to 100
        sd['htf_open'] = 100.0

        # Scenario 1: 1HR candle opens below and closes above (Breakout Buy)
        sd['current_ltf_candle'] = {'epoch': 1000, 'open': 99.0, 'close': 101.0}
        sd['last_tick'] = 101.0

        self.bot._process_strategy(symbol, is_candle_close=True)

        calls = [json.loads(c.args[0]) for c in self.bot.ws.send.call_args_list]
        buy_calls = [c for c in calls if c.get('buy') == 1]
        self.assertEqual(len(buy_calls), 1)
        self.assertEqual(buy_calls[0]['parameters']['contract_type'], 'CALL')

        self.bot.ws.send.reset_mock()

        # Scenario 2: 1HR candle opens above and closes above (No Breakout Buy)
        sd['current_ltf_candle'] = {'epoch': 2000, 'open': 100.5, 'close': 101.0}
        sd['last_tick'] = 101.0

        self.bot._process_strategy(symbol, is_candle_close=True)

        calls = [json.loads(c.args[0]) for c in self.bot.ws.send.call_args_list]
        buy_calls = [c for c in calls if c.get('buy') == 1]
        self.assertEqual(len(buy_calls), 0)

if __name__ == '__main__':
    unittest.main()
