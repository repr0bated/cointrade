"""Canonical Uniswap state and size-specific pool quotes; not a token safety audit."""
from decimal import Decimal
from . import evm
from .enrichment import concentrated_spot, v2_market

V3_QUOTER='0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7'
V4_QUOTER='0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
STATE='0xf3334192d15450cdd385c8b70e03f9a6bd9e673b'
LENS='0x0000001b173c3bbf3984d417d8614e3eed34865b'


def word(n):
    return f'{(int(n,16) if isinstance(n,str) else n) % (2**256):064x}'


def poolkey(p):
    return ''.join(word(p[k]) for k in ('token0','token1','fee','tick_spacing','hooks'))


def quote_calldata(p,zero_for_one,amount):
    if not 0 < amount < 2**128:raise ValueError('Quote amount outside supported range')
    if p['version']==4:
        data='0xaa9d21cb'+word(32)+poolkey(p)+word(int(zero_for_one))+word(amount)+word(256)+word(0)
        target=V4_QUOTER
    elif p['version']==3:
        data='0xc6a5026a'+word(p['token0'] if zero_for_one else p['token1'])+word(p['token1'] if zero_for_one else p['token0'])+word(amount)+word(p['fee'])+word(0)
        target=V3_QUOTER
    else:
        raise ValueError('V2 quotes use reserve math')
    return target,data


def exact_input(rpc,p,zero_for_one,amount,height):
    if p['version']==2:
        r=evm.words(rpc.read(p['pool'],'0x0902f1ac',height))
        i=0 if zero_for_one else 1
        if not r[i] or not r[1-i]:raise ValueError('Pool has no reserves')
        return amount*997*r[1-i]//(r[i]*1000+amount*997)
    target,data=quote_calldata(p,zero_for_one,amount)
    result=evm.words(rpc.read(target,data,height))
    if not result or result[0]<=0:raise ValueError('Pool quote returned zero output')
    return result[0]


def market(rpc,p,decimals,height,eth_usd,usd_size):
    qi=0 if p['token0'] in (evm.ZERO,evm.WETH) else 1
    token=p['token1'] if qi==0 else p['token0']
    if p['version']==2:
        result=v2_market(rpc,p,decimals,height,eth_usd)
    else:
        if p['version']==3:
            slot=evm.words(rpc.read(p['pool'],'0x3850c7bd',height))
            price=concentrated_spot(slot[0],qi,decimals,eth_usd)
            q=evm.words(rpc.read(evm.WETH,'0x70a08231'+word(p['pool']),height))[0]
            t=evm.words(rpc.read(token,'0x70a08231'+word(p['pool']),height))[0]
            liquidity=float(Decimal(q)/10**18*eth_usd+Decimal(t)/10**decimals*Decimal(str(price))) if price else 0
            source='v3_slot0_and_pool_balances'
        else:
            tvl=evm.words(rpc.read(LENS,'0xf95138f2'+word(evm.V4)+poolkey(p),height))
            if len(tvl)!=14:raise ValueError('V4 liquidity snapshot malformed')
            price=concentrated_spot(tvl[6],qi,decimals,eth_usd)
            liquidity=float(Decimal(tvl[qi])/10**18*eth_usd+Decimal(tvl[1-qi])/10**decimals*Decimal(str(price))) if price else 0
            source='v4_reserves_lens_core_tvl'
        result=dict(price=price,liquidity=liquidity,source=source,block=height)
    amount=int(Decimal(str(usd_size))/eth_usd*10**18)
    try:
        bought=exact_input(rpc,p,qi==0,amount,height)
        sold=exact_input(rpc,p,qi!=0,bought,height)
        result['execution_quote']=dict(status='quoted',source='canonical_pool_quote',block=height,
           quote_in_atomic=str(amount),token_out_atomic=str(bought),quote_out_atomic=str(sold),
           quantity=float(Decimal(bought)/10**decimals),usd_in=float(Decimal(amount)/10**18*eth_usd),
           usd_out=float(Decimal(sold)/10**18*eth_usd),fee_included=True,
           limitation='Independent buy and sell pool quotes at the same block; excludes token transfer restrictions and taxes')
    except ValueError as exc:
        result['execution_quote']=dict(status='unavailable',error=str(exc),block=height)
    return result
