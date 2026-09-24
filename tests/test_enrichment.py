import unittest
from cointrade import enrichment,evm

class EnrichmentTests(unittest.TestCase):
    def test_zero_lp_supply_is_undefined_not_zero_percent_burned(self):
        class RPC:
            def read(self,token,data,height):
                return '0x'+'0'*64*(3 if data=='0x0902f1ac' else 1)
        value=enrichment.v2_market(RPC(),dict(pool='pool',token0=evm.WETH),18,100,2000)
        self.assertIsNone(value['lp_burned_fraction'])
        self.assertEqual(value['lp_burn_status'],'undefined')
        self.assertEqual(value['lp_burn_reason'],'zero_lp_supply')
        self.assertEqual(value['lp_total_supply_atomic'],'0')
        self.assertEqual(value['liquidity'],0)

    def test_spot_decimal_orientation(self):
        self.assertEqual(enrichment.concentrated_spot(2**96,0,18,2000),2000)
        self.assertAlmostEqual(enrichment.concentrated_spot(2**96,1,6,2000),2e-9)

    def test_holders_reconcile_supply_and_balances(self):
        addr='0x'+'1'*40
        class RPC:
            def logs(self,*args):
                return [dict(topics=[evm.TRANSFER,evm.ZERO_TOPIC,'0x'+addr[2:].zfill(64)],data='0x'+hex(100)[2:].zfill(64))]
            def read(self,*args):return '0x'+hex(100)[2:].zfill(64)
            def reads(self,*args):return ['0x'+hex(100)[2:].zfill(64)]
        self.assertEqual(enrichment.holders(RPC(),'token',1,5)['top10_share'],1)
        class Inconsistent(RPC):
            def reads(self,*args):return ['0x'+hex(99)[2:].zfill(64)]
        with self.assertRaisesRegex(ValueError,'balanceOf'):
            enrichment.holders(Inconsistent(),'token',1,5)

    def test_wallet_flow_requires_both_legs(self):
        wallet='0x'+'1'*40
        other='0x'+'2'*40
        token='0x'+'3'*40
        def transfer(asset,src,dst,n):
            return dict(address=asset,topics=[evm.TRANSFER,'0x'+src[2:].zfill(64),'0x'+dst[2:].zfill(64)],data='0x'+hex(n)[2:].zfill(64))
        receipt={'from':wallet,'logs':[transfer(token,other,wallet,100)]}
        self.assertIsNone(enrichment.erc20_flow(receipt,token))
        receipt['logs'].append(transfer(evm.WETH,wallet,other,10))
        self.assertEqual(enrichment.erc20_flow(receipt,token),(100,-10))
        receipt['logs'].append(dict(address=other,topics=[evm.MINT2]))
        self.assertIsNone(enrichment.erc20_flow(receipt,token))
