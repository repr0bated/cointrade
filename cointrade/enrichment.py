"""On-chain evidence with explicit provenance, never inferred safety guarantees."""
from collections import defaultdict
from decimal import Decimal, localcontext
from . import evm


def holders(rpc, token, deployed, height, include_balances=False):
    logs = rpc.logs(deployed, height, [evm.TRANSFER], [token])
    balances = defaultdict(int)
    for log in logs:
        if log.get('removed'):
            raise ValueError('Removed holder log')
        if len(log['topics']) != 3:
            continue
        value = evm.words(log['data'])
        if len(value) != 1:
            continue
        sender, recipient = evm.address(log['topics'][1]), evm.address(log['topics'][2])
        if sender != evm.ZERO:
            balances[sender] -= value[0]
        if recipient != evm.ZERO:
            balances[recipient] += value[0]
    supply = evm.words(rpc.read(token, '0x18160ddd', height))[0]
    if supply <= 0 or any(v < 0 for v in balances.values()) or sum(balances.values()) != supply:
        raise ValueError('Transfer history does not reconcile with totalSupply')
    ordered = sorted(((a,b) for a,b in balances.items() if b > 0),key=lambda x:x[1],reverse=True)
    # Validate all reconstructed holders, including nonstandard/rebasing tokens.
    calls = [(token,'0x70a08231'+a[2:].zfill(64)) for a,_ in ordered]
    values = rpc.reads(calls,height)
    if len(values)!=len(ordered) or any(v is None for v in values):
        raise ValueError('A holder balanceOf call failed')
    if any(evm.words(raw) != [balance] for raw,(_,balance) in zip(values,ordered)):
        raise ValueError('Transfer ledger does not match balanceOf')
    result = dict(holder_count=len(ordered), top10_share=sum(b for _,b in ordered[:10])/supply,
                source='verified_transfer_ledger', block=height, total_supply=str(supply))
    if include_balances:result['verified_balances']=ordered
    return result


def v2_market(rpc,pool,decimals,height,eth_usd):
    reserve = evm.words(rpc.read(pool['pool'],'0x0902f1ac',height))
    qi = 0 if pool['token0'] == evm.WETH else 1
    q,t = reserve[qi],reserve[1-qi]
    with localcontext() as ctx:
        ctx.prec = 80
        liquidity = Decimal(q)*Decimal(eth_usd)*2/Decimal(10**18)
        price = Decimal(q)*Decimal(eth_usd)*Decimal(10**decimals)/(Decimal(t)*Decimal(10**18)) if t and q else None
    supply = evm.words(rpc.read(pool['pool'],'0x18160ddd',height))[0]
    burned = sum(evm.words(rpc.read(pool['pool'],'0x70a08231'+a[2:].zfill(64),height))[0]
                 for a in (evm.ZERO,'0x000000000000000000000000000000000000dead'))
    return dict(price=float(price) if price is not None else None,liquidity=float(liquidity),
                lp_burned_fraction=burned/supply if supply else None,
                lp_total_supply_atomic=str(supply),lp_burned_atomic=str(burned),
                lp_burn_status='measured' if supply else 'undefined',
                lp_burn_reason=None if supply else 'zero_lp_supply',
                source='v2_reserves',block=height)


def concentrated_spot(sqrt_price,quote_index,decimals,eth_usd):
    if sqrt_price <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec=80
        ratio=Decimal(sqrt_price)**2/Decimal(2**192)
        quote_per_token=(1/ratio if quote_index == 0 else ratio)*Decimal(10**decimals)/Decimal(10**18)
        return float(quote_per_token*Decimal(eth_usd))


def erc20_flow(receipt,token):
    """Wallet net flows can span multiple pools; requires both asset legs at tx origin."""
    wallet=receipt['from'].lower()
    tokens={l['address'].lower() for l in receipt['logs'] if len(l['topics'])==3 and l['topics'][0]==evm.TRANSFER}
    net={t:evm.transfers(receipt['logs'],t,wallet) for t in tokens}
    net={t:v for t,v in net.items() if v}
    if set(net) != {token,evm.WETH} or net[token]*net[evm.WETH] >= 0:
        return None
    # LP actions and unrelated token flows cannot be treated as swap P&L.
    if any(l['topics'] and l['topics'][0] in evm.ACTIVITY[len(evm.SWAPS):] for l in receipt['logs']):
        return None
    return net[token],net[evm.WETH]
