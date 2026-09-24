import json
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
import threading

from cointrade import astra_live
from cointrade.config import Config
from cointrade.dashboard import Dashboard, Monitor, handler_for
from cointrade.launchrisk import FLAGS
from cointrade.onchain import Scanner, schema
from cointrade.store import Store


class LiveAstraTests(unittest.TestCase):
    def setUp(self):
        self.c=Config()
        self.store=Store(':memory:',self.c,'robinhood')
        self.db=self.store.db
        self.addCleanup(self.db.close)
        schema(self.db);astra_live.start(self.db)
        self.now=time.time()
        self.token='0x'+'1'*40
        self.snapshot=dict(token=self.token,pool='0x'+'a'*40,symbol='Test',price=1,observed_at=self.now,
          liquidity=100000,top10_share=.2,risk_reasons=[],risk_checked_at=self.now,
          launch_age=18,deployment_confirmed=True,distinct_buyers=3,qualified_wallet_buys=0,
          source_verification={'status':'verified'},risk_checks={k:'pass' for k in FLAGS},
          market_evidence={'lp_burned_fraction':1},requires_execution_quote=True,
          execution_quote={'status':'quoted','quantity':1.995,'usd_in':1.995})
        meta=dict(block=100,block_hash='0x'+'b'*64,observed_at=self.now)
        self.snapshot.update(
          source_verification=dict(status='verified',provider='blockscout',checked_at=self.now,**meta),
          market_evidence=dict(lp_burned_fraction=1,lp_total_supply_atomic='100',source='v2_reserves',**meta),
          holder_evidence=dict(source='verified_transfer_ledger',**meta),
          buyer_evidence=dict(transactions=3,reviewed=3,pending=0,ambiguous=0,**meta),
          execution_quote=dict(status='quoted',quantity=1.995,usd_in=1.995,source='canonical_pool_quote',**meta))
        with self.db:
            self.db.execute('INSERT INTO chain_tokens VALUES(?,?,?,1,18,\'hash\',NULL,\'{}\')',(self.token,100,self.now))
            self.decision()
        self.scanner=Scanner.__new__(Scanner)
        self.scanner.store=self.store;self.scanner.config=self.c;self.scanner.head=102

    def decision(self):
        return self.db.execute('INSERT INTO launch_decisions(ts,token,pool,action,score,reasons,evidence) VALUES(?,?,?,\'SNIPE\',80,\'[]\',?)',
          (self.now,self.token,self.snapshot['pool'],json.dumps(self.snapshot))).lastrowid

    def approve(self,action='SNIPE'):
        astra_live.request(self.db,self.token)
        item=astra_live.claim(self.db)
        answer=dict(action=action,rationale='Measured evidence',missing_evidence=[],known_failures=[],data_quality_findings=[])
        with patch('cointrade.subscription.run_review',return_value=(answer,{'output_tokens':20})):
            astra_live.review(self.db,item)

    def evaluate(self):
        with patch.object(self.scanner,'snapshots',return_value=[self.snapshot]), patch('cointrade.onchain.time.time',return_value=self.now):
            self.scanner.evaluate()

    def test_review_policies_match_launch_rules_and_keep_momentum_limits_separate(self):
        _,packet=astra_live.evidence_for(self.db,self.token)
        self.assertNotIn('min_age_seconds',packet['configured_limits'])
        self.assertNotIn('min_volume_h24',packet['configured_limits'])
        self.assertEqual(packet['configured_limits']['snipe_max_age_seconds'],300)
        self.assertEqual(packet['policy_version'],2)
        from cointrade import momentum
        momentum.start(self.db,self.c,now=self.now)
        _,packet=astra_live.evidence_for(self.db,self.token)
        policies=packet['strategy_policies']
        self.assertEqual(policies['onchain_launch']['limits']['min_score'],65)
        self.assertEqual(policies['wallet_momentum_astra']['min_score'],0)
        self.assertEqual(policies['wallet_momentum_astra']['limits']['max_launch_age'],900)
        self.assertEqual(policies['wallet_momentum_astra']['limits']['min_samples'],10)

    def test_fresh_review_gets_priority_but_every_fourth_turn_serves_backlog(self):
        fresh='0x'+'2'*40
        with self.db:
            self.db.execute('UPDATE launch_decisions SET ts=?',(self.now-200,))
            s=dict(self.snapshot,token=fresh)
            self.db.execute("INSERT INTO launch_decisions(ts,token,pool,action,score,reasons,evidence) VALUES(?,?,?,'SNIPE',80,'[]',?)",(self.now,fresh,s['pool'],json.dumps(s)))
        astra_live.request(self.db,self.token);astra_live.request(self.db,fresh)
        item=astra_live.claim(self.db)
        self.assertEqual(json.loads(item[1])['token'],fresh)
        with self.db:
            self.db.execute("UPDATE astra_live_reviews SET status='queued' WHERE status='running'")
            for i in range(3):self.db.execute("INSERT INTO astra_live_reviews(token,queued_at,status,model) VALUES(?,?,'failed','test')",('finished'+str(i),self.now))
        item=astra_live.claim(self.db)
        self.assertEqual(json.loads(item[1])['token'],self.token)

    def test_new_tokens_without_assessments_are_queued_once(self):
        other='0x'+'2'*40
        with self.db:
            self.db.execute('INSERT INTO chain_tokens VALUES(?,?,?,0,NULL,NULL,NULL,\'{}\')',(other,101,self.now))
        astra_live.enqueue(self.db);astra_live.enqueue(self.db)
        self.assertEqual(astra_live.state(self.db)['counts'],{'queued':2})
        first=astra_live.claim(self.db)
        self.assertEqual(json.loads(first[1])['token'],self.token)
        self.assertIsNone(astra_live.claim(self.db))
        with self.db:self.db.execute('UPDATE astra_live_reviews SET queued_at=? WHERE token=?',(self.now-181,other))
        item=astra_live.claim(self.db)
        self.assertEqual(json.loads(item[1])['action'],'SKIP')
        self.assertIn('No complete',json.loads(item[1])['reasons'][0])

    def test_manual_request_deduplicates_and_new_evidence_allows_recheck(self):
        first=astra_live.request(self.db,self.token)
        self.assertEqual(astra_live.request(self.db,self.token),first)
        self.approve()
        self.assertEqual(astra_live.request(self.db,self.token)['status'],'complete')
        with self.db:self.decision()
        self.assertEqual(astra_live.request(self.db,self.token)['status'],'queued')

    def test_stale_review_requests_refresh_before_bounded_wait(self):
        self.snapshot['observed_at']-=300
        self.snapshot['risk_checked_at']-=300
        for key in ('market_evidence','holder_evidence','buyer_evidence','execution_quote'):
            self.snapshot[key]['observed_at']-=300
        with self.db:self.decision()
        astra_live.request(self.db,self.token)
        self.assertIsNone(astra_live.claim(self.db))
        requests=self.db.execute('SELECT action,status FROM evidence_refresh_requests').fetchall()
        self.assertTrue(requests)
        self.assertTrue(all(r['status']=='queued' for r in requests))
        with self.db:self.db.execute('UPDATE astra_live_reviews SET refresh_requested_at=?',(self.now-91,))
        item=astra_live.claim(self.db)
        self.assertIsNotNone(item)
        packet=json.loads(item[1])
        self.assertIn('quality_at_review',packet)
        self.assertTrue(any(f['severity']=='blocker' for f in packet['quality_at_review']['findings']))

    def test_restart_keeps_pause_and_history(self):
        self.approve()
        astra_live.control(self.db,'pause');astra_live.start(self.db)
        self.assertEqual(astra_live.state(self.db)['status'],'paused')
        self.assertIsNone(astra_live.claim(self.db))
        self.assertEqual(astra_live.state(self.db)['counts']['complete'],1)
        self.assertEqual(astra_live.entry_gate(self.db,self.snapshot,self.now,'SNIPE'),['astra_paused'])

    def test_failure_stops_requests_without_retry_or_fallback(self):
        astra_live.request(self.db,self.token)
        with patch('cointrade.subscription.run_review',side_effect=ValueError('Subscription unavailable')) as call:
            astra_live.review(self.db,astra_live.claim(self.db))
        self.assertEqual(call.call_count,1)
        self.assertEqual(astra_live.state(self.db)['status'],'error')
        self.assertIsNone(astra_live.claim(self.db))
        self.assertIn('astra_error',astra_live.entry_gate(self.db,self.snapshot,self.now,'SNIPE'))

    def test_approval_is_bound_to_pool_signal_and_fresh_evidence(self):
        self.approve()
        self.assertEqual(astra_live.entry_gate(self.db,self.snapshot,self.now,'SNIPE'),[])
        self.assertEqual(astra_live.entry_gate(self.db,dict(self.snapshot,pool='other'),self.now,'SNIPE'),['astra_review_pool_mismatch'])
        self.assertEqual(astra_live.entry_gate(self.db,self.snapshot,self.now,'MIRROR'),['astra_signal_no_longer_matches'])
        self.assertEqual(astra_live.entry_gate(self.db,self.snapshot,self.now+301,'SNIPE'),['astra_review_stale'])

    def test_paper_entry_requires_astra_then_uses_quoted_amount_and_fees(self):
        self.evaluate()
        self.assertEqual(self.store.positions(),[])
        self.approve()
        self.now+=1;self.evaluate()
        p=self.store.positions()[0]
        self.assertAlmostEqual(p['quantity'],1.995*.99)
        self.assertAlmostEqual(self.store.account()['cash'],38)
        fill=self.db.execute('SELECT * FROM trades').fetchone()
        self.assertEqual(fill['side'],'buy');self.assertAlmostEqual(fill['fee'],.005)

    def test_astra_cannot_override_risk_failure_or_its_own_skip(self):
        self.approve()
        self.snapshot['risk_reasons']=['is_honeypot_flagged']
        self.evaluate();self.assertEqual(self.store.positions(),[])
        self.snapshot['risk_reasons']=[]
        with self.db:self.db.execute("UPDATE astra_live_reviews SET action='SKIP'")
        self.now+=1;self.evaluate();self.assertEqual(self.store.positions(),[])

    def test_pause_keeps_quoted_exit_rules_active(self):
        self.approve();self.evaluate()
        astra_live.control(self.db,'pause')
        self.now+=10
        self.snapshot.update(price=1.4,observed_at=self.now,position_quote={'status':'quoted','usd_out':2.8})
        self.evaluate()
        self.assertEqual(self.store.positions(),[])
        fill=self.db.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(fill['side'],'sell');self.assertEqual(fill['reason'],'take_profit')
        self.assertAlmostEqual(fill['cash_flow'],2.8*.99-.005)


class TerminalControlTests(unittest.TestCase):
    def test_cross_origin_simple_requests_and_unknown_routes_are_rejected(self):
        class Stub:
            pass
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(Stub()))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}'
            for path,headers,status in [('/api/astra',{},403),('/api/rpc',{'Content-Type':'application/json','X-Cointrade-Request':'terminal'},404)]:
                with self.assertRaises(HTTPError) as exc:
                    urlopen(Request(url+path,data=b'{}',headers=headers))
                self.assertEqual(exc.exception.code,status);exc.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)
