"""Versioned measurement contract shared by storage, scoring, Astra and the UI.

Shape checks and cross-field rules use the same field catalogue as the exported
JSON Schema. Missing, error and undefined observations never acquire numeric zero.
"""
from copy import deepcopy
import math
import re

VERSION = 1
CONTROLS = ('is_honeypot','cannot_buy','cannot_sell_all','is_mintable','is_proxy',
    'is_blacklisted','is_whitelisted','transfer_pausable','slippage_modifiable',
    'personal_slippage_modifiable','hidden_owner','owner_change_balance',
    'can_take_back_ownership','selfdestruct')
# id -> value type, unit, minimum, maximum
SPECS = {
    'market.price':('number','USD/token',0,None),
    'market.liquidity':('number','USD',0,None),
    'quote.quantity':('number','token',0,None),
    'quote.usd_in':('number','USD',0,None),
    'simulation.passed':('boolean','boolean',None,None),
    'simulation.buy_shortfall':('number','fraction',0,1),
    'simulation.sell_shortfall':('number','fraction',0,1),
    'simulation.roundtrip_loss':('number','fraction',None,1),
    'holders.raw_top10':('number','fraction',0,1),
    'holders.adjusted_top10':('number','fraction',0,1),
    'buyers.distinct':('integer','wallets',0,None),
    'buyers.qualified':('integer','wallets',0,None),
    'buyers.total':('integer','transactions',0,None),
    'buyers.reviewed':('integer','transactions',0,None),
    'buyers.pending':('integer','transactions',0,None),
    'buyers.ambiguous':('integer','transactions',0,None),
    'buyers.coverage':('number','fraction',0,1),
    'liquidity.lp_supply':('string','atomic LP tokens',None,None),
    'liquidity.lp_burned':('number','fraction',0,1),
    'source.verified':('boolean','boolean',None,None),
    **{'controls.'+key:('boolean','risk flag',None,None) for key in CONTROLS},
}
STATUSES = ('measured','partial','missing','error','undefined','not_applicable')
ACTIONS = ('none','refresh_market','refresh_holders','complete_buyers','refresh_source','refresh_permissions')
CODES = ('STALE_QUOTE','STALE_HOLDERS','STALE_PERMISSIONS','MISSING_PROVENANCE',
    'INCOMPLETE_BUYER_HISTORY','UNRESOLVED_HOLDER_CLASSIFICATION','UNDEFINED_LP_BURN',
    'MISSING_PERMISSIONS','SOURCE_NOT_VERIFIED','BLOCK_MISMATCH','SCHEMA_INVALID',
    'SIMULATION_SCOPE','SELLABILITY_PROVIDER_UNKNOWN','MISSING_QUOTE','LIQUIDITY_LOCK_UNRESOLVED')
BASE_FIELDS = ('value','status','reason','source','observed_at','block_number','block_hash','unit','scope')


def finite(n):
    return type(n) in (int,float) and math.isfinite(n)


def schema():
    measurements = {}
    for name,(kind,unit,low,high) in SPECS.items():
        value = {'type':[kind,'null']}
        if low is not None:value['minimum']=low
        if high is not None:value['maximum']=high
        if name=='liquidity.lp_supply':value['pattern']='^[0-9]+$'
        measurements[name] = dict(type='object',additionalProperties=False,required=list(BASE_FIELDS),properties={
            'value':value,'status':{'enum':list(STATUSES)},'reason':{'type':['string','null']},
            'source':{'type':['string','null']},'observed_at':{'type':['number','null'],'minimum':0},
            'block_number':{'type':['integer','null'],'minimum':0},
            'block_hash':{'type':['string','null'],'pattern':'^0x[0-9a-fA-F]{64}$'},
            'unit':{'const':unit},'scope':{'type':'string','minLength':1}})
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','title':'Cointrade evidence v1',
        'type':'object','additionalProperties':False,
        'required':['schema_version','chain_id','token','pool','assessed_at','origin','measurements'],
        'properties':{'schema_version':{'const':VERSION},'chain_id':{'const':4663},
            'token':{'type':'string','pattern':'^0x[0-9a-f]{40}$'},
            'pool':{'type':['string','null'],'pattern':'^0x([0-9a-f]{40}|[0-9a-f]{64})$'},
            'assessed_at':{'type':'number','minimum':0},'origin':{'enum':['collector','legacy_adapter']},
            'measurements':{'type':'object','additionalProperties':False,'required':list(SPECS),'properties':measurements}}}


def validate(document):
    """Strict shape, finite values, units, coverage arithmetic and denominator checks."""
    errors=[]
    expected={'schema_version','chain_id','token','pool','assessed_at','origin','measurements'}
    if not isinstance(document,dict) or set(document)!=expected:return ['Invalid evidence document fields']
    if type(document['schema_version']) is not int or document['schema_version']!=VERSION:errors.append('Unsupported schema version')
    if type(document['chain_id']) is not int or document['chain_id']!=4663:errors.append('Unexpected chain')
    if not isinstance(document['token'],str) or not re.fullmatch(r'0x[0-9a-f]{40}',document['token']):errors.append('Invalid token')
    pool=document['pool']
    if pool is not None and (not isinstance(pool,str) or not re.fullmatch(r'0x(?:[0-9a-f]{40}|[0-9a-f]{64})',pool)):errors.append('Invalid pool')
    if not finite(document['assessed_at']) or document['assessed_at']<0:errors.append('Invalid assessment timestamp')
    if document['origin'] not in ('collector','legacy_adapter'):errors.append('Invalid origin')
    ms=document['measurements']
    if not isinstance(ms,dict) or set(ms)!=set(SPECS):return errors+['Invalid measurement catalogue']
    for name,(kind,unit,low,high) in SPECS.items():
        m=ms[name]
        if not isinstance(m,dict) or set(m)!=set(BASE_FIELDS):errors.append(name+': invalid fields');continue
        v=m['value'];status=m['status']
        if status not in STATUSES:errors.append(name+': invalid status')
        if status in ('missing','error','undefined','not_applicable') and v is not None:errors.append(name+': unavailable value must be null')
        if status in ('measured','partial') and v is None:errors.append(name+': measured value required')
        if v is not None:
            valid = finite(v) if kind=='number' else type(v) is int if kind=='integer' else type(v) is bool if kind=='boolean' else isinstance(v,str)
            if not valid:errors.append(name+': invalid value type')
            elif kind in ('number','integer') and ((low is not None and v<low) or (high is not None and v>high)):errors.append(name+': out of range')
            elif name=='liquidity.lp_supply' and not re.fullmatch('[0-9]+',v):errors.append(name+': invalid atomic amount')
        for key in ('source','reason'):
            if m[key] is not None and not isinstance(m[key],str):errors.append(name+': invalid '+key)
        if not isinstance(m['scope'],str) or not m['scope']:errors.append(name+': missing scope')
        if m['unit']!=unit:errors.append(name+': wrong unit')
        ts=m['observed_at'];height=m['block_number'];hash_=m['block_hash']
        if ts is not None and (not finite(ts) or ts<0):errors.append(name+': invalid observation time')
        if height is not None and (type(height) is not int or height<0):errors.append(name+': invalid block number')
        if hash_ is not None and height is None:errors.append(name+': block hash requires block number')
        if hash_ is not None and (not isinstance(hash_,str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}',hash_)):errors.append(name+': invalid block hash')
        if status=='measured' and (not m['source'] or ts is None):errors.append(name+': measured value requires provenance')
        if status not in ('measured',) and not m['reason']:errors.append(name+': non-measured status requires a reason')
    if errors:return errors
    values={k:m['value'] for k,m in ms.items()}
    total,reviewed,pending,ambiguous=(values['buyers.'+k] for k in ('total','reviewed','pending','ambiguous'))
    if all(type(x) is int for x in (total,reviewed,pending,ambiguous)):
        if reviewed+pending!=total or ambiguous>reviewed:errors.append('buyers: inconsistent coverage counts')
        coverage=values['buyers.coverage']
        if total==0 and (coverage is not None or ms['buyers.coverage']['status']!='undefined'):errors.append('buyers: zero denominator must be undefined')
        if total and (coverage is None or abs(coverage-reviewed/total)>1e-9):errors.append('buyers: incorrect coverage fraction')
        if (pending or ambiguous) and any(ms['buyers.'+k]['status']=='measured' for k in ('distinct','qualified')):errors.append('buyers: incomplete counts must be partial')
    if values['liquidity.lp_supply']=='0' and (values['liquidity.lp_burned'] is not None or ms['liquidity.lp_burned']['status']!='undefined'):
        errors.append('liquidity: zero LP supply must have undefined burn fraction')
    if values['holders.adjusted_top10'] is not None and ms['holders.adjusted_top10']['source']!='verified_address_classification':
        errors.append('holders: adjusted concentration requires classification evidence')
    return errors


def build(snapshot,assessed_at,origin='collector'):
    """Normalize existing collector fields without fabricating missing provenance."""
    s=snapshot;m={};obs=s.get('observed_at');market=s.get('market_evidence') or {}
    quote=s.get('execution_quote') or {};sim=s.get('trade_simulation') or {};holder=s.get('holder_evidence') or {}
    buyers=s.get('buyer_evidence') or {};source=s.get('source_verification') or {}
    def add(name,value,provider=None,ts=None,block=None,hash_=None,scope='token',status=None,reason=None):
        if status is None:status='missing' if value is None else 'measured' if provider and ts is not None else 'partial'
        if status!='measured' and not reason:reason='not_observed' if value is None else 'provenance_incomplete'
        m[name]=dict(value=value,status=status,reason=reason,source=provider,observed_at=ts,
            block_number=block,block_hash=hash_,unit=SPECS[name][1],scope=scope)
    def chain_meta(e):
        return dict(provider=e.get('source'),ts=e.get('observed_at'),block=e.get('block'),hash_=e.get('block_hash'))
    market_meta=chain_meta(market)
    # Legacy observations have a reliable timestamp only when tied to the saved quote block.
    if market and market_meta['ts'] is None:market_meta['ts']=obs
    for name,key in [('market.price','price'),('market.liquidity','liquidity')]:add(name,s.get(key),scope='selected pool',**market_meta)
    quote_meta=dict(market_meta,provider=quote.get('source'),block=quote.get('block'))
    for name,key in [('quote.quantity','quantity'),('quote.usd_in','usd_in')]:
        add(name,quote.get(key) if quote.get('status')=='quoted' else None,scope='size-specific pool quote',reason=quote.get('error'),**quote_meta)
    sim_meta=chain_meta(sim)
    if sim and sim_meta['ts'] is None and sim.get('block')==market.get('block'):sim_meta['ts']=obs
    scope=f"account={sim.get('synthetic_account','unknown')}; input={quote.get('usd_in','unknown')} USD; one block only"
    add('simulation.passed',True if sim.get('status')=='passed' else None,scope=scope,
        status='error' if sim.get('status')=='failed' else None,reason=sim.get('error'),**sim_meta)
    for name,key in [('simulation.buy_shortfall','buy_transfer_shortfall'),('simulation.sell_shortfall','sell_transfer_shortfall'),('simulation.roundtrip_loss','roundtrip_loss_fraction')]:
        add(name,sim.get(key) if sim.get('status')=='passed' else None,scope=scope,**sim_meta)
    add('holders.raw_top10',s.get('top10_share'),scope='raw addresses; includes pool and custody balances',**chain_meta(holder))
    add('holders.adjusted_top10',None,scope='requires documented address classification and denominator',reason='address_classification_unresolved')
    bmeta=chain_meta(buyers)
    if buyers and bmeta['ts'] is None and buyers.get('block')==market.get('block'):bmeta['ts']=obs
    bmeta['provider']='receipt_and_call_trace' if buyers else None
    partial=buyers.get('pending',0)>0 or buyers.get('ambiguous',0)>0
    for name,key in [('buyers.distinct','distinct_buyers'),('buyers.qualified','qualified_wallet_buys')]:
        add(name,s.get(key),status='partial' if partial and s.get(key) is not None else None,
            reason='pending_or_ambiguous_transactions' if partial else None,scope='verified lower bound in indexed pools, last 300 seconds',**bmeta)
    for name,key in [('buyers.total','transactions'),('buyers.reviewed','reviewed'),('buyers.pending','pending'),('buyers.ambiguous','ambiguous')]:
        add(name,buyers.get(key),scope='indexed pools, last 300 seconds',**bmeta)
    total=buyers.get('transactions');reviewed=buyers.get('reviewed')
    coverage=reviewed/total if type(total) is int and total>0 and type(reviewed) is int else None
    add('buyers.coverage',coverage,status='undefined' if total==0 else None,reason='no_observed_transactions' if total==0 else None,scope='reviewed / observed transactions',**bmeta)
    version=s.get('pool_version')
    if version is None:
        provider=market.get('source') or ''
        version=3 if provider.startswith('v3_') else 4 if provider.startswith('v4_') else 2 if provider=='v2_reserves' else None
    not_v2=version in (3,4)
    supply=None if not_v2 else market.get('lp_total_supply_atomic')
    add('liquidity.lp_supply',supply,status='not_applicable' if not_v2 else None,reason='not_a_v2_pool' if not_v2 else None,scope='V2 LP total supply',**market_meta)
    zero=supply=='0'
    add('liquidity.lp_burned',None if zero or not_v2 else market.get('lp_burned_fraction'),
        status='not_applicable' if not_v2 else 'undefined' if zero else None,reason='not_a_v2_pool' if not_v2 else 'zero_lp_supply' if zero else None,scope='burned V2 LP / issued V2 LP; not a time-lock proof',**market_meta)
    add('source.verified',{'verified':True,'unverified':False}.get(source.get('status')),
        provider=source.get('provider'),ts=source.get('checked_at'),block=source.get('block'),hash_=source.get('block_hash'),
        scope='published source match; does not establish permission safety')
    for flag in CONTROLS:
        val={'pass':False,'fail':True}.get((s.get('risk_checks') or {}).get(flag))
        add('controls.'+flag,val,provider='goplus',ts=s.get('risk_checked_at'),scope='provider risk flag; False=reported absent, True=reported present')
    return dict(schema_version=VERSION,chain_id=4663,token=s.get('token'),pool=s.get('pool'),
        assessed_at=assessed_at,origin=origin,measurements=m)


def quality(doc,now,max_age=120):
    errors=validate(doc);findings=[]
    def add(code,ids,detail,follow_up='none',severity='warning'):
        findings.append(dict(code=code,evidence_ids=ids,detail=detail,follow_up=follow_up,severity=severity))
    if errors:
        add('SCHEMA_INVALID',[], '; '.join(errors[:8]),severity='blocker')
        return dict(valid=False,errors=errors,findings=findings,freshness={})
    ms=doc['measurements'];freshness={}
    for name,m in ms.items():
        ts=m['observed_at'];limit=3600 if name=='source.verified' else max_age
        freshness[name]='unknown' if ts is None else 'fresh' if -15<=now-ts<=limit else 'stale'
    for name,code,action in [('quote.quantity','STALE_QUOTE','refresh_market'),('holders.raw_top10','STALE_HOLDERS','refresh_holders')]:
        if freshness[name]=='stale':add(code,[name],'Observation exceeds its age limit.',action,'blocker')
        elif ms[name]['value'] is not None and freshness[name]=='unknown':add('MISSING_PROVENANCE',[name],'Observation time is unknown.',action,'blocker')
    if ms['quote.quantity']['value'] is None:add('MISSING_QUOTE',['quote.quantity'],'No executable size-specific quote.','refresh_market','blocker')
    for prefix,action in [('market.','refresh_market'),('simulation.','refresh_market'),('buyers.','complete_buyers')]:
        stale=[k for k in ms if k.startswith(prefix) and ms[k]['value'] is not None and freshness[k]!='fresh']
        if stale:add('MISSING_PROVENANCE' if any(freshness[k]=='unknown' for k in stale) else 'STALE_QUOTE',stale,'These observations are not fresh.',action,'blocker')
    absent_source=[k for k,m in ms.items() if m['value'] is not None and not m['source']]
    if absent_source:add('MISSING_PROVENANCE',absent_source,'Measurement source is unknown.',severity='blocker')
    if ms['liquidity.lp_burned']['value'] is not None and freshness['liquidity.lp_burned']!='fresh':
        add('MISSING_PROVENANCE',['liquidity.lp_burned'],'Liquidity protection evidence is not fresh.','refresh_market','blocker')
    controls=['controls.'+x for x in CONTROLS[3:]]
    missing=[k for k in controls if ms[k]['value'] is None]
    if missing:add('MISSING_PERMISSIONS',missing,'Administrative permissions are unresolved.','refresh_permissions','blocker')
    stale=[k for k in controls if ms[k]['value'] is not None and freshness[k]!='fresh']
    if stale:add('STALE_PERMISSIONS',stale,'Permission observations need refreshing.','refresh_permissions','blocker')
    if ms['source.verified']['value'] is not True or freshness['source.verified']!='fresh':add('SOURCE_NOT_VERIFIED',['source.verified'],'A current source match is not established.','refresh_source','blocker')
    pending=ms['buyers.pending']['value'];ambiguous=ms['buyers.ambiguous']['value']
    if pending or ambiguous:add('INCOMPLETE_BUYER_HISTORY',['buyers.distinct','buyers.qualified','buyers.coverage'],'Buyer counts are lower bounds; missing attribution cannot establish absence.','complete_buyers')
    if ms['holders.adjusted_top10']['value'] is None:add('UNRESOLVED_HOLDER_CLASSIFICATION',['holders.raw_top10','holders.adjusted_top10'],'Raw concentration cannot establish beneficial-owner concentration.')
    if ms['liquidity.lp_burned']['status']=='undefined':add('UNDEFINED_LP_BURN',['liquidity.lp_supply','liquidity.lp_burned'],'No issued LP tokens: burned percentage is undefined.')
    if ms['liquidity.lp_burned']['status']=='not_applicable':
        add('LIQUIDITY_LOCK_UNRESOLVED',['liquidity.lp_supply','liquidity.lp_burned'],'V2 LP burning does not apply to this pool. Position ownership and withdrawal protection remain unresolved.',severity='blocker')
    reference=ms['quote.quantity']['block_number']
    mismatched=[k for k in ('holders.raw_top10','simulation.passed','buyers.distinct') if reference is not None and ms[k]['block_number'] is not None and ms[k]['block_number']!=reference]
    if mismatched:add('BLOCK_MISMATCH',['quote.quantity']+mismatched,'Measurements refer to different blocks; each retains its own timestamp.','refresh_holders')
    if ms['simulation.passed']['value'] is True:
        add('SIMULATION_SCOPE',['simulation.passed'],'Successful simulation is limited to its recorded account, amount and block.',severity='info')
        if ms['controls.cannot_sell_all']['value'] is None:add('SELLABILITY_PROVIDER_UNKNOWN',['controls.cannot_sell_all','simulation.passed'],'Provider classification is unresolved; the scoped simulation passed.',severity='info')
    return dict(valid=True,errors=[],findings=findings,freshness=freshness)


def effective(snapshot,now,max_age=120):
    """Fail closed when a new contract is invalid; stale values earn no score."""
    doc=snapshot.get('evidence_contract')
    if doc is None:return snapshot
    s=deepcopy(snapshot);q=quality(doc,now,max_age);s['_evidence_quality']=q
    if not q['valid']:
        s.update(price=None,liquidity=None,top10_share=None,distinct_buyers=None,qualified_wallet_buys=None,
            risk_checks={},execution_quote=None,trade_simulation=None,source_verification={},market_evidence={})
        return s
    ms=doc['measurements']
    def value(key):return ms[key]['value'] if q['freshness'][key]=='fresh' and ms[key]['source'] else None
    s.update(price=value('market.price'),liquidity=value('market.liquidity'),top10_share=value('holders.raw_top10'),
        distinct_buyers=value('buyers.distinct'),qualified_wallet_buys=value('buyers.qualified'))
    s['source_verification']=dict(s.get('source_verification') or {},status='verified' if value('source.verified') is True else 'unverified' if value('source.verified') is False else 'unavailable')
    s['risk_checks']={flag:'pass' if value('controls.'+flag) is False else 'fail' if value('controls.'+flag) is True else 'unknown' for flag in CONTROLS}
    if value('quote.quantity') is None:s['execution_quote']=None
    elif s.get('execution_quote'):
        s['execution_quote'].update(quantity=value('quote.quantity'),usd_in=value('quote.usd_in'))
    if value('simulation.passed') is not True:s['trade_simulation']=None
    s['market_evidence']=dict(s.get('market_evidence') or {},lp_burned_fraction=value('liquidity.lp_burned'))
    return s
