import copy
import json
import math
import sqlite3
import unittest
from cointrade import evidence, ledger, subscription, astra_live
from cointrade.config import Config
from cointrade.launchrisk import candidate_score, decide
from cointrade.onchain import schema
from cointrade.store import Store

TOKEN='0x'+'1'*40
POOL='0x'+'2'*40
HASH='0x'+'3'*64
TX='0x'+'4'*64


def snapshot(now=1000):
    meta=dict(block=100,block_hash=HASH,observed_at=now)
    return dict(token=TOKEN,pool=POOL,observed_at=now,price=1,liquidity=100000,top10_share=.2,
      risk_checked_at=now,risk_checks={x:'pass' for x in evidence.CONTROLS},risk_reasons=[],
      launch_age=20,deployment_confirmed=True,distinct_buyers=3,qualified_wallet_buys=0,
      market_evidence=dict(source='v2_reserves',lp_total_supply_atomic='100',lp_burned_fraction=1,**meta),
      execution_quote=dict(status='quoted',quantity=1.995,usd_in=1.995,source='canonical_pool_quote',**meta),
      trade_simulation=dict(status='passed',source='eth_simulateV1',buy_transfer_shortfall=0,sell_transfer_shortfall=0,roundtrip_loss_fraction=.01,**meta),
      holder_evidence=dict(source='verified_transfer_ledger',**meta),
      buyer_evidence=dict(transactions=3,reviewed=3,pending=0,ambiguous=0,**meta),
      source_verification=dict(status='verified',provider='blockscout',checked_at=now,**meta))


class EvidenceTests(unittest.TestCase):
    def test_valid_contract_and_passed_scoped_simulation(self):
        s=snapshot();s['risk_checks']['cannot_sell_all']='unknown'
        doc=evidence.build(s,1000)
        self.assertEqual(evidence.validate(doc),[])
        q=evidence.quality(doc,1000)
        self.assertIn('SELLABILITY_PROVIDER_UNKNOWN',[f['code'] for f in q['findings']])
        self.assertNotIn('blocker',[f['severity'] for f in q['findings']])
        self.assertEqual(doc['measurements']['holders.adjusted_top10']['value'],None)

    def test_wrong_types_nonfinite_numbers_ranges_and_extra_fields_rejected(self):
        original=evidence.build(snapshot(),1000)
        for key,val in [('market.price',True),('market.price',float('nan')),('market.price',float('inf')),
          ('buyers.total',2.5),('liquidity.lp_supply',100),('holders.raw_top10',1.1)]:
            with self.subTest(key=key,value=val):
                doc=copy.deepcopy(original);doc['measurements'][key]['value']=val
                self.assertTrue(evidence.validate(doc))
        doc=copy.deepcopy(original);doc['extra']='x';self.assertTrue(evidence.validate(doc))
        doc=copy.deepcopy(original);doc['measurements']['market.price']['unit']='ETH'
        self.assertTrue(evidence.validate(doc))

    def test_partial_buyer_history_is_a_lower_bound_not_zero(self):
        s=snapshot();s['buyer_evidence'].update(transactions=372,reviewed=32,pending=340,ambiguous=1)
        doc=evidence.build(s,1000);m=doc['measurements']
        self.assertEqual(evidence.validate(doc),[])
        self.assertEqual(m['buyers.distinct']['status'],'partial')
        self.assertAlmostEqual(m['buyers.coverage']['value'],32/372)
        m['buyers.pending']['value']=341
        self.assertTrue(evidence.validate(doc))

    def test_zero_denominators_remain_undefined(self):
        s=snapshot();s['market_evidence'].update(lp_total_supply_atomic='0',lp_burned_fraction=None)
        s['buyer_evidence'].update(transactions=0,reviewed=0,pending=0,ambiguous=0)
        s.update(distinct_buyers=0,qualified_wallet_buys=0)
        doc=evidence.build(s,1000);self.assertEqual(evidence.validate(doc),[])
        for key in ('liquidity.lp_burned','buyers.coverage'):
            self.assertIsNone(doc['measurements'][key]['value'])
            self.assertEqual(doc['measurements'][key]['status'],'undefined')
        doc['measurements']['liquidity.lp_burned'].update(value=0,status='measured')
        self.assertTrue(evidence.validate(doc))

    def test_v4_lp_burning_is_not_applicable_and_not_zero_supply(self):
        s=snapshot();s['pool_version']=4
        doc=evidence.build(s,1000);self.assertEqual(evidence.validate(doc),[])
        self.assertEqual(doc['measurements']['liquidity.lp_burned']['status'],'not_applicable')
        self.assertIsNone(doc['measurements']['liquidity.lp_supply']['value'])
        q=evidence.quality(doc,1000)
        self.assertIn('LIQUIDITY_LOCK_UNRESOLVED',[f['code'] for f in q['findings']])
        content=json.dumps({'evidence':{'evidence_contract':doc}})
        codes=subscription.review_schema(content)['properties']['findings']['items']['properties']['code']['enum']
        self.assertNotIn('UNDEFINED_LP_BURN',codes)

    def test_stale_or_invalid_values_cannot_support_entry(self):
        s=snapshot();s['evidence_contract']=evidence.build(s,1000)
        c=Config();self.assertEqual(decide(s,1000,c)[0],'SNIPE')
        old=candidate_score(s,1000,c)['value']
        self.assertLess(candidate_score(s,1200,c)['value'],old)
        self.assertEqual(decide(s,1200,c)[0],'SKIP')
        s['evidence_contract']['measurements']['market.price']['value']=True
        self.assertEqual(candidate_score(s,1000,c)['value'],0)
        self.assertIn('evidence_schema_invalid',decide(s,1000,c)[2])

    def test_unknown_provenance_and_unclassified_holdings_are_not_invented(self):
        s=snapshot();s['holder_evidence'].pop('observed_at')
        doc=evidence.build(s,1000,origin='legacy_adapter')
        self.assertEqual(doc['measurements']['holders.raw_top10']['status'],'partial')
        self.assertIsNone(doc['measurements']['holders.raw_top10']['observed_at'])
        self.assertIn('MISSING_PROVENANCE',[f['code'] for f in evidence.quality(doc,1000)['findings']])
        doc['measurements']['holders.adjusted_top10'].update(value=.1,status='measured',source='guess',observed_at=1000)
        self.assertTrue(evidence.validate(doc))

    def test_source_verification_does_not_resolve_admin_permissions(self):
        s=snapshot();s['risk_checks']={};doc=evidence.build(s,1000)
        self.assertTrue(doc['measurements']['source.verified']['value'])
        self.assertIn('MISSING_PERMISSIONS',[f['code'] for f in evidence.quality(doc,1000)['findings']])

    def test_typed_astra_references_and_actions_are_validated(self):
        answer=dict(action='SKIP',rationale='Missing permission proof',missing_evidence=[],known_failures=[],data_quality_findings=[],
          findings=[dict(code='MISSING_PERMISSIONS',evidence_ids=['controls.is_mintable'],detail='Unknown role',follow_up='refresh_permissions',severity='blocker')])
        content=json.dumps({'evidence':{'evidence_contract':evidence.build(snapshot(),1000)}})
        self.assertEqual(subscription.validate(answer,content),answer)
        for key,value in [('follow_up','broadcast_trade'),('code','CUSTOM_CODE'),('evidence_ids',['made_up_field'])]:
            bad=copy.deepcopy(answer);bad['findings'][0][key]=value
            with self.assertRaises(ValueError):subscription.validate(bad,content)
        with self.assertRaises(ValueError):subscription.validate(answer,'{"evidence":{}}')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:',Config(),'robinhood');self.db=self.store.db
        self.addCleanup(self.db.close);schema(self.db)

    def event(self,tx=TX,hash_=HASH,height=100):
        raw=dict(transactionHash=tx,blockHash=hash_,blockNumber=hex(height),blockTimestamp=hex(1000),transactionIndex='0x0')
        self.db.execute('INSERT INTO chain_events VALUES(?,?,?,?,?,?,?)',(tx,0,height,hash_,'token_mint',TOKEN,json.dumps(raw)))

    def test_backfill_is_bounded_resumable_and_receipts_enrich_partial_records(self):
        with self.db:
            self.event();self.event('0x'+'5'*64)
        ledger.sync(self.db,1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM data_logs').fetchone()[0],1)
        row=self.db.execute('SELECT * FROM data_transactions').fetchone()
        self.assertEqual(row['receipt_available'],0);self.assertIsNone(row['sender'])
        raw=dict(transactionHash=TX,blockHash=HASH,blockNumber='0x64',transactionIndex='0x0',
          status='0x1',gasUsed=hex(21000),effectiveGasPrice=hex(10**20),**{'from':TOKEN,'to':POOL})
        with self.db:self.db.execute('INSERT INTO chain_receipts VALUES(?,?)',(TX,json.dumps(raw)))
        ledger.sync(self.db,1);ledger.sync(self.db,1)
        row=self.db.execute('SELECT * FROM data_transactions WHERE tx=?',(TX,)).fetchone()
        self.assertEqual(row['receipt_gas_fee_wei'],str(21000*10**20))
        self.assertIsNone(row['value_wei']);self.assertEqual(row['receipt_available'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM data_logs').fetchone()[0],2)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(ledger.state(self.db)['counts']['transactions'],2)

    def test_conflicting_block_rolls_back_batch_and_preserves_cursor(self):
        with self.db:self.event()
        ledger.sync(self.db)
        with self.db:self.event('0x'+'5'*64,'0x'+'6'*64)
        before=self.db.execute("SELECT last_rowid FROM data_sync WHERE source='chain_events'").fetchone()[0]
        with self.assertRaises(ValueError):ledger.sync(self.db)
        self.assertEqual(self.db.execute("SELECT last_rowid FROM data_sync WHERE source='chain_events'").fetchone()[0],before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM data_logs').fetchone()[0],1)

    def test_foreign_keys_reject_orphan_transactions(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO data_transactions(tx,block_hash) VALUES(?,?)',(TX,HASH))

    def test_observations_relate_to_decision_and_block_without_rounding_atomic_balances(self):
        doc=evidence.build(snapshot(),1000);q=evidence.quality(doc,1000)
        with self.db:
            self.db.execute("INSERT INTO launch_decisions VALUES(1,1000,?,?,'SKIP',0,'[]','{}')",(TOKEN,POOL))
            ledger.save_evidence(self.db,1,doc,q)
            ledger.balances(self.db,TOKEN,HASH,[(POOL,10**70+7)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence_measurements').fetchone()[0],len(evidence.SPECS))
        self.assertEqual(self.db.execute('SELECT amount_atomic FROM data_balances').fetchone()[0],str(10**70+7))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_refresh_deduplication_cooldown_cap_and_unavailable_are_honest(self):
        now=1000
        for attempt in range(3):
            with self.db:
                self.assertTrue(ledger.request_refresh(self.db,TOKEN,'refresh_permissions',now))
                self.assertFalse(ledger.request_refresh(self.db,TOKEN,'refresh_permissions',now))
            self.assertEqual(ledger.refresh_actions(self.db,TOKEN,now),{'refresh_permissions'})
            q=dict(valid=True,findings=[dict(follow_up='refresh_permissions')])
            ledger.finish_refresh(self.db,TOKEN,q,now)
            r=self.db.execute('SELECT * FROM evidence_refresh_requests').fetchone()
            self.assertEqual(r['status'],'unavailable');self.assertEqual(r['attempts'],attempt+1)
            self.assertFalse(ledger.request_refresh(self.db,TOKEN,'refresh_permissions',now+10))
            now+=301
        self.assertFalse(ledger.request_refresh(self.db,TOKEN,'refresh_permissions',now))
        self.assertFalse(ledger.request_refresh(self.db,TOKEN,'broadcast_trade',now))

    def test_invalid_contract_is_recorded_as_failure_not_measurements(self):
        doc=evidence.build(snapshot(),1000);doc['schema_version']=99
        with self.db:
            self.db.execute("INSERT INTO launch_decisions VALUES(1,1000,?,?,'SKIP',0,'[]','{}')",(TOKEN,POOL))
            ledger.save_evidence(self.db,1,doc,evidence.quality(doc,1000))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence_measurements').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT code FROM evidence_findings').fetchone()[0],'SCHEMA_INVALID')

    def test_contract_code_and_source_have_separate_provenance(self):
        with self.db:
            self.db.execute('INSERT INTO chain_tokens VALUES(?,?,?,1,18,?,NULL,?)',
              (TOKEN,100,1000,'hash',json.dumps({'bytecode':'0x6001','bytecode_block':100})))
            self.db.execute('INSERT INTO chain_source_checks VALUES(?,?,?)',
              (TOKEN,1001,json.dumps({'status':'verified','provider':'blockscout'})))
            self.db.execute('INSERT INTO chain_source_documents VALUES(?,?,?,?)',
              (TOKEN,'blockscout',1001,json.dumps({'abi':[{'type':'function','name':'mint'}]})))
        ledger.sync(self.db)
        row=self.db.execute('SELECT * FROM data_contracts').fetchone()
        self.assertEqual(row['runtime_code'],'0x6001')
        self.assertEqual(row['source_status'],'verified')
        self.assertEqual(json.loads(row['abi_json'])[0]['name'],'mint')
        account=self.db.execute('SELECT * FROM data_accounts').fetchone()
        self.assertEqual(account['is_contract'],1);self.assertEqual(account['classification_block'],100)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_zero_log_timestamp_cannot_overwrite_a_known_block_time(self):
        with self.db:
            self.event()
            raw=dict(transactionHash='0x'+'5'*64,blockHash=HASH,blockNumber='0x64',blockTimestamp='0x0')
            self.db.execute('INSERT INTO chain_events VALUES(?,?,?,?,?,?,?)',
              (raw['transactionHash'],0,100,HASH,'token_mint',TOKEN,json.dumps(raw)))
        ledger.sync(self.db)
        self.assertEqual(self.db.execute('SELECT timestamp FROM data_blocks').fetchone()[0],1000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM data_logs').fetchone()[0],2)
