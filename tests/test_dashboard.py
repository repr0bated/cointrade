import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from cointrade.config import Config
from cointrade.dashboard import Dashboard, Monitor, handler_for
from cointrade.onchain import schema
from cointrade.store import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'chain.sqlite')
        self.config = Config()
        self.store = Store(self.path, self.config, 'robinhood')
        self.addCleanup(self.store.db.close)
        schema(self.store.db)
        self.monitor = Monitor()
        self.dashboard = Dashboard(self.path, self.config, self.monitor)

    def test_empty_account_has_honest_zero_counts_and_no_ai_calls(self):
        state = self.dashboard.state()
        self.assertIsNone(state['chain']['cursor'])
        self.assertEqual(state['counts']['events'], 0)
        self.assertEqual(state['scanner']['status'], 'idle')
        self.assertEqual(state['ai']['calls'], 0)
        self.assertEqual(state['paper']['equity'], 40)
        self.assertEqual(state['activity'], [])
        self.assertEqual(state['histogram'], [])
        self.assertEqual(state['ai']['routes'][-1]['model'], 'openai/gpt-6-astra')

    def test_activity_joins_and_real_histogram(self):
        with self.store.db as db:
            db.execute('INSERT INTO chain_cursor VALUES(1,4663,1,1200,?,2)', ('hash',))
            db.execute('INSERT INTO chain_events VALUES(?,?,?,?,?,?,?)',
                       ('tx',2,1199,'hash','swap','pool',json.dumps({'data':'0x00'})))
            db.execute('INSERT INTO chain_swaps VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('tx',2,'pool','token','wallet',1000,'buy','100','1000',0))
        s = self.dashboard.state()
        self.assertEqual(s['counts']['swaps'], 1)
        self.assertEqual(s['counts']['wallets'], 1)
        self.assertEqual(s['counts']['qualified_wallets'], 0)
        self.assertEqual(s['activity'][0]['id'], 'tx:2')
        self.assertEqual(s['activity'][0]['side'], 'buy')
        self.assertFalse(s['activity'][0]['attributed'])
        self.assertEqual(s['activity'][0]['details']['data'], '0x00')
        self.assertEqual(sum(b['count'] for b in s['histogram']), 1)
        self.assertFalse(s['wallets'][0]['mirror_eligible'])

    def test_monitor_does_not_confuse_web_connection_with_scanner(self):
        self.monitor.update(status='error', error='Provider unavailable')
        s = self.dashboard.state()
        self.assertEqual(s['scanner']['status'], 'error')
        self.assertIsNone(s['scanner']['last_success'])

    def test_legacy_scores_are_explained_without_rewriting_history(self):
        with self.store.db as db:
            db.execute('INSERT INTO launch_decisions VALUES(1,1000,\'t\',\'p\',\'SKIP\',50,\'[]\',?)',
                       (json.dumps({'liquidity':100,'top10_share':1}),))
        decision=self.dashboard.state()['decisions'][0]
        self.assertNotEqual(decision['score'],50)
        self.assertEqual(decision['recorded_score'],50)
        self.assertEqual(decision['score_details']['version'],3)
        self.assertEqual(self.store.db.execute('SELECT score FROM launch_decisions').fetchone()[0],50)

    def test_api_is_read_only_and_paths_are_allowlisted(self):
        server = ThreadingHTTPServer(('127.0.0.1',0), handler_for(self.dashboard))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            root = f'http://127.0.0.1:{server.server_port}'
            with urlopen(root+'/api/state') as result:
                self.assertEqual(json.load(result)['paper']['cash'],40)
                self.assertEqual(result.headers['Cache-Control'],'no-store')
                self.assertIn("script-src 'self'",result.headers['Content-Security-Policy'])
            for path in ('/../README.md','/data/chain.sqlite','/.env'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(root+path)
                self.assertEqual(error.exception.code,404)
                error.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(root+'/api/state',data=b'{}')
            self.assertEqual(error.exception.code,403)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
