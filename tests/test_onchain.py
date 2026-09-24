import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from cointrade import evm, wallets
from cointrade.config import Config
from cointrade.launchrisk import FLAGS, assess, decide
from cointrade.onchain import Scanner, schema
from cointrade.store import Store


def abi(*values):
    return '0x' + ''.join(f'{v % 2**256:064x}' for v in values)


class OnchainTests(unittest.TestCase):
    def setUp(self):
        self.c = Config()
        self.store = Store(':memory:', self.c, 'robinhood')
        self.addCleanup(self.store.db.close)
        schema(self.store.db)

    def test_factory_provenance_and_v3_decode(self):
        log = {'address': evm.V3, 'topics': [evm.POOL, abi(1), abi(2), abi(10000)], 'data': abi(200, 3)}
        p = evm.pool_created(log)
        self.assertEqual(p['pool'], evm.address(3))
        self.assertEqual(p['fee'], 10000)
        log['address'] = evm.address(55)
        self.assertIsNone(evm.pool_created(log))

    def test_swap_direction_differs_for_v4(self):
        for topic, data in ((evm.SWAP2, abi(100,0,0,20)),
                            (evm.SWAP3, abi(100,-20,1,1,0)),
                            (evm.SWAP4, abi(-100,20,1,1,0,3000))):
            self.assertEqual(evm.swap_amounts({'topics':[topic], 'data':data}), (100,-20))

    def test_token_transfers_not_nft_or_router(self):
        logs = [dict(address=evm.address(1), topics=[evm.TRANSFER,abi(2),abi(3)],data=abi(50)),
                dict(address=evm.address(1), topics=[evm.TRANSFER,abi(2),abi(3),abi(99)],data='0x')]
        self.assertEqual(evm.transfers(logs, evm.address(1), evm.address(3)), 50)
        self.assertEqual(evm.transfers(logs, evm.address(1), evm.address(4)), 0)

    def test_fifo_partial_close_and_gas_costs(self):
        db = self.store.db
        wallets.record(db,'buy','wallet','token',1000,'100','-1','.01',2)
        wallets.record(db,'sell1','wallet','token',1100,'-50','.7','.01',2)
        self.assertEqual(wallets.profile(db,'wallet')['closed_lots'], 0)
        wallets.record(db,'sell2','wallet','token',1200,'-50','.7','.01',2)
        p = wallets.profile(db,'wallet')
        self.assertEqual(Decimal(p['realized_pnl_weth']), Decimal('.37'))
        self.assertEqual(p['closed_lots'],1)
        self.assertFalse(p['mirror_eligible'])
        wallets.record(db,'sell2','wallet','token',1200,'-50','.7','.01',2)
        self.assertEqual(wallets.profile(db,'wallet')['closed_lots'],1)

    def test_unknown_basis_is_not_profit(self):
        wallets.record(self.store.db,'sell','wallet','token',1000,'-100','10','.01',2)
        p = wallets.profile(self.store.db,'wallet')
        self.assertEqual(p['wins'],0)
        self.assertEqual(p['unknown_basis_or_attribution'],1)

    def test_profitable_wallet_requires_diverse_realized_history(self):
        for i in range(10):
            token = f'token{i}'
            wallets.record(self.store.db,f'b{i}','wallet',token,1000,'100','-1','0.01',2)
            wallets.record(self.store.db,f's{i}','wallet',token,1100,'-100','1.5','0.01',2)
        self.assertTrue(wallets.profile(self.store.db,'wallet')['mirror_eligible'])

    def test_no_missing_security_field_becomes_safe(self):
        pool=dict(version=2,pool='pool',hooks=evm.ZERO)
        risk = assess({},pool,1000)
        self.assertTrue(risk['reasons'])
        self.assertIsNone(risk['top10_share'])
        data={key:'0' for key in FLAGS}
        data.update(is_open_source='1',buy_tax='0',sell_tax='0',holder_count='1',holders=[{'percent':'.1'}],
                    dex=[{'pair':'pool','liquidity':'100000'}],lp_holders=[{'address':evm.ZERO,'percent':'1'}])
        self.assertEqual(assess(data,pool,1000)['reasons'], [])
        del data['is_mintable']
        self.assertIn('is_mintable_unknown',assess(data,pool,1000)['reasons'])

    def test_source_verification_distinguishes_unavailable_from_unverified(self):
        pool=dict(version=2,pool='pool',hooks=evm.ZERO)
        missing=assess({},pool,1000)
        unverified=assess({'is_open_source':'0'},pool,1000)
        verified=assess({'is_open_source':'1'},pool,1000)
        self.assertEqual(missing['source_status'],'unavailable')
        self.assertIn('source_verification_data_unavailable',missing['reasons'])
        self.assertEqual(unverified['source_status'],'unverified')
        self.assertIn('source_reported_unverified_by_goplus',unverified['reasons'])
        self.assertEqual(verified['source_status'],'verified')
        self.assertFalse(any(r.startswith('source_') for r in verified['reasons']))

    def test_snipe_mirror_skip_and_risk_precedence(self):
        s=dict(token='token',price=1,observed_at=1000,liquidity=100000,top10_share=.2,
               risk_reasons=[],risk_checked_at=1000,launch_age=18,deployment_confirmed=True,
               distinct_buyers=3,qualified_wallet_buys=0,source_verification={'status':'verified'},
               risk_checks={k:'pass' for k in FLAGS},market_evidence={'lp_burned_fraction':1})
        self.assertEqual(decide(s,1000,self.c)[0], 'SNIPE')
        s['qualified_wallet_buys']=1
        self.assertEqual(decide(s,1000,self.c)[0], 'MIRROR')
        s['risk_reasons']=['is_honeypot_flagged']
        self.assertEqual(decide(s,1000,self.c)[0], 'SKIP')

    def test_cursor_resume_and_reorg_halts(self):
        class FakeRPC:
            calls=0
            bad=False
            def verify(self): pass
            def call(self,method,params):
                if method == 'eth_blockNumber': return hex(102)
                raise AssertionError(method)
            def block(self,n):
                return {'hash': ('bad' if self.bad else 'hash') + str(n), 'timestamp':hex(1000+n)}
            def logs(self,*args): return []
        rpc=FakeRPC()
        scanner=Scanner(self.store,self.c,rpc,lookback=1,batch_blocks=1)
        scanner.ingest()
        self.assertEqual(self.store.db.execute('SELECT height FROM chain_cursor').fetchone()[0],99)
        scanner.ingest()
        self.assertEqual(self.store.db.execute('SELECT height FROM chain_cursor').fetchone()[0],100)
        scanner.ingest()
        rpc.bad=True
        with self.assertRaises(ValueError): scanner.ingest()
        self.assertEqual(self.store.account()['halted'],1)

    def test_assessment_excludes_token_metadata_from_a_future_block(self):
        class FakeRPC:
            def verify(self):pass
            def block(self,n):return {'hash':'hash','timestamp':hex(1000)}
        with self.store.db as db:
            db.execute('INSERT INTO chain_cursor VALUES(1,4663,90,100,\'hash\',2)')
            db.execute('INSERT INTO chain_tokens VALUES(\'token\',101,1001,1,18,\'code\',NULL,\'{}\')')
            db.execute('INSERT INTO chain_pools VALUES(\'pool\',2,?,\'token\',3000,?,100,999,\'wallet\',\'tx\')',(evm.WETH,evm.ZERO))
        scanner=Scanner(self.store,self.c,FakeRPC());scanner.head=102
        with patch('cointrade.onchain.security',side_effect=AssertionError('Future token assessed')):
            self.assertEqual(scanner.snapshots(1000),[])

    def test_failed_fetch_preserves_cursor(self):
        class BrokenRPC:
            calls=0
            def verify(self): pass
            def call(self,*args): return hex(102)
            def block(self,n): return {'hash':str(n),'timestamp':hex(1000+n)}
            def logs(self,*args): raise ValueError('rate limited')
        scanner=Scanner(self.store,self.c,BrokenRPC(),lookback=1)
        with self.assertRaises(ValueError): scanner.ingest()
        self.assertEqual(self.store.db.execute('SELECT height FROM chain_cursor').fetchone()[0],98)


if __name__ == '__main__':
    unittest.main()
