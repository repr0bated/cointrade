from dataclasses import replace
import json
import tempfile
import unittest
from unittest.mock import patch

from cointrade.ai import analyze, reserve
from cointrade.config import Config
from cointrade.engine import Engine
from cointrade.providers import fetch_snapshot, fetch_coinbase, safety, TOKEN_PROGRAM
from cointrade.store import Store
from cointrade.strategy import screen, screen_coinbase


def snapshot(ts=1000, **updates):
    return dict(token='TEST', symbol='TEST', chain='solana', observed_at=ts,
                price=1, liquidity=100000, volume_h24=50000, age_seconds=7200,
                standard_token=True, mint_authority=None, freeze_authority=None,
                top10_share=0.2, buys_m5=40, sells_m5=10, change_m5=5, **updates)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.c = Config()
        self.store = Store(':memory:', self.c, 'replay')
        self.addCleanup(self.store.db.close)
        self.engine = Engine(self.store, self.c)

    def tick(self, ts, **updates):
        s = snapshot(ts)
        s.update(updates)
        return self.engine.tick([s], ts)

    def test_round_trip_accounts_for_both_fees_and_slippage(self):
        report = self.tick(1000)
        self.assertEqual(report['cash'], 38)
        quantity = (2 - .005) / 1.003 / 1.01
        self.assertAlmostEqual(report['positions'][0]['quantity'], quantity)
        report = self.tick(1060, price=1.5)
        proceeds = quantity * 1.5 * .99 * .997 - .005
        self.assertAlmostEqual(report['cash'], 38 + proceeds)
        self.assertAlmostEqual(report['realized_pnl'], proceeds - 2)
        self.assertEqual(report['positions'], [])
        self.assertEqual(report['fills'], 2)

    def test_stale_quote_cannot_buy_or_sell(self):
        self.tick(1000, observed_at=1)
        self.assertEqual(self.store.positions(), [])
        self.tick(1060)
        result = self.tick(1120, observed_at=1000, price=.1)
        self.assertEqual(result['fills'], 1)
        self.assertEqual(result['positions'][0]['mark'], 1)

    def test_same_position_does_not_rebuy(self):
        self.tick(1000)
        self.assertEqual(self.tick(1060)['fills'], 1)
        with self.assertRaises(ValueError):
            self.tick(1060)
        self.assertEqual(self.store.report(1060, self.c)['fills'], 1)

    def test_missing_safety_fields_fail_closed(self):
        for field in ('mint_authority', 'freeze_authority', 'top10_share', 'liquidity', 'standard_token'):
            s = snapshot()
            del s[field]
            self.assertTrue(screen(s, 1000, self.c)[1], field)

    def test_nan_and_infinite_values_rejected(self):
        for field in ('price', 'top10_share', 'buys_m5', 'change_m5'):
            s = snapshot()
            s[field] = float('nan')
            self.assertTrue(screen(s, 1000, self.c)[1])
        with self.assertRaises(ValueError):
            Config(position_size=float('inf'))

    def test_position_and_exposure_caps(self):
        snapshots = []
        for i in range(20):
            s = snapshot()
            s['token'] = str(i)
            snapshots.append(s)
        result = self.engine.tick(snapshots, 1000)
        self.assertEqual(len(result['positions']), 5)
        self.assertEqual(result['cash'], 30)

    def test_halt_persists_and_exits_only_on_fresh_quote(self):
        self.tick(1000)
        with self.store.db:
            self.store.db.execute('UPDATE account SET cash=18 WHERE id=1')
        report = self.engine.tick([], 1060)
        self.assertTrue(report['halted'])
        self.assertEqual(len(report['positions']), 1)
        report = self.tick(1120)
        self.assertTrue(report['halted'])
        self.assertEqual(report['positions'], [])
        self.assertEqual(self.tick(5000)['fills'], 2)

    def test_missing_portfolio_quote_blocks_new_entry(self):
        self.tick(1000)
        s = snapshot(1060)
        s['token'] = 'OTHER'
        result = self.engine.tick([s], 1060)
        self.assertEqual(result['fills'], 1)

    def test_bad_batch_rolls_back(self):
        with self.assertRaises(ValueError):
            self.engine.tick([snapshot(), snapshot()], 1000)
        self.assertIsNone(self.store.account()['last_tick'])
        self.assertEqual(self.store.account()['cash'], 40)

    def test_stop_loss_and_cooldown(self):
        self.tick(1000)
        self.assertEqual(self.tick(1060, price=.7)['fills'], 2)
        self.assertEqual(self.tick(1120)['fills'], 2)
        self.assertEqual(self.tick(5000)['fills'], 3)

    def test_restart_and_config_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + '/paper.sqlite'
            s = Store(path, self.c, 'replay')
            Engine(s, self.c).tick([snapshot()], 1000)
            s.db.close()
            s = Store(path, self.c, 'replay')
            self.assertEqual(s.account()['cash'], 38)
            self.assertEqual(len(s.positions()), 1)
            s.db.close()
            for c, source in ((self.c, 'market'), (replace(self.c, position_size=1), 'replay')):
                with self.assertRaises(ValueError):
                    Store(path, c, source)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.c = Config()
        self.store = Store(':memory:', self.c, 'replay')
        self.addCleanup(self.store.db.close)

    def test_daily_and_monthly_limits(self):
        for day in range(1, 31):
            stamp = f'2026-10-{day:02d}T00:00:00+00:00'
            for _ in range(5):
                reserve(self.store.db, stamp)
            with self.assertRaises(ValueError):
                reserve(self.store.db, stamp)
        with self.assertRaises(ValueError):
            reserve(self.store.db, '2026-10-31T00:00:00+00:00')
        reserve(self.store.db, '2026-11-01T00:00:00+00:00')

    @patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-only'})
    @patch('cointrade.ai.request_json', side_effect=ValueError('timeout'))
    def test_failed_calls_retain_reservation(self, request):
        with self.assertRaises(ValueError):
            analyze(self.store, self.store.report(1000, self.c))
        row = self.store.db.execute('SELECT * FROM ai_calls').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['reserved_cents'], 2)
        payload = request.call_args.args[1]
        self.assertEqual(payload['provider']['max_price']['completion'], 2)
        self.assertEqual(payload['plugins'][0]['cost_tier'], 'low')

    @patch.dict('os.environ', {'OPENROUTER_API_KEY': 'test-only'})
    @patch('cointrade.ai.request_json', return_value={'model': 'example', 'choices': [{'message': {'content': 'Paper only'}}], 'usage': {'cost': .004}})
    def test_response_logged_and_no_trading_authority(self, request):
        result = analyze(self.store, self.store.report(1000, self.c))
        self.assertEqual(result['model'], 'example')
        self.assertEqual(self.store.account()['cash'], 40)
        self.assertEqual(self.store.positions(), [])
        self.assertEqual(result['reserved_cents'], 2)


class ProviderTests(unittest.TestCase):
    @patch('cointrade.providers.request_json')
    def test_exchange_closed_candles_and_real_spread(self, request):
        request.side_effect = [
            {'time': '1970-01-01T00:30:00Z', 'bid': '121', 'ask': '121.01', 'volume': '10000'},
            [[600+i*60, 99+i, 102+i, 100+i, 101+i, 50] for i in range(21)],
        ]
        s = fetch_coinbase('BTC-USD', 1800)
        self.assertEqual(s['candle_end'], 1800)
        self.assertAlmostEqual(s['sma_fast'], 118)
        c = Config()
        self.assertEqual(screen_coinbase(s, 1800, c)[1], [])
        store = Store(':memory:', c, 'coinbase')
        self.addCleanup(store.db.close)
        report = Engine(store, c, screen_coinbase).tick([s], 1800)
        self.assertEqual(report['fills'], 1)
        self.assertAlmostEqual(report['positions'][0]['entry_price'], 121.01 * 1.01)
        self.assertIn('invalid_or_stale_quote', screen_coinbase(s, 2000, c)[1])
        self.assertIn('invalid_or_stale_quote', screen_coinbase(s, 1700, c)[1])

    @patch('cointrade.providers.time.sleep')
    @patch('cointrade.providers.rpc')
    def test_authorities_and_raw_supply_concentration(self, rpc, sleep):
        rpc.side_effect = [
            {'value': {'owner': TOKEN_PROGRAM, 'data': {'parsed': {'type': 'mint', 'info': {'supply': '1000000', 'mintAuthority': None, 'freezeAuthority': None}}}}},
            {'value': [{'amount': '10000'}, {'amount': '20000'}]},
        ]
        result = safety('example')
        self.assertAlmostEqual(result['top10_share'], .03)
        self.assertTrue(result['standard_token'])

    @patch('cointrade.providers.safety', side_effect=ValueError('rate limited'))
    @patch('cointrade.providers.request_json')
    def test_rpc_failure_preserves_quote_but_blocks_entry(self, request, safety_mock):
        token = 'So11111111111111111111111111111111111111112'
        request.return_value = [{'chainId': 'solana', 'baseToken': {'address': token, 'symbol': 'SOL'},
                                 'priceUsd': '100', 'pairAddress': 'pool', 'liquidity': {'usd': 100000},
                                 'pairCreatedAt': 1000, 'volume': {'h24': 100000},
                                 'txns': {'m5': {'buys': 40, 'sells': 10}}, 'priceChange': {'m5': 5}}]
        result = fetch_snapshot(token, 10000)
        self.assertEqual(result['price'], 100)
        self.assertTrue(screen(result, 10000, Config())[1])


if __name__ == '__main__':
    unittest.main()
