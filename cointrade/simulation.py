"""Read-only EVM round-trip simulation with a funded synthetic account; never signs."""
from . import evm
from .quotes import word,poolkey,quote_calldata

CALLER='0x1000000000000000000000000000000000000001'
V2_ROUTER='0x89e5db8b5aa49aa85ac63f691524311aeb649eba'
V3_ROUTER='0xcaf681a66d020601342297493863e78c959e5cb2'
V4_ROUTER='0x204faca1764b154221e35c0d20abb3c525710498'
PERMIT2='0x000000000022d473030f116ddee9f6b43ac78ba3'
MAX=2**256-1


class Dynamic(str):pass


def pack(parts):
    offset=sum(64 if isinstance(p,Dynamic) else len(p) for p in parts)//2
    head=[];tail=[]
    for p in parts:
        if isinstance(p,Dynamic):
            head.append(word(offset));tail.append(p);offset+=len(p)//2
        else:head.append(p)
    return ''.join(head+tail)


def blob(raw):
    return Dynamic(word(len(raw)//2)+raw.ljust((len(raw)+63)//64*64,'0'))


def array_blobs(values):return Dynamic(word(len(values))+pack([blob(v) for v in values]))


def call(to,data,value=0):
    return dict(from_=CALLER,to=to,data=data,value=hex(value),gas='0x989680',gasPrice='0x0')


def approval(token,spender):return call(token,'0x095ea7b3'+word(spender)+word(MAX))


def swap(p,asset_in,asset_out,amount):
    if p['version']==2:
        path=Dynamic(word(2)+word(asset_in)+word(asset_out))
        return call(V2_ROUTER,'0x38ed1739'+pack([word(amount),word(0),path,word(CALLER),word(2**64-1)]))
    if p['version']==3:
        return call(V3_ROUTER,'0x04e45aaf'+''.join(word(x) for x in (asset_in,asset_out,p['fee'],CALLER,amount,0,0)))
    z=asset_in==p['token0']
    single=pack([Dynamic(pack([poolkey(p),word(int(z)),word(amount),word(0),blob('')]))])
    actions=pack([blob('060c0f'),array_blobs([single,word(asset_in)+word(amount),word(asset_out)+word(0)])])
    data='0x3593564c'+pack([blob('10'),array_blobs([actions]),word(2**64-1)])
    return call(V4_ROUTER,data,amount if asset_in==evm.ZERO else 0)


def approvals(p,token):
    if token==evm.ZERO:return []
    if p['version']!=4:return [approval(token,V2_ROUTER if p['version']==2 else V3_ROUTER)]
    return [approval(token,PERMIT2),call(PERMIT2,'0x87517c45'+''.join(word(x) for x in (token,V4_ROUTER,2**160-1,2**48-1)))]


def simulate(rpc,calls,height):
    for c in calls:c['from']=c.pop('from_')
    result=rpc.call('eth_simulateV1',[{'blockStateCalls':[{'stateOverrides':{CALLER:{'balance':hex(10**22)}},'calls':calls}],
                                     'validation':False,'traceTransfers':True},hex(height)])
    results=result[0]['calls']
    if len(results)!=len(calls):raise ValueError('Simulation call count mismatch')
    for i,r in enumerate(results):
        if r.get('error') or int(r.get('status','0x0'),16)!=1:
            raise ValueError(f'Round-trip simulation reverted at call {i+1}')
    return results


def roundtrip(rpc,p,quote,height):
    qi=0 if p['token0'] in (evm.ZERO,evm.WETH) else 1
    currency=p['token0'] if qi==0 else p['token1'];token=p['token1'] if qi==0 else p['token0']
    amount=int(quote['quote_in_atomic']);expected=int(quote['token_out_atomic'])
    def buy_calls():
        calls=[]
        if currency==evm.WETH:calls.append(call(evm.WETH,'0xd0e30db0',amount))
        return calls+approvals(p,currency)+[swap(p,currency,token,amount)]
    first=simulate(rpc,buy_calls(),height)
    buy_logs=first[-1].get('logs',[])
    received=evm.transfers(buy_logs,token,CALLER)
    if received<=0:raise ValueError('Simulation buy delivered no tokens')
    if p['version']==2:
        quote_call=call(V2_ROUTER,'0xd06ca61f'+pack([word(received),Dynamic(word(2)+word(token)+word(currency))]))
    else:
        target,data=quote_calldata(p,qi!=0,received)
        quote_call=call(target,data)
    buys=buy_calls()
    results=simulate(rpc,buys+[quote_call]+approvals(p,token)+[swap(p,token,currency,received)],height)
    quoted=evm.words(results[len(buys)]['returnData'])
    expected_return=quoted[3] if p['version']==2 else quoted[0]
    if expected_return<=0:raise ValueError('Simulation sell quote was zero')
    sell_logs=results[-1].get('logs',[])
    sold=-evm.transfers(sell_logs,token,CALLER)
    if sold!=received:raise ValueError('Simulation did not sell the entire acquired balance')
    # ETH transfer logs use the conventional synthetic address with traceTransfers enabled.
    if currency==evm.WETH:
        returned=evm.transfers(sell_logs,evm.WETH,CALLER)
    else:
        returned=evm.transfers(sell_logs,'0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',CALLER)
    if returned<=0:raise ValueError('Simulation sell delivered no quote currency')
    return dict(status='passed',block=height,source='eth_simulateV1',synthetic_account=CALLER,
                token_received_atomic=str(received),quote_returned_atomic=str(returned),
                buy_transfer_shortfall=max(0,1-received/expected),
                sell_transfer_shortfall=max(0,1-returned/expected_return),
                roundtrip_loss_fraction=1-returned/amount,
                limitation='One synthetic account at one block; cannot prove future permissions or liquidity safety')
