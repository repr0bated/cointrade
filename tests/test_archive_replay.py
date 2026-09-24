import json
import unittest
from unittest.mock import Mock,patch

from cointrade import archive_replay as a, momentum as m, evm
from cointrade.config import Config
from cointrade.store import Store
from cointrade.onchain import schema
from tests.test_momentum import snapshot


class ArchiveReplayTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:',Config(),'robinhood');self.db=self.store.db
        schema(self.db);m.start(self.db,Config(),now=2000)
        with self.db:
            self.db.execute("INSERT INTO chain_cursor VALUES(1,4663,1,2000,'latest',2)")
            self.db.execute("INSERT INTO chain_pools VALUES('pool',2,?,'token',3000,?,1,900,'creator','create')",(evm.WETH,evm.ZERO))
            self.db.execute("INSERT INTO chain_events VALUES('leader',0,10,'h10','swap','pool','{}')")
            self.db.execute("INSERT INTO chain_swaps VALUES('leader',0,'pool','token','wallet',1000,'buy','100','100',1)")
            self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status,replay_version) VALUES('leader','wallet','token',1000,2000,'queued',2)")

    def tearDown(self):self.db.close()

    def evidence(self,db,rpc,case,target):
        s=snapshot(block=int(target),stamp=target)
        s['fx_evidence']=dict(source='test',available_at=target-10,close='2000')
        return s

    def case(self):return dict(self.db.execute('SELECT * FROM momentum_replay_cases').fetchone())

    def test_archive_entry_and_exit_without_any_scanner_assessments(self):
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM launch_decisions').fetchone()[0],0)
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',side_effect=[1.95,.2]):
            a.step(self.db,None,now=2000)
            self.assertEqual(self.case()['next_target'],1060)
            a.step(self.db,None,now=2001)
        c=self.case();self.assertEqual(c['status'],'complete');self.assertAlmostEqual(c['return_fraction'],-.9)
        self.assertEqual(c['reason'],'stop_loss')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM momentum_trades').fetchone()[0],0)

    def test_transient_rpc_failure_retries_same_time_without_fabricated_loss(self):
        with patch.object(a,'archive_evidence',side_effect=ValueError('Provider connection failed')):
            a.step(self.db,None,now=2000)
        c=self.case();self.assertEqual(c['status'],'entry');self.assertEqual(c['next_target'],1030)
        self.assertIsNone(c['return_fraction']);self.assertIsNone(c['stress_return'])
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):
            self.assertFalse(a.step(self.db,None,now=2001))
            a.step(self.db,None,now=2010)
        self.assertEqual(self.case()['status'],'exit')

    def test_failed_entry_is_rejection_not_missing_data_or_a_trade(self):
        with patch.object(a,'archive_evidence',side_effect=ValueError('Pool has no reserves')):
            a.step(self.db,None,now=2000)
        self.assertEqual(self.case()['status'],'entry_rejected')
        self.assertIsNone(self.case()['return_fraction'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM momentum_trades').fetchone()[0],0)

    def test_confirmed_quote_revert_is_not_retried_as_provider_outage(self):
        with patch.object(a,'archive_evidence',side_effect=evm.CallReverted('RPC eth_call reverted')):
            a.step(self.db,None,now=2000)
        self.assertEqual(self.case()['status'],'entry_rejected')
        self.assertEqual(self.case()['retries'],0)

    def test_rpc_revert_category_does_not_leak_provider_details(self):
        rpc=evm.RPC.__new__(evm.RPC);rpc.url='https://example.invalid';rpc.calls=0;rpc.last=0
        with patch.object(evm,'throttle'),patch.object(evm,'request_json',return_value={'error':{'code':3,'message':'execution reverted: private provider details'}}):
            with self.assertRaises(evm.CallReverted) as caught:rpc.call('eth_call',[{},'latest'])
        self.assertEqual(str(caught.exception),'RPC eth_call reverted')
        with patch.object(evm,'throttle'),patch.object(evm,'request_json',return_value={'error':{'code':-32000,'message':'missing state'}}):
            with self.assertRaises(ValueError) as caught:rpc.call('eth_call',[{},'latest'])
        self.assertNotIsInstance(caught.exception,evm.CallReverted)

    def test_unavailable_final_exit_has_separate_stress_value(self):
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):a.step(self.db,None,now=2000)
        with self.db:self.db.execute('UPDATE momentum_replay_cases SET next_target=entry_at+900,retries=3')
        with patch.object(a,'archive_evidence',side_effect=ValueError('Provider connection failed')):a.step(self.db,None,now=2001)
        c=self.case();self.assertEqual(c['status'],'unresolved');self.assertIsNone(c['return_fraction']);self.assertEqual(c['stress_return'],-1)
        sample=self.db.execute('SELECT * FROM momentum_samples').fetchone()
        self.assertIsNone(sample['return_fraction']);self.assertEqual(sample['stress_return'],-1)

    def test_missing_interval_disqualifies_later_profit_as_fully_measured(self):
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):a.step(self.db,None,now=2000)
        with patch.object(a,'archive_evidence',side_effect=ValueError('Pool has no reserves')):a.step(self.db,None,now=2001)
        self.assertEqual(self.case()['has_gaps'],1)
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=2.5):a.step(self.db,None,now=2002)
        c=self.case();self.assertEqual(c['status'],'incomplete');self.assertEqual(c['return_fraction'],.25)
        self.assertEqual(c['stress_return'],-1)

    def test_timeout_is_sampled_on_schedule_and_restart_does_not_reset_it(self):
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):
            a.step(self.db,None,now=2000)
            for i in range(30):
                a.initialize(self.db,now=2001+i)
                a.step(self.db,None,now=2001+i)
        c=self.case();self.assertEqual(c['status'],'complete');self.assertEqual(c['reason'],'time_exit')
        self.assertEqual(c['ended_at'],c['entry_at']+900)
        self.assertEqual(len(json.loads(c['path'])),31)
        self.assertFalse(a.step(self.db,None,now=2100))

    def test_identical_pool_time_cases_share_archive_work(self):
        with self.db:
            self.db.execute("INSERT INTO chain_events VALUES('second',0,10,'h10','swap','pool','{}')")
            self.db.execute("INSERT INTO chain_swaps VALUES('second',0,'pool','token','another',1000,'buy','100','100',1)")
            self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status,replay_version) VALUES('second','another','token',1000,2000,'queued',2)")
        a.enroll(self.db,2000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM momentum_replay_cases').fetchone()[0],1)
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',side_effect=[1.95,2.5]):
            a.step(self.db,None,now=2000);a.step(self.db,None,now=2001)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM momentum_samples WHERE status='complete'").fetchone()[0],2)

    def test_first_seen_latency_cannot_be_replaced_by_faster_assumed_delay(self):
        with self.db:self.db.execute("INSERT INTO momentum_flows VALUES('leader','wallet','token',1000,1150,'buy','1','wallet')")
        a.enroll(self.db,2000)
        r=self.db.execute('SELECT * FROM momentum_samples').fetchone()
        self.assertEqual(r['status'],'entry_rejected');self.assertEqual(r['reason'],'observed_after_entry_deadline')

    def add_case(self,key,pool,target):
        with self.db:
            self.db.execute("INSERT INTO momentum_replay_cases(key,token,pool,anchor_block,entry_target,status,next_target) VALUES(?,'token',?,10,?,'entry',?)",(key,pool,target,target))

    def test_multiple_pools_progress_without_waiting_for_one_full_path(self):
        for i in range(1,4):self.add_case(str(i),f'pool{i}',1040+i)
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):
            for i in range(4):a.step(self.db,None,now=2000+i)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM momentum_replay_cases WHERE status='exit'").fetchone()[0],4)
        slots=[tuple(r) for r in self.db.execute('SELECT * FROM momentum_replay_slots ORDER BY slot')]
        a.initialize(self.db,now=2010)
        self.assertEqual(slots,[tuple(r) for r in self.db.execute('SELECT * FROM momentum_replay_slots ORDER BY slot')])
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=2.5):
            for i in range(4):a.step(self.db,None,now=2010+i)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM momentum_replay_cases WHERE status='complete'").fetchone()[0],4)
        for r in self.db.execute('SELECT * FROM momentum_replay_cases'):
            self.assertEqual(r['ended_at']-r['entry_at'],30)
            self.assertEqual(len(json.loads(r['path'])),2)

    def test_pool_retry_does_not_block_another_pool_or_skip_its_own_target(self):
        self.add_case('other','other_pool',1040)
        with patch.object(a,'archive_evidence',side_effect=ValueError('Provider connection failed')):a.step(self.db,None,now=2000)
        with patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):a.step(self.db,None,now=2001)
        first=self.db.execute("SELECT * FROM momentum_replay_cases WHERE pool='pool'").fetchone()
        self.assertEqual(first['next_target'],1030);self.assertEqual(first['retries'],1)
        self.assertIsNone(first['return_fraction'])
        self.assertEqual(self.db.execute("SELECT status FROM momentum_replay_cases WHERE key='other'").fetchone()[0],'exit')

    def test_many_early_buyers_cannot_monopolize_the_next_pool_assignment(self):
        # A second early buyer in the same pool stays queued while a later,
        # untouched pool gets a slot. No profitability fields enter scheduling.
        self.add_case('repeat','pool',1031)
        self.add_case('fresh','new_pool',2000)
        with patch.object(a,'ACTIVE_CASES',1),patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',side_effect=[1.95,2.5,1.95]):
            a.step(self.db,None,now=3000);a.step(self.db,None,now=3001);a.step(self.db,None,now=3002)
        statuses=dict(self.db.execute("SELECT key,status FROM momentum_replay_cases WHERE key IN ('repeat','fresh')"))
        self.assertEqual(statuses,dict(repeat='entry',fresh='exit'))

    def test_long_hold_yields_then_resumes_without_skipping_an_observation(self):
        self.add_case('other','other_pool',1040)
        with patch.object(a,'ACTIVE_CASES',1),patch.object(a,'archive_evidence',side_effect=self.evidence),patch.object(m,'exit_value',return_value=1.95):
            for i in range(5):a.step(self.db,None,now=3000+i)
            first=self.db.execute("SELECT * FROM momentum_replay_cases WHERE pool='pool'").fetchone()
            self.assertEqual(first['next_target'],1150)
            self.assertEqual(self.db.execute("SELECT status FROM momentum_replay_cases WHERE key='other'").fetchone()[0],'exit')
            # A new arrival joins behind the earlier unfinished pool.
            self.add_case('new','new_pool',1200)
            a.initialize(self.db,now=3005)
            for i in range(5,9):a.step(self.db,None,now=3000+i)
        first=self.db.execute("SELECT * FROM momentum_replay_cases WHERE pool='pool'").fetchone()
        self.assertEqual([p['target'] for p in json.loads(first['path'])],[1030,1060,1090,1120,1150])
        self.assertEqual(self.db.execute("SELECT status FROM momentum_replay_cases WHERE key='new'").fetchone()[0],'entry')

    def test_wallet_focus_uses_chronology_and_finishes_losers_too(self):
        with self.db:
            for wallet,stamp,ret in [('early',100,-.8),('later',200,.8)]:
                for i in range(10):
                    self.db.execute('''INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status,return_fraction,replay_version)
                      VALUES(?,?,?,?,2000,?,?,2)''',(wallet+str(i),wallet,str(i),stamp+i,'queued' if i==9 else 'complete',None if i==9 else ret))
        self.assertEqual(a.focus_wallet(self.db,2000),'early')
        a.initialize(self.db,now=2001)
        self.assertEqual(a.focus_wallet(self.db,2001),'early')
        with self.db:self.db.execute("UPDATE momentum_samples SET status='unresolved' WHERE wallet='early' AND status='queued'")
        self.assertEqual(a.focus_wallet(self.db,2002),'later')
        self.assertEqual(self.db.execute("SELECT finished_at FROM momentum_replay_focus WHERE wallet='early'").fetchone()[0],2002)

    def test_three_slots_focus_on_one_wallet_and_one_keeps_broad_coverage(self):
        a.enroll(self.db,2000)
        for i in range(10):
            key='focus'+str(i);self.add_case(key,'pool'+str(i),1200+i)
            with self.db:
                self.db.execute("INSERT INTO momentum_samples(tx,wallet,token,leader_ts,queued_at,status,replay_version) VALUES(?,'repeat',?,1100,2000,'replaying',2)",(key,'token'+str(i)))
                self.db.execute('INSERT INTO momentum_replay_members VALUES(?,?)',(key,key))
        a.next_case(self.db,2001)
        slots=dict(self.db.execute('SELECT slot,case_key FROM momentum_replay_slots'))
        self.assertTrue(all(slots[i].startswith('focus') for i in range(3)))
        self.assertFalse(slots[3].startswith('focus'))

    def test_historical_fx_excludes_unfinished_candles(self):
        with self.db:
            self.db.execute("INSERT INTO momentum_fx_candles VALUES(900,'2000',960,'coinbase')")
            self.db.execute("INSERT INTO momentum_fx_candles VALUES(960,'3000',1020,'coinbase')")
        with patch.object(a,'request_json') as request:
            value,provenance=a.fx_at(self.db,1000)
        self.assertEqual(value,2000);self.assertEqual(provenance['available_at'],960);request.assert_not_called()

    def test_atomic_execution_replay_does_not_depend_on_metadata_or_tvl_lens(self):
        from decimal import Decimal
        rpc=Mock();rpc.block.return_value=dict(hash='h10')
        with patch.object(a,'block_at',return_value=dict(number=10,hash='h10',ts=1000)), \
             patch.object(a,'fx_at',return_value=(Decimal('2000'),{'available_at':960})), \
             patch.object(a.quotes,'market',side_effect=ValueError('Optional TVL lens unavailable')) as market, \
             patch.object(a.quotes,'exact_input',side_effect=[100000,990000000000000]), \
             patch.object(a.simulation,'roundtrip',return_value=snapshot()['trade_simulation']):
            r=a.archive_evidence(self.db,rpc,dict(pool='pool',token='token',anchor_block=1),1000)
        self.assertAlmostEqual(r['execution_quote']['usd_in'],1.995)
        self.assertEqual(r['execution_quote']['token_out_atomic'],'100000')
        market.assert_not_called();rpc.read.assert_not_called()

    def test_block_search_returns_first_block_at_or_after_target(self):
        rpc=Mock()
        rpc.block.side_effect=lambda n:dict(number=hex(n),hash=f'h{n}',timestamp=hex(1000+(n-10)//2))
        result=a.block_at(self.db,rpc,1030,10)
        self.assertEqual(result['number'],70)
        self.assertEqual(result['ts'],1030)

    def test_legacy_missing_data_results_are_archived_not_reused_as_losses(self):
        with self.db:
            self.db.execute('DELETE FROM momentum_replay_version')
            self.db.execute("UPDATE momentum_samples SET status='stressed_loss',return_fraction=-1,completed_at=1900,replay_version=1")
            self.db.execute("UPDATE momentum_accounts SET cash=38 WHERE arm='simple_momentum'")
        a.initialize(self.db,now=2000)
        r=self.db.execute('SELECT * FROM momentum_samples').fetchone()
        self.assertEqual(r['status'],'queued');self.assertIsNone(r['return_fraction'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM momentum_sample_archive').fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT cash FROM momentum_accounts WHERE arm='simple_momentum'").fetchone()[0],38)


if __name__=='__main__':unittest.main()
