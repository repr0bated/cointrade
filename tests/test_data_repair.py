import unittest
from cointrade import attribution,evm,simulation,quotes

class DataRepairTests(unittest.TestCase):
 def test_trace_reverts_and_delegate_values_ignored(self):
  w='0x'+'1'*40;o='0x'+'2'*40
  t={'type':'CALL','from':w,'to':o,'value':'0xa','calls':[
    {'type':'CALL','from':o,'to':w,'value':'0x3'},
    {'type':'DELEGATECALL','from':w,'to':o,'value':'0xa'},
    {'type':'CALL','from':o,'to':w,'value':'0x64','error':'revert','calls':[{'type':'CALL','to':w,'value':'0x64'}]}]}
  self.assertEqual(attribution.native_delta(t,w),-7)
 def test_native_buy_and_gas_separation(self):
  w='0x'+'1'*40;o='0x'+'2'*40;token='0x'+'3'*40
  log={'address':token,'topics':[evm.TRANSFER,'0x'+quotes.word(o),'0x'+quotes.word(w)],'data':'0x'+quotes.word(100)}
  r={'from':w,'to':o,'status':'0x1','logs':[log]}
  t={'type':'CALL','from':w,'to':o,'value':'0xa'}
  self.assertEqual(attribution.flow(r,t),(w,token,100,-10))
  r['status']='0x0'
  with self.assertRaises(ValueError):attribution.flow(r,t)
 def test_contract_creator_not_confused_with_origin(self):
  from cointrade.contracts import deployment_origin
  token='0x'+'3'*40
  trace={'type':'CALL','from':'wallet','to':'factory','calls':[{'type':'CREATE2','from':'factory','to':token}]}
  result=deployment_origin(trace,token)
  self.assertEqual(result['deployer'],'factory')
  self.assertEqual(result['initiator'],'wallet')
  trace['calls'][0]['error']='reverted'
  self.assertIsNone(deployment_origin(trace,token))

 def test_v4_abi_offsets(self):
  p=dict(version=4,token0=evm.ZERO,token1='0x'+'1'*40,fee=3000,tick_spacing=60,hooks=evm.ZERO)
  _,data=quotes.quote_calldata(p,True,10)
  words=evm.words('0x'+data[10:])
  self.assertEqual(words[0],32)
  self.assertEqual(words[-2:], [256,0])
  swap=simulation.swap(p,evm.ZERO,p['token1'],10)
  self.assertEqual(swap['value'],'0xa')
 def test_simulation_revert_fails_closed(self):
  class RPC:
   def call(self,*args):return [{'calls':[{'status':'0x0','error':{'code':3}}]}]
  with self.assertRaisesRegex(ValueError,'reverted'):
   simulation.simulate(RPC(),[simulation.call(evm.WETH,'0x')],1)

class QuoteAccountingTests(unittest.TestCase):
 def test_pool_fees_not_charged_twice(self):
  import tempfile
  from cointrade.config import Config
  from cointrade.store import Store
  from cointrade.engine import Engine
  with tempfile.TemporaryDirectory() as d:
   c=Config();store=Store(d+'/test.sqlite',c,'robinhood')
   try:
    engine=Engine(store,c,lambda s,n,c:(100,[]))
    s=dict(token='token',price=.2,observed_at=1000,requires_execution_quote=True,
           execution_quote=dict(status='quoted',quantity=10,usd_in=1.995))
    engine.tick([s],1000)
    self.assertAlmostEqual(store.positions()[0]['quantity'],9.9)
    self.assertAlmostEqual(store.db.execute('SELECT fee FROM trades').fetchone()[0],.005)
    s.update(price=1,observed_at=1001,position_quote=dict(status='quoted',usd_out=3))
    engine.tick([s],1001)
    self.assertAlmostEqual(store.account()['cash'],40.965)
   finally:store.db.close()

class AggregationTests(unittest.TestCase):
 def test_aggregate_calls_keep_failures_and_decode_dynamic_offsets(self):
  from unittest.mock import patch
  from cointrade.simulation import pack,Dynamic,blob
  from cointrade.quotes import word
  value=pack([Dynamic(word(2)+pack([Dynamic(pack([word(1),blob(word(123))])),Dynamic(pack([word(0),blob('')]))]))])
  r=evm.RPC();r._multicall_verified=True
  with patch.object(r,'read',return_value='0x'+value):
   self.assertEqual(r.reads([(evm.WETH,'0x18160ddd')]*2,1),['0x'+word(123),None])
  with patch.object(r,'read',return_value='0x'+word(32)+word(2)):
   with self.assertRaisesRegex(ValueError,'Malformed'):r.reads([(evm.WETH,'0x18160ddd')]*2,1)
 def test_aggregate_rejects_different_runtime(self):
  from unittest.mock import patch
  r=evm.RPC()
  with patch.object(r,'call',return_value='0x6000'):
   with self.assertRaisesRegex(ValueError,'runtime'):r.reads([(evm.WETH,'0x18160ddd')],1)

class ContractSourceTests(unittest.TestCase):
 def test_explorer_verification_requires_matching_runtime(self):
  from unittest.mock import patch
  from cointrade import contracts
  class RPC:
   def call(self,*args):return '0x6000'
  data=dict(is_verified=True,source_code='contract T {}',deployed_bytecode='0x6001',abi=[])
  with patch('cointrade.blockscout.key',return_value='test'),patch('cointrade.blockscout.get',return_value=data),patch('cointrade.contracts.request_json',side_effect=ValueError('Provider HTTP 404')):
   self.assertNotEqual(contracts.verify_source(RPC(),'0x1',1)['status'],'verified')
  data['deployed_bytecode']='0x6000'
  with patch('cointrade.blockscout.key',return_value='test'),patch('cointrade.blockscout.get',return_value=data):
   result=contracts.verify_source(RPC(),'0x1',1)
   self.assertEqual(result['status'],'verified')
   self.assertEqual(result['provider'],'blockscout')
