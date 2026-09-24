import json
import unittest
from unittest.mock import Mock

from cointrade import attribution as a, evm
from cointrade.config import Config
from cointrade.store import Store
from cointrade.onchain import schema
from cointrade.simulation import V2_ROUTER

WALLET='0x'+'3'*40
TOKEN='0x'+'1'*40
POOL='0x'+'2'*40


def transfer(asset,sender,recipient,amount):
    return dict(address=asset,topics=[evm.TRANSFER,'0x'+sender[2:].zfill(64),'0x'+recipient[2:].zfill(64)],data='0x'+f'{amount:064x}')


class LiveAttributionTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:',Config(),'robinhood');self.db=self.store.db;schema(self.db)
        with self.db:
            self.db.execute("INSERT INTO chain_cursor VALUES(1,4663,1,100,'hash',2)")
            self.db.execute("INSERT INTO launch_decisions(ts,token,pool,action,score,reasons,evidence) VALUES(1000,?,?,'SKIP',0,'[]','{}')",(TOKEN,POOL))
            self.db.execute("INSERT INTO chain_events VALUES('tx',0,100,'hash','swap',?,'{}')",(POOL,))
            self.db.execute("INSERT INTO chain_swaps VALUES('tx',0,?,?,NULL,1000,'buy','100','10',0)",(POOL,TOKEN))
        self.receipt=dict(transactionHash='tx',blockHash='hash',blockNumber=hex(100),status='0x1',
          **{'from':WALLET,'to':V2_ROUTER},logs=[transfer(TOKEN,POOL,WALLET,100),transfer(evm.WETH,WALLET,POOL,10)])
        self.trace=dict(type='CALL',value='0x0',**{'from':WALLET,'to':V2_ROUTER})
        self.rpc=Mock();self.rpc.call.side_effect=lambda method,args:self.receipt if method=='eth_getTransactionReceipt' else self.trace

    def tearDown(self):self.db.close()

    def test_fast_worker_attributes_recent_trades_without_touching_fifo_history(self):
        self.assertEqual(a.fast_tick(self.db,self.rpc,now=1005),1)
        r=self.db.execute('SELECT * FROM live_trade_observations').fetchone()
        self.assertEqual(r['status'],'complete');self.assertEqual(r['wallet'],WALLET);self.assertEqual(r['side'],'buy')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wallet_flows').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wallet_lots').fetchone()[0],0)
        self.assertEqual(a.fast_tick(self.db,self.rpc,now=1006),0)
        self.assertEqual(self.rpc.call.call_count,2)

    def test_mismatched_receipt_is_not_cached_or_counted_as_verified(self):
        self.receipt['blockHash']='different'
        with self.assertRaisesRegex(ValueError,'provenance'):a.fast_tick(self.db,self.rpc,now=1005)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM live_trade_observations').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM chain_receipts').fetchone()[0],0)

    def test_old_history_cannot_occupy_fast_worker(self):
        with self.db:self.db.execute('UPDATE chain_swaps SET ts=1')
        self.assertEqual(a.fast_tick(self.db,self.rpc,now=1005),0)
        self.rpc.call.assert_not_called()

    def test_new_pool_buyers_are_decoded_before_safety_assessment_exists(self):
        with self.db:
            self.db.execute('DELETE FROM launch_decisions')
            self.db.execute("INSERT INTO chain_pools VALUES(?,2,?,?,3000,?,90,995,'creator','create')",(POOL,evm.WETH,TOKEN,evm.ZERO))
        self.assertEqual(a.fast_tick(self.db,self.rpc,now=1005),1)
        self.assertEqual(self.db.execute('SELECT wallet FROM live_trade_observations').fetchone()[0],WALLET)

    def test_old_unassessed_pool_does_not_take_new_launch_capacity(self):
        with self.db:
            self.db.execute('DELETE FROM launch_decisions')
            self.db.execute("INSERT INTO chain_pools VALUES(?,2,?,?,3000,?,1,1,'creator','create')",(POOL,evm.WETH,TOKEN,evm.ZERO))
        self.assertEqual(a.fast_tick(self.db,self.rpc,now=1005),0)
        self.rpc.call.assert_not_called()

    def test_cached_reads_are_reused_for_live_attribution(self):
        with self.db:
            self.db.execute('INSERT INTO chain_receipts VALUES(?,?)',('tx',json.dumps(self.receipt)))
            self.db.execute('INSERT INTO chain_traces VALUES(?,?)',('tx',json.dumps(self.trace)))
        self.assertEqual(a.fast_tick(self.db,self.rpc,now=1005),1)
        self.rpc.call.assert_not_called()


if __name__=='__main__':unittest.main()
