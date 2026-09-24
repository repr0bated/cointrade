import sqlite3
import unittest
from unittest.mock import patch
from cointrade import experiment, evm

class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE chain_tokens(token TEXT,first_block INT,first_ts REAL,deployment_confirmed INT,decimals INT,owner TEXT);
        CREATE TABLE launch_decisions(id INT,token TEXT,action TEXT,reasons TEXT,evidence TEXT);
        CREATE TABLE chain_pools(pool TEXT,version INT,token0 TEXT,token1 TEXT,block INT,ts REAL);''')
        self.db.executemany('INSERT INTO chain_tokens VALUES(?,?,0,1,18,NULL)',[(str(i),i) for i in range(12)])
        self.db.commit()
        experiment.start(self.db)

    def test_budget_persists_no_duplicate_claims(self):
        items = [experiment.claim(self.db) for _ in range(10)]
        self.assertEqual(len({i[0] for i in items}),10)
        self.assertIsNone(experiment.claim(self.db))
        experiment.start(self.db)
        self.assertIsNone(experiment.claim(self.db))
        self.assertEqual(experiment.state(self.db)['reserved_usd'],3)

    def test_explicit_high_and_accounting(self):
        item = experiment.claim(self.db)
        result = {'model':experiment.MODEL,'usage':{'cost':.08},'choices':[{'message':{'content':'{"action":"SKIP","rationale":"unknown"}'}}]}
        with patch('cointrade.experiment.request_json',return_value=result) as req:
            experiment.review(self.db,item,'fake')
        payload = req.call_args.args[1]
        self.assertEqual(payload['model'],experiment.MODEL)
        self.assertEqual(payload['reasoning']['effort'],'high')
        self.assertNotIn('plugins',payload)
        self.assertEqual(experiment.state(self.db)['completed'],1)
        self.assertEqual(experiment.state(self.db)['reserved_usd'],.3)

    def test_uncertain_failure_stops_and_keeps_allocation(self):
        with patch('cointrade.experiment.request_json',side_effect=ValueError('Provider connection failed')):
            experiment.review(self.db,experiment.claim(self.db),'fake')
        self.assertIsNone(experiment.claim(self.db))
        self.assertEqual(experiment.state(self.db)['reserved_usd'],.3)

class LogSplitTests(unittest.TestCase):
    def test_split_complete_ordered_range(self):
        rpc = evm.RPC()
        def call(method,params):
            q = params[0]
            lo,hi = int(q['fromBlock'],16),int(q['toBlock'],16)
            if hi-lo > 1:
                raise ValueError('Response too large')
            return list(range(lo,hi+1))
        with patch.object(rpc,'call',side_effect=call):
            self.assertEqual(rpc.logs(1,8,['topic']),list(range(1,9)))
    def test_unsplittable_failure(self):
        rpc = evm.RPC()
        with patch.object(rpc,'call',side_effect=ValueError('Response too large')):
            with self.assertRaisesRegex(ValueError,'cursor preserved'):
                rpc.logs(1,1,['topic'])
