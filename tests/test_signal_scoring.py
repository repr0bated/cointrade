import json
import sqlite3
import unittest
from cointrade import attribution,evm
from cointrade.config import Config
from cointrade.launchrisk import candidate_score,decide,FLAGS
from cointrade.onchain import schema
from cointrade.quotes import word


class SignalScoreTests(unittest.TestCase):
    def test_missing_evidence_does_not_get_fifty_free_points(self):
        score=candidate_score({},1000,Config())
        self.assertEqual(score['value'],0)
        self.assertTrue(all(p['points']==0 for p in score['components']))

    def test_liquidity_and_execution_differentiate_candidates(self):
        c=Config();low=dict(liquidity=100,top10_share=1,distinct_buyers=0,qualified_wallet_buys=0)
        high=dict(low,liquidity=25000,observed_at=1000,execution_quote={'status':'quoted'},
          trade_simulation=dict(status='passed',buy_transfer_shortfall=0,sell_transfer_shortfall=0,roundtrip_loss_fraction=.02))
        self.assertGreater(candidate_score(high,1000,c)['value'],candidate_score(low,1000,c)['value'])
        self.assertLess(candidate_score(high,1201,c)['value'],candidate_score(high,1000,c)['value'])

    def test_full_score_is_bounded_and_cannot_override_risk_failure(self):
        c=Config();s=dict(token='t',price=1,observed_at=1000,source_verification={'status':'verified'},
          risk_checks={k:'pass' for k in FLAGS},liquidity=100000,top10_share=.1,
          distinct_buyers=100,qualified_wallet_buys=30,risk_checked_at=1000,
          risk_reasons=['unreviewed_v4_hook'],launch_age=10,deployment_confirmed=True,
          market_evidence={'lp_burned_fraction':1},
          execution_quote={'status':'quoted'},trade_simulation=dict(status='passed',
            buy_transfer_shortfall=0,sell_transfer_shortfall=0,roundtrip_loss_fraction=.01))
        self.assertEqual(candidate_score(s,1000,c)['value'],100)
        self.assertEqual(decide(s,1000,c)[0],'SKIP')
        s['risk_checks']={};s['source_verification']={'status':'unavailable'}
        self.assertEqual(candidate_score(s,1000,c)['value'],70)


class RecentBuyerTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close);schema(self.db)
        self.wallet='0x'+'1'*40;self.pool='0x'+'2'*40;self.token='0x'+'3'*40
        self.db.execute('INSERT INTO chain_events VALUES(?,?,?,?,?,?,?)',('tx',0,100,'hash','swap',self.pool,'{}'))
        self.db.execute('INSERT INTO chain_swaps VALUES(?,?,?,?,?,?,?,?,?,?)',
          ('tx',0,self.pool,self.token,None,1000,'buy','100','10',0))
        self.db.commit()
        self.receipt=dict(blockHash='hash',status='0x1',to=self.pool,logs=[dict(address=self.token,
          topics=[evm.TRANSFER,'0x'+word(self.pool),'0x'+word(self.wallet)],data='0x'+word(100))])
        self.receipt['from']=self.wallet
        self.trace=dict(type='CALL',to=self.pool,value='0xa');self.trace['from']=self.wallet

    def test_live_buyers_do_not_wait_for_or_reorder_fifo_history(self):
        outer=self
        class RPC:
            calls=0
            def call(self,method,args):
                self.calls+=1
                return outer.receipt if method=='eth_getTransactionReceipt' else outer.trace
        rpc=RPC()
        buyers,evidence=attribution.recent_buyers(self.db,rpc,self.token,100,1000)
        self.assertEqual(buyers,[self.wallet]);self.assertEqual(evidence['pending'],0)
        self.assertEqual(rpc.calls,2)
        for table in ('wallet_flows','wallet_lots','wallet_reviews'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        attribution.recent_buyers(self.db,rpc,self.token,100,1000)
        self.assertEqual(rpc.calls,2)

    def test_provider_failure_remains_pending_and_future_blocks_are_excluded(self):
        class RPC:
            def call(self,*args):raise ValueError('Provider unavailable')
        buyers,evidence=attribution.recent_buyers(self.db,RPC(),self.token,100,1000)
        self.assertEqual(buyers,[]);self.assertEqual(evidence['pending'],1)
        self.assertTrue(evidence['errors'])
        buyers,evidence=attribution.recent_buyers(self.db,RPC(),self.token,99,1000)
        self.assertEqual(evidence['transactions'],0)
