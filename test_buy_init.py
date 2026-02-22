import unittest
from unittest.mock import MagicMock
import json
from bot_engine import TradingBotEngine

class TestBuyMessageInit(unittest.TestCase):
    def setUp(self):
        self.emit_mock = MagicMock()
        self.bot = TradingBotEngine('config.json', self.emit_mock)
        self.bot.config['tp_enabled'] = True
        self.bot.config['tp_value'] = 10
        self.bot.config['use_fixed_balance'] = True

    def test_buy_message_initializes_contract(self):
        # Setup mock symbol data with last_tick
        symbol = 'R_100'
        self.bot.symbol_data[symbol] = {'last_tick': 1000}

        # Simulated buy response message
        data = {
            'msg_type': 'buy',
            'buy': {'contract_id': 'cid123', 'buy_price': 50},
            'echo_req': {
                'parameters': {
                    'symbol': symbol,
                    'contract_type': 'MULTUP',
                    'multiplier': 100
                }
            }
        }

        self.bot.on_message(None, json.dumps(data))

        # Verify contract was created in self.bot.contracts
        self.assertIn('cid123', self.bot.contracts)
        c = self.bot.contracts['cid123']
        self.assertEqual(c['symbol'], symbol)
        self.assertEqual(c['entry_price'], 1000)
        # tp_price = 1000 * (1 + 10 / (100 * 50)) = 1000 * (1 + 0.002) = 1002
        self.assertAlmostEqual(c['tp_price'], 1002)

if __name__ == '__main__':
    unittest.main()
