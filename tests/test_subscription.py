import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from cointrade import subscription


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE launch_decisions(id,token,action,reasons,evidence,ts);
          CREATE TABLE astra_experiment(id,status);
          INSERT INTO astra_experiment VALUES(1,'running');''')
        self.db.executemany('INSERT INTO launch_decisions VALUES(?,?,\'SKIP\',\'[]\',\'{}\',?)',
          [(i,'token'+str(i),time.time()) for i in range(12)])
        self.db.commit();subscription.start(self.db)

    def test_ten_review_limit_and_api_cancellation_persist(self):
        self.assertEqual(self.db.execute('SELECT status FROM astra_experiment').fetchone()[0],'cancelled')
        claimed=[subscription.claim(self.db)[0] for _ in range(10)]
        self.assertEqual(len(set(claimed)),10)
        self.assertIsNone(subscription.claim(self.db))
        subscription.start(self.db)
        self.assertEqual(subscription.state(self.db)['status'],'complete')
        self.assertIsNone(subscription.claim(self.db))

    def test_subscription_environment_cannot_inherit_api_keys(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','OPENROUTER_API_KEY':'secret',
                                    'CODEX_API_KEY':'secret','OPENAI_BASE_URL':'https://unwanted.example'}):
            self.assertFalse(any('API_KEY' in k or k=='OPENAI_BASE_URL' for k in subscription.environment()))
        cmd=subscription.command(Path('/tmp/review'))
        self.assertIn('forced_login_method="chatgpt"',cmd)
        self.assertIn('model_reasoning_effort="high"',cmd)
        self.assertEqual(cmd[cmd.index('--model')+1],'gpt-6-astra')
        self.assertIn('--ignore-user-config',cmd)

    def test_failure_stops_without_retry(self):
        item=subscription.claim(self.db)
        with patch.object(subscription,'run_review',side_effect=ValueError('Quota unavailable')):
            subscription.review(self.db,item)
        self.assertEqual(subscription.state(self.db)['status'],'error')
        self.assertIsNone(subscription.claim(self.db))

    def test_structured_review_is_persisted_as_advice(self):
        answer=dict(action='SKIP',rationale='Missing evidence',missing_evidence=['permissions'],
                    known_failures=[],data_quality_findings=[])
        with patch.object(subscription,'run_review',return_value=(answer,{'output_tokens':25})):
            subscription.review(self.db,subscription.claim(self.db))
        state=subscription.state(self.db)
        self.assertEqual(state['completed'],1)
        self.assertEqual(json.loads(state['recent'][0]['response']),answer)
        with self.assertRaises(ValueError):subscription.validate(dict(answer,action='BUY_REAL'))

    def test_stale_assessments_are_not_sent(self):
        self.db.execute('UPDATE launch_decisions SET ts=?',(time.time()-200,));self.db.commit()
        self.assertIsNone(subscription.claim(self.db))
