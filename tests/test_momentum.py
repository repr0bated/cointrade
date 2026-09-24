import json
import sqlite3
import unittest
from unittest.mock import patch, Mock

from cointrade import momentum as m
from cointrade import evm
from cointrade.config import Config
from cointrade.store import Store
from cointrade.onchain import schema


def snapshot(block=10, stamp=1000):
    return dict(token='token',pool='pool',observed_at=stamp,price=.001,
        market_evidence=dict(block=block,observed_at=stamp,block_hash=f'h{block}'),
        execution_quote=dict(status='quoted',block=block,usd_in=1.995,
            quote_in_atomic='1000000000000000',quantity=1995),
        trade_simulation=dict(status='passed',block=block,token_received_atomic='1995000',
            buy_transfer_shortfall=0,sell_transfer_shortfall=0,roundtrip_loss_fraction=.006))


class MomentumTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:',Config(),'robinhood')
        self.db = self.store.db
        schema(self.db)
        m.start(self.db,Config(),now=1000)

    def tearDown(self):
        self.db.close()

    def decision(self,stamp,s=None):
        s = s or snapshot(stamp=stamp)
        with self.db:
            return self.db.execute('INSERT INTO launch_decisions(ts,token,pool,action,score,reasons,evidence) VALUES(?,?,?,\'SKIP\',0,\'[]\',?)',
              (stamp,'token','pool',json.dumps(s))).lastrowid

    def sample(self, tx='tx',stamp=950):
        with self.db:
            self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status) VALUES(?, 'wallet','token',?,999,'queued')",(tx,stamp))

    def test_separate_accounts_and_frozen_rules_survive_restart(self):
        with self.db:self.db.execute("UPDATE momentum_accounts SET cash=38 WHERE arm='simple_momentum'")
        m.start(self.db,Config(),now=2000)
        self.assertEqual(self.store.account()['cash'],40)
        self.assertEqual(dict(self.db.execute('SELECT arm,cash FROM momentum_accounts')),dict(wallet_momentum=40,wallet_momentum_astra=40,simple_momentum=38))
        with self.assertRaisesRegex(ValueError,'frozen'):
            m.start(self.db,Config(min_liquidity=5000))

    def test_seed_first_buy_per_wallet_token_without_winner_selection(self):
        with self.db:
            for tx,w,t,ts in [('1','w1','t1',1),('2','w1','t1',2),('3','w2','t1',3),('4','w1','t2',4)]:
                self.db.execute("INSERT INTO wallet_flows VALUES(?,?,?,?, 'buy','1','1','0',1,'receipt_and_call_trace')",(tx,w,t,ts))
        m.seed_history(self.db,2000);m.seed_history(self.db,2001)
        self.assertEqual([r[0] for r in self.db.execute('SELECT tx FROM momentum_samples ORDER BY tx')],['1','3','4'])




    def test_catchup_marker_requires_current_index_and_preserves_other_risks(self):
        with self.db:self.db.execute("INSERT INTO chain_cursor VALUES(1,4663,1,10,'h10',2)")
        s=dict(snapshot(),risk_reasons=['scanner_catching_up','liquidity_below_limit'])
        self.assertEqual(m.refresh_scanner_status(self.db,s,{'indexed_through':999},1000)['risk_reasons'],['liquidity_below_limit'])
        self.assertIn('scanner_catching_up',s['risk_reasons'])
        self.assertIn('scanner_catching_up',m.refresh_scanner_status(self.db,s,{'indexed_through':950},1000)['risk_reasons'])
        self.assertIn('scanner_catching_up',m.refresh_scanner_status(self.db,s,{'indexed_through':1100},1000)['risk_reasons'])
        with self.db:self.db.execute('UPDATE chain_cursor SET height=9')
        self.assertIn('scanner_catching_up',m.refresh_scanner_status(self.db,s,{'indexed_through':999},1000)['risk_reasons'])

    def test_rankings_cannot_see_future_or_just_backfilled_outcomes(self):
        with self.db:
            for i in range(10):
                self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status,ended_at,completed_at,return_fraction,replay_version) VALUES(?, 'w',?,100,100,'complete',200,500,.1,2)",(str(i),str(i)))
        self.assertFalse(m.rankings(self.db,400)[0]['qualified'])
        self.assertTrue(m.rankings(self.db,501)[0]['qualified'])
        with self.db:
            for i in range(10,20):
                self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status) VALUES(?, 'w',?,100,100,'no_entry_evidence')",(str(i),str(i)))
        self.assertFalse(m.rankings(self.db,501)[0]['qualified'])

    def test_net_sell_and_shared_origin_do_not_qualify(self):
        with self.db:
            self.db.execute("INSERT INTO chain_cursor VALUES(1,4663,1,10,'h10',2)")
            self.db.execute("INSERT INTO chain_pools VALUES('pool',2,?,'token',3000,?,1,900,'creator','tx')",(evm.WETH,evm.ZERO))
            self.db.execute("INSERT INTO data_blocks VALUES('h10',4663,10,NULL,995,'rpc',995)")
            self.db.execute("INSERT INTO chain_creators VALUES('token','creator','creator','tx','verified')")
            for tx,w,side,quote in [('a','w1','buy','1'),('b','w2','buy','1'),('c','w3','sell','3')]:
                self.db.execute('INSERT INTO momentum_flows VALUES(?,?,?,?,?,?,?,?)',(tx,w,'token',990,995,side,quote,'shared'))
                atomic=int(quote)*10**18
                amounts=[atomic,0,0,1] if side=='buy' else [0,1,atomic,0]
                raw=dict(address='pool',blockHash='h10',topics=[evm.SWAP2],data='0x'+''.join(f'{x:064x}' for x in amounts))
                self.db.execute("INSERT INTO chain_events VALUES(?,0,10,'h10','swap','pool',?)",(tx,json.dumps(raw)))
                self.db.execute('INSERT INTO chain_swaps VALUES(?,0,?,?,?,990,?,?,?,1)',(tx,'pool','token',w,side,'1',str(atomic)))
        r=m.signal(self.db,snapshot(),1000,dict(w1={'available_before':900},w2={'available_before':900}))
        self.assertEqual(len(r['buyers']),1)
        self.assertIn('verified_net_buying_not_positive',r['reasons'])
        self.assertIn('fewer_than_two_distinct_buyers',r['reasons'])

    def test_pool_net_flow_does_not_require_every_wallet_to_be_identified(self):
        self.test_net_sell_and_shared_origin_do_not_qualify()
        with self.db:
            self.db.execute("DELETE FROM chain_swaps WHERE tx='c'")
            self.db.execute("UPDATE momentum_flows SET origin='different' WHERE tx='b'")
            self.db.execute("DELETE FROM momentum_flows WHERE tx='c'")
            # Add an unattributed buy: its pool amount is still verifiable.
            raw=dict(address='pool',blockHash='h10',topics=[evm.SWAP2],data='0x'+''.join(f'{x:064x}' for x in [10**18,0,0,1]))
            self.db.execute("INSERT INTO chain_events VALUES('d',0,10,'h10','swap','pool',?)",(json.dumps(raw),))
            self.db.execute("INSERT INTO chain_swaps VALUES('d',0,'pool','token',NULL,990,'buy','1',?,0)",(str(10**18),))
        r=m.signal(self.db,snapshot(),1000,{})
        self.assertEqual(r['net_buy_eth'],'3')
        self.assertEqual(r['observed_transactions'],3)
        self.assertEqual(r['attributed_transactions'],2)
        self.assertEqual(r['reasons'],[])

    def test_unknown_exit_is_never_reported_as_measured_wallet_loss(self):
        with self.db:
            self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status,ended_at,completed_at,return_fraction,stress_return,replay_version) VALUES('x','w','t',100,100,'unresolved',200,500,NULL,-1,2)")
        r=m.rankings(self.db,501)[0]
        self.assertEqual(r['samples'],0)
        self.assertIsNone(r['measured_mean_return'])
        self.assertEqual(r['stressed_exits'],1)
        self.assertFalse(r['qualified'])

    def test_future_and_stale_observations_blocked(self):
        with self.assertRaises(ValueError):m.observation(snapshot(),1000,now=999)
        with self.assertRaises(ValueError):m.observation(snapshot(),1000,now=1121)


    def test_missing_live_exit_stays_open_at_zero_stressed_value(self):
        with self.db:
            self.db.execute("INSERT INTO momentum_positions VALUES(1,'wallet_momentum','token','pool','100',100,2,2,990,'quoted',1)")
        m.liquidate(self.db,None,1000)
        r=m.state(self.db,1000)['accounts']
        a=next(a for a in r if a['arm']=='wallet_momentum')
        self.assertEqual(a['positions'][0]['stressed_value'],0)
        self.assertEqual(a['realized_pnl'],0)
        self.assertEqual(a['fills'],0)

    def test_independent_exit_quote_accounts_for_fees_and_slippage(self):
        s=snapshot()
        with self.db:
            self.db.execute('INSERT INTO momentum_quotes VALUES(?,?,?,?)',('pool','h10','100','1000000000000000'))
        expected=1.995*.99-.005
        self.assertAlmostEqual(m.exit_value(self.db,None,s,100,1000),expected)

    def test_live_arms_are_idempotent_and_astra_cannot_default_to_approval(self):
        s=snapshot();s.update(liquidity=100000)
        s['market_evidence']['lp_burned_fraction']=1
        self.decision(1000,s)
        sig=dict(buyers={'a':{},'b':{}},qualified_wallets=['a','b'],reasons=[])
        with patch.object(m,'capture_flows'), patch.object(m,'risk_reasons',return_value=[]), \
             patch.object(m,'signal',return_value=sig), patch.object(m,'live_evidence',return_value=s), \
             patch.object(m,'exit_value',return_value=1.95), patch.object(m.time,'time',return_value=1000):
            m.live_tick(self.db,None,Config(),now=1000)
            m.live_tick(self.db,None,Config(),now=1000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM momentum_trades').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM momentum_positions').fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT cash FROM momentum_accounts WHERE arm='wallet_momentum_astra'").fetchone()[0],40)
        self.assertEqual(self.store.account()['cash'],40)
        self.assertIn('astra_not_enabled',self.db.execute("SELECT reasons FROM momentum_decisions WHERE arm='wallet_momentum_astra'").fetchone()[0])


if __name__ == '__main__':unittest.main()
