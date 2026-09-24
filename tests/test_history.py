import unittest
from decimal import Decimal
import json
import sqlite3
from cointrade.history import fifo, explorer_crosscheck, resolve_predeployment_balances


class WalletHistoryTests(unittest.TestCase):
    def test_predeployment_zero_requires_code_absence(self):
        class RPC:
            def batch(self,method,calls):
                self.calls=calls
                return ['0x','0x6000']
        rpc=RPC()
        result=resolve_predeployment_balances(rpc,{'new':None,'reverting':None,'existing':7},9)
        self.assertEqual(result,{'new':0,'reverting':None,'existing':7})
        self.assertEqual(rpc.calls,[['new','0x9'],['reverting','0x9']])

    def test_opening_inventory_does_not_become_profit(self):
        events=[dict(tx='sell',token='t',quantity=-100,proceeds=1000)]
        sales,lots=fifo(events,{'t':100})
        self.assertIsNone(sales[0]['pnl'])
        self.assertEqual(sales[0]['unknown_quantity'],100)

    def test_transfers_reduce_inventory_without_realizing_profit(self):
        events=[dict(tx='buy',token='t',quantity=100,cost=210),
                dict(tx='send',token='t',quantity=-40),
                dict(tx='sell',token='t',quantity=-60,proceeds=180)]
        sales,lots=fifo(events,{})
        self.assertEqual(len(sales),1)
        self.assertEqual(Decimal(sales[0]['cost']),126)
        self.assertEqual(Decimal(sales[0]['pnl']),54)
        self.assertFalse(lots['t'])

    def test_mixed_unknown_and_known_sale_is_excluded(self):
        events=[dict(tx='receive',token='t',quantity=20),
                dict(tx='buy',token='t',quantity=100,cost=200),
                dict(tx='sell',token='t',quantity=-40,proceeds=100),
                dict(tx='sell2',token='t',quantity=-80,proceeds=200)]
        sales,lots=fifo(events,{})
        self.assertIsNone(sales[0]['pnl'])
        self.assertEqual(Decimal(sales[1]['pnl']),40)

    def test_explorer_crosscheck_detects_missing_and_conflicting_receipts(self):
        wallet='0xwallet'
        job=dict(id=1,start_block=10,end_block=20)
        receipt=dict(from_=wallet,to='0xrouter',blockHash='0xblock',blockNumber='0xf',
                     gasUsed='0xa',effectiveGasPrice='0x2',status='0x1')
        receipt['from']=receipt.pop('from_')
        item=dict(hash='0xtx',blockNumber='15',blockHash='0xblock',gasUsed='10',gasPrice='2',
                  isError='0',txreceipt_status='1',to='0xrouter')
        item['from']=wallet
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        self.addCleanup(db.close)
        db.executescript('CREATE TABLE wallet_history_events(job,wallet,tx); CREATE TABLE chain_receipts(tx,raw);')
        db.execute('INSERT INTO wallet_history_events VALUES(?,?,?)',(1,wallet,'0xtx'))
        db.execute('INSERT INTO chain_receipts VALUES(?,?)',('0xtx',json.dumps(receipt)))
        result=explorer_crosscheck(db,job,wallet,[item])
        self.assertEqual(result['status'],'matched')
        self.assertEqual(result['receipt_matches'],1)
        self.assertEqual(Decimal(result['explorer_reported_outgoing_gas_fees_eth']),Decimal(20)/10**18)
        self.assertEqual(explorer_crosscheck(db,job,wallet,[])['missing_external_transactions'],['0xtx'])
        item['gasPrice']='3'
        self.assertEqual(explorer_crosscheck(db,job,wallet,[item])['receipt_mismatches'],['0xtx'])
        with self.assertRaisesRegex(ValueError,'duplicate'):
            explorer_crosscheck(db,job,wallet,[item,item])
        item['blockNumber']='21'
        with self.assertRaisesRegex(ValueError,'outside'):
            explorer_crosscheck(db,job,wallet,[item])
