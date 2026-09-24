"""Evidence-based triage. Missing provider fields are unknown, never a pass."""
import math

from .providers import request_json
from .evidence import effective
from .evm import ZERO
from .strategy import number, price_valid

FLAGS = ('is_honeypot', 'cannot_buy', 'cannot_sell_all', 'is_mintable', 'is_proxy',
         'is_blacklisted', 'is_whitelisted', 'transfer_pausable', 'slippage_modifiable',
         'personal_slippage_modifiable', 'hidden_owner', 'owner_change_balance',
         'can_take_back_ownership', 'selfdestruct')
CONTROLS=FLAGS[3:]


def candidate_score(s,now,config):
    """Explicit evidence-weighted heuristic, not a calibrated probability or safety verdict."""
    s=effective(s,now,config.max_snapshot_age)
    parts=[]
    def add(name,weight,fraction=None,detail=''):
        parts.append(dict(name=name,maximum=weight,
          points=round(weight*max(0,min(1,fraction)),2) if fraction is not None else 0,
          status='unknown' if fraction is None else 'met' if fraction>=1 else 'partial' if fraction>0 else 'not_met',
          detail=detail))
    source=(s.get('source_verification') or {}).get('status')
    add('Published source',10,1 if source=='verified' else 0 if source=='unverified' else None,
        'Source verification is not a permissions audit.')
    checks=s.get('risk_checks') or {}
    passed=sum(checks.get(k)=='pass' for k in CONTROLS)
    known=sum(checks.get(k) in ('pass','fail') for k in CONTROLS)
    add('Contract controls',20,passed/len(CONTROLS) if known else None,
        f'{passed}/{len(CONTROLS)} checks passed; {len(CONTROLS)-known} unknown.')
    fresh=number(s.get('observed_at')) and -15<=now-s['observed_at']<=config.max_snapshot_age
    quote=s.get('execution_quote') or {};sim=s.get('trade_simulation') or {}
    add('Executable quote',5,float(quote.get('status')=='quoted' and fresh) if quote else None,
        'A size-specific quote at the assessment block; freshness is required.')
    add('Buy and sell simulation',5,float(sim.get('status')=='passed' and fresh) if sim else None,
        'Applies only to the simulated account and block.')
    economics=[sim.get(k) for k in ('buy_transfer_shortfall','sell_transfer_shortfall','roundtrip_loss_fraction')]
    add('Round-trip costs',5,float(fresh and economics[0]<=.03 and economics[1]<=.03 and economics[2]<=.10)
        if sim.get('status')=='passed' and all(number(x) for x in economics) else None,
        'Requires transfer shortfalls at most 3% and round-trip loss at most 10%.')
    liquidity=s.get('liquidity')
    liquidity_fraction=None
    if number(liquidity):
        liquidity_fraction=min(liquidity/config.min_liquidity,1) if config.min_liquidity else float(liquidity>0)
    add('Liquidity',15,liquidity_fraction,f'Full points at the configured ${config.min_liquidity:g} minimum.')
    share=s.get('top10_share')
    distribution=None
    if number(share) and share<=1:
        distribution=min(1,(1-share)/(1-config.max_top10_share)) if config.max_top10_share<1 else 1
    add('Holder distribution',10,distribution,
        'Raw top-ten address share includes pool/custody addresses; attribution is not yet resolved.')
    burned=(s.get('market_evidence') or {}).get('lp_burned_fraction')
    add('Liquidity withdrawal protection',10,float(burned>=.95) if number(burned) and burned<=1 else None,
        'Requires evidence that at least 95% of V2 LP is burned; V3/V4 lock proofs remain unresolved.')
    buyers=s.get('distinct_buyers');qualified=s.get('qualified_wallet_buys')
    add('Verified recent buyers',15,min(buyers/5,1) if number(buyers) else None,
        f"Three points per verified buyer in five minutes, capped at five buyers. {(s.get('buyer_evidence') or {}).get('pending','Unknown number of')} transactions pending attribution.")
    add('Qualified wallet activity',5,min(qualified,1) if number(qualified) else None,
        'Requires a verified recent buy by a wallet meeting the history rules.')
    return dict(version=3,value=round(sum(p['points'] for p in parts),1),components=parts,
      interpretation='Evidence-weighted heuristic; not a probability of profit or a safety approval. Missing evidence earns no points; risk failures still block trades.')


def security(token):
    r = request_json(f'https://api.gopluslabs.io/api/v1/token_security/4663?contract_addresses={token}')
    if r.get('code') != 1:
        raise ValueError('Token security service unavailable')
    result = r.get('result', {}).get(token)
    if not isinstance(result, dict):
        raise ValueError('Token security service has no coverage yet')
    return result


def fraction(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and 0 <= n <= 1 else None
    except (TypeError, ValueError):
        return None


def assess(data, pool, now):
    reasons = []
    source_status = {'1':'verified', '0':'unverified'}.get(data.get('is_open_source'), 'unavailable')
    if source_status == 'unverified':
        reasons.append('source_reported_unverified_by_goplus')
    elif source_status == 'unavailable':
        reasons.append('source_verification_data_unavailable')
    for name in FLAGS:
        if data.get(name) != '0':
            reasons.append(name + ('_flagged' if data.get(name) == '1' else '_unknown'))
    for name in ('buy_tax', 'sell_tax'):
        val = fraction(data.get(name))
        if val is None:
            reasons.append(name + '_data_unavailable')
        elif val > .03:
            reasons.append(name + '_above_limit')
    if pool['hooks'] != ZERO:
        reasons.append('unreviewed_v4_hook')
    dexes = data.get('dex', data.get('dexs', []))
    matched = [d for d in dexes if str(d.get('pair', '')).lower() == pool['pool']]
    liquidity = None
    if matched:
        try:
            liquidity = float(matched[0]['liquidity'])
        except (KeyError, ValueError, TypeError):
            pass
    holders = data.get('holders')
    top10 = None
    if isinstance(holders, list) and holders:
        # Include all reported holders (even pool vaults) for a conservative proxy.
        percentages = [fraction(h.get('percent')) for h in holders]
        if all(x is not None for x in percentages):
            count = data.get('holder_count')
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = None
            if count is not None and len(holders) >= min(count, 10):
                top10 = sum(sorted(percentages, reverse=True)[:10])
    # Token-wide LP reports cannot establish which position in multiple pools is locked.
    locked = 0
    lp = data.get('lp_holders') or []
    if pool['version'] == 2 and len(dexes) == 1 and matched:
        for h in lp:
            pct = fraction(h.get('percent'))
            burned = h.get('address', '').lower() in (ZERO, '0x000000000000000000000000000000000000dead')
            # Burned V2 LP is irrevocable; a lock flag without expiry is insufficient.
            if pct is not None and burned:
                locked += pct
    if locked < .95:
        reasons.append('liquidity_withdrawal_risk_unresolved')
    return dict(reasons=reasons, liquidity=liquidity, top10_share=top10, source_status=source_status,
                checks={k:'pass' if data.get(k)=='0' else 'fail' if data.get(k)=='1' else 'unknown' for k in FLAGS},
                creator=data.get('creator_address'), creator_history='unknown',
                buy_tax=fraction(data.get('buy_tax')), sell_tax=fraction(data.get('sell_tax')),
                checked_at=now, provider='goplus', lp_burned_fraction=locked)


def decide(s, now, config):
    s=effective(s,now,config.max_snapshot_age)
    reasons = list(s.get('risk_reasons', ['risk_unavailable']))
    quality=s.get('_evidence_quality') or {}
    reasons.extend('evidence_'+f['code'].lower() for f in quality.get('findings',[]) if f['severity']=='blocker')
    if not price_valid(s, now, config):
        reasons.append('quote_missing_or_stale')
    if not number(s.get('liquidity')):
        reasons.append('liquidity_data_unavailable')
    elif s['liquidity'] < config.min_liquidity:
        reasons.append('liquidity_below_limit')
    if not number(s.get('top10_share')):
        reasons.append('holder_concentration_data_unavailable')
    elif s['top10_share'] > config.max_top10_share:
        reasons.append('holder_concentration_above_limit')
    if not number(s.get('risk_checked_at')) or not 0 <= now - s['risk_checked_at'] <= 120:
        reasons.append('risk_stale')
    age = s.get('launch_age')
    if not number(age) or age > 86400:
        reasons.append('launch_too_old_or_unknown')
    if s.get('deployment_confirmed') is not True:
        reasons.append('token_deployment_unconfirmed')
    action = 'SKIP'
    if not reasons:
        if (s.get('qualified_wallet_buys') or 0) >= 1:
            action = 'MIRROR'
        elif age <= 300 and (s.get('distinct_buyers') or 0) >= 3:
            action = 'SNIPE'
        else:
            reasons.append('insufficient_launch_or_wallet_signal')
    score = candidate_score(s,now,config)['value']
    if score < config.min_score and not reasons:
        reasons.append('score_below_threshold')
        action = 'SKIP'
    return action, score, reasons


def screen_launch(s, now, config):
    _, score, reasons = decide(s, now, config)
    return score, reasons
