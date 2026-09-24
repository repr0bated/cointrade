"""Isolated, persistent paper experiment. No signing or order-broadcast path.

Training uses scheduled archive observations, not leader-wallet P&L.
Unknown exits have a separate stress value, never a fabricated measured return.
"""
from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
import math
import sqlite3
import statistics
import time

from . import attribution, evm, quotes, simulation
from .providers import request_json
from .launchrisk import decide

RULES = dict(version=1, bankroll=40., position_size=2., network_fee=.005,
             slippage=.01, stop_loss=.08, take_profit=.20, max_hold=900,
             max_positions=5, reserve=10., halt_equity=20., window=60,
             replay_delay=30, replay_entry_deadline=120, replay_exit_grace=120,
             max_entry_premium=.02, max_launch_age=900, min_samples=10,
             min_tokens=5, min_coverage=.8, min_win_rate=.55,
             min_mean_return=.02, min_median_return=0.)
ARMS = ('wallet_momentum', 'wallet_momentum_astra', 'simple_momentum')


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS momentum_run(id INTEGER PRIMARY KEY CHECK(id=1),
        started REAL, rules TEXT, rules_hash TEXT, status TEXT, last_live REAL,
        last_replay REAL, live_error TEXT, replay_error TEXT);
      CREATE TABLE IF NOT EXISTS momentum_accounts(arm TEXT PRIMARY KEY,cash REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS momentum_flows(tx TEXT PRIMARY KEY,wallet TEXT,token TEXT,
        ts REAL,seen_at REAL,side TEXT,quote_eth TEXT,origin TEXT);
      CREATE INDEX IF NOT EXISTS momentum_flow_token ON momentum_flows(token,ts);
      CREATE TABLE IF NOT EXISTS momentum_samples(tx TEXT PRIMARY KEY,wallet TEXT,token TEXT,
        leader_ts REAL,queued_at REAL,status TEXT,entry_id INTEGER,entry_at REAL,
        ended_at REAL,completed_at REAL,return_fraction REAL,reason TEXT,path TEXT);
      CREATE INDEX IF NOT EXISTS momentum_sample_wallet ON momentum_samples(wallet,completed_at);
      CREATE INDEX IF NOT EXISTS momentum_sample_queue ON momentum_samples(status,leader_ts,tx);
      CREATE TABLE IF NOT EXISTS momentum_quotes(pool TEXT,block_hash TEXT,amount TEXT,
        output TEXT,PRIMARY KEY(pool,block_hash,amount));
      CREATE TABLE IF NOT EXISTS momentum_observations(assessment INTEGER PRIMARY KEY,evidence TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS momentum_decisions(id INTEGER PRIMARY KEY,assessment INTEGER,
        arm TEXT,token TEXT,ts REAL,action TEXT,reasons TEXT,signal TEXT,
        UNIQUE(assessment,arm));
      CREATE TABLE IF NOT EXISTS momentum_positions(id INTEGER PRIMARY KEY,arm TEXT,token TEXT,
        pool TEXT,quantity_atomic TEXT,opened_at REAL,cost REAL,mark REAL,marked_at REAL,
        quote_status TEXT,assessment INTEGER,UNIQUE(arm,token));
      CREATE TABLE IF NOT EXISTS momentum_trades(id INTEGER PRIMARY KEY,arm TEXT,token TEXT,
        ts REAL,side TEXT,quantity_atomic TEXT,cash_flow REAL,pnl REAL,reason TEXT,provenance TEXT);
      CREATE INDEX IF NOT EXISTS momentum_decisions_time ON momentum_decisions(ts DESC);
      CREATE INDEX IF NOT EXISTS momentum_flows_time ON momentum_flows(ts);
    ''')


def start(db, config, now=None):
    schema(db)
    now = time.time() if now is None else now
    rules = encoded(dict(RULES, safety_limits=asdict(config)))
    old = db.execute('SELECT rules FROM momentum_run WHERE id=1').fetchone()
    if old and old[0] != rules:
        raise ValueError('Momentum rules differ from the frozen experiment')
    with db:
        db.execute("INSERT OR IGNORE INTO momentum_run VALUES(1,?,?,?,'running',NULL,NULL,NULL,NULL)",
                   (now, rules, hashlib.sha256(rules.encode()).hexdigest()))
        for arm in ARMS:
            db.execute('INSERT OR IGNORE INTO momentum_accounts VALUES(?,?)', (arm, RULES['bankroll']))
    from . import archive_replay
    archive_replay.initialize(db,now)


def seed_history(db, now):
    """First observed buy per wallet/token, regardless of subsequent success.

    Only matured buys are enrolled; late attribution remains explicitly late.
    Missing entry observations remain in the denominator of coverage.
    """
    with db:
        db.execute('''INSERT OR IGNORE INTO momentum_samples
          (tx,wallet,token,leader_ts,queued_at,status,replay_version)
          SELECT q.tx,q.wallet,q.token,q.ts,?,'queued',2 FROM (
            SELECT f.*,ROW_NUMBER() OVER(PARTITION BY wallet,token ORDER BY ts,tx) rn
            FROM (SELECT tx,wallet,token,ts FROM wallet_flows WHERE side='buy'
              UNION SELECT tx,wallet,token,ts FROM momentum_flows WHERE side='buy') f
            WHERE ts<?) q
          WHERE rn=1 AND NOT EXISTS(SELECT 1 FROM momentum_samples m
            WHERE m.wallet=q.wallet AND m.token=q.token AND m.tx!=q.tx)''',
                   (now, now-RULES['max_hold']-RULES['replay_entry_deadline']-RULES['replay_exit_grace']))


def entry_amount(s):
    q, sim = s.get('execution_quote') or {}, s.get('trade_simulation') or {}
    if q.get('status') != 'quoted' or sim.get('status') != 'passed':
        raise ValueError('entry_quote_or_simulation_missing')
    if abs(float(q['usd_in'])-(RULES['position_size']-RULES['network_fee'])) > .000001:
        raise ValueError('entry_size_mismatch')
    if any(not math.isfinite(float(sim[k])) or not 0 <= sim[k] <= cap for k, cap in (
            ('buy_transfer_shortfall', .03), ('sell_transfer_shortfall', .03),
            ('roundtrip_loss_fraction', .10))):
        raise ValueError('entry_simulation_costs_failed')
    amount = int(Decimal(sim['token_received_atomic'])*(1-Decimal(str(RULES['slippage']))))
    if amount <= 0:
        raise ValueError('entry_quantity_zero')
    return amount


def pool_for(db, pool):
    row = db.execute('SELECT * FROM chain_pools WHERE pool=?', (pool,)).fetchone()
    if not row:
        raise ValueError('pool_unavailable')
    p = dict(row)
    if p['version'] == 4:
        rows = db.execute("SELECT raw FROM chain_events WHERE tx=? AND kind='pool_created'", (p['tx'],))
        event = next((json.loads(r[0]) for r in rows if json.loads(r[0])['topics'][1].lower() == pool), None)
        if not event:
            raise ValueError('pool_key_unavailable')
        p['tick_spacing'] = evm.signed(evm.words(event['data'])[1])
    return p


def observation(s, assessed_at, now=None):
    market = s.get('market_evidence') or {}
    block, stamp, block_hash = market.get('block'), market.get('observed_at'), market.get('block_hash')
    if not isinstance(block, int) or not stamp or not block_hash:
        raise ValueError('quote_provenance_missing')
    # Never fill before either the observation block or its recorded availability.
    available = max(assessed_at, stamp)
    if abs(assessed_at-stamp) > 120 or (now is not None and not 0 <= now-available <= 120):
        raise ValueError('quote_stale')
    return block, stamp, block_hash, available






def exit_value(db, rpc, s, amount, assessed_at):
    block, _, block_hash, _ = observation(s, assessed_at)
    sim = s.get('trade_simulation') or {}
    if sim.get('status') != 'passed' or sim.get('block') != block:
        raise ValueError('sell_simulation_missing')
    tax = sim.get('sell_transfer_shortfall')
    if not isinstance(tax, (int, float)) or not math.isfinite(tax) or not 0 <= tax <= .03:
        raise ValueError('sell_transfer_cost_unknown')
    q = s['execution_quote']
    if q.get('block') != block or int(q['quote_in_atomic']) <= 0:
        raise ValueError('quote_block_mismatch')
    rate = Decimal(str(q['usd_in']))/Decimal(q['quote_in_atomic'])
    if not rate.is_finite() or rate <= 0:
        raise ValueError('usd_reference_invalid')
    cached = db.execute('SELECT output FROM momentum_quotes WHERE pool=? AND block_hash=? AND amount=?',
                        (s['pool'], block_hash, str(amount))).fetchone()
    if cached:
        output = int(cached[0])
    else:
        if rpc.block(block)['hash'] != block_hash:
            raise ValueError('archive_block_hash_mismatch')
        p = pool_for(db, s['pool'])
        output = quotes.exact_input(rpc, p, p['token0'] == s['token'], amount, block)
        if rpc.block(block)['hash'] != block_hash:
            raise ValueError('archive_block_hash_mismatch')
        if output <= 0:
            raise ValueError('exit_quote_zero')
        with db:
            db.execute('INSERT OR IGNORE INTO momentum_quotes VALUES(?,?,?,?)',
                       (s['pool'], block_hash, str(amount), str(output)))
    return max(0., float(Decimal(output)*rate)*(1-tax)*(1-RULES['slippage'])-RULES['network_fee'])


def exit_reason(value, cost, elapsed):
    if value <= cost*(1-RULES['stop_loss']):
        return 'stop_loss'
    if value >= cost*(1+RULES['take_profit']):
        return 'take_profit'
    if elapsed >= RULES['max_hold']:
        return 'time_exit'
    return None




def rankings(db, as_of):
    groups = {}
    for r in db.execute('SELECT * FROM momentum_samples WHERE leader_ts<? AND queued_at<?', (as_of, as_of)):
        groups.setdefault(r['wallet'], []).append(r)
    result = []
    for wallet, samples in groups.items():
        usable = [r for r in samples if r['replay_version']==2 and r['completed_at'] and r['completed_at'] < as_of
                  and r['ended_at'] and r['ended_at'] < as_of
                  and (r['return_fraction'] is not None or r['stress_return'] is not None)]
        measured = [r for r in usable if r['status']=='complete' and r['return_fraction'] is not None]
        returns = [r['stress_return'] if r['stress_return'] is not None else r['return_fraction'] for r in usable]
        n, total = len(measured), len(samples)
        mean, median = (statistics.mean(returns), statistics.median(returns)) if n else (None, None)
        tokens = len({r['token'] for r in measured})
        coverage, wins = n/total, sum(x > 0 for x in returns)
        eligible = bool(n >= RULES['min_samples'] and tokens >= RULES['min_tokens']
                        and coverage >= RULES['min_coverage'] and wins/len(returns) >= RULES['min_win_rate']
                        and mean >= RULES['min_mean_return'] and median > RULES['min_median_return'])
        result.append(dict(wallet=wallet, samples=n, observed_buys=total, tokens=tokens,
                           coverage=coverage, mean_return=mean, median_return=median,
                           measured_mean_return=statistics.mean(r['return_fraction'] for r in measured) if measured else None,
                           wins=wins, stressed_exits=sum(r['stress_return'] is not None for r in usable),
                           qualified=eligible))
    return sorted(result, key=lambda r: (r['qualified'], r['samples'], r['mean_return'] if r['mean_return'] is not None else -10), reverse=True)


def capture_flows(db, now):
    rows = db.execute('''SELECT o.*,r.raw receipt,t.raw trace FROM live_trade_observations o
      JOIN chain_receipts r ON r.tx=o.tx JOIN chain_traces t ON t.tx=o.tx
      LEFT JOIN momentum_flows m ON m.tx=o.tx
      WHERE m.tx IS NULL AND o.status='complete' AND o.ts>=? ORDER BY o.ts DESC LIMIT 200''', (now-300,)).fetchall()
    with db:
        for r in rows:
            try:
                receipt = json.loads(r['receipt'])
                wallet, token, quantity, quote = attribution.flow(receipt, json.loads(r['trace']))
                if token != r['token'] or wallet != r['wallet']:
                    continue
                db.execute('INSERT OR IGNORE INTO momentum_flows VALUES(?,?,?,?,?,?,?,?)',
                           (r['tx'], wallet, token, r['ts'], now, 'buy' if quantity > 0 else 'sell',
                            str(abs(Decimal(quote))/10**18), receipt['from'].lower()))
            except (ValueError, KeyError, TypeError):
                continue


def signal(db, s, now, ranked):
    token = s['token']
    events = db.execute('''SELECT w.*,e.raw,e.block,e.block_hash FROM chain_swaps w
      JOIN chain_events e ON e.tx=w.tx AND e.log_index=w.log_index
      WHERE w.token=? AND w.pool=? AND w.ts BETWEEN ? AND ?
      AND e.block<=(SELECT height FROM chain_cursor WHERE id=1)''',
      (token,s['pool'],now-RULES['window'],now)).fetchall()
    expected = {r['tx'] for r in events}
    rows = db.execute('''SELECT * FROM momentum_flows WHERE token=? AND ts BETWEEN ? AND ?
      AND seen_at<=? ORDER BY ts''', (token, now-RULES['window'], now, now)).fetchall()
    rows = [r for r in rows if r['tx'] in expected]
    seen = {r['tx'] for r in rows}
    attributed_net = sum((Decimal(r['quote_eth'])*(1 if r['side']=='buy' else -1) for r in rows), Decimal(0))
    net = Decimal(0); flow_error = None
    try:
        pool=pool_for(db,s['pool'])
        qi=0 if pool['token0'] in (evm.ZERO,evm.WETH) else 1
        for r in events:
            raw=json.loads(r['raw'])
            if evm.pool_key(raw)!=s['pool'] or raw['blockHash']!=r['block_hash']:
                raise ValueError('Pool log provenance mismatch')
            amounts=evm.swap_amounts(raw)
            if amounts[0]*amounts[1]>=0 or str(abs(amounts[qi]))!=r['quote_amount']:
                raise ValueError('Pool log amount mismatch')
            net+=Decimal(amounts[qi])/10**18
    except (ValueError,KeyError,TypeError,IndexError,StopIteration):
        net=None;flow_error='pool_flow_evidence_missing'
    indexed=db.execute('SELECT MAX(timestamp) FROM data_blocks WHERE number<=(SELECT height FROM chain_cursor WHERE id=1)').fetchone()[0]
    creator = db.execute('SELECT * FROM chain_creators WHERE token=?', (token,)).fetchone()
    excluded = {creator[k] for k in ('deployer','initiator')} if creator else set()
    # Deduplicate transaction origins too: two holder addresses behind the same
    # observed sender do not count as independent confirmations.
    buyers, origins = {}, set()
    for r in rows:
        if r['side'] != 'buy' or r['wallet'] in excluded or r['origin'] in excluded or r['origin'] in origins:
            continue
        if r['wallet'] not in buyers:
            buyers[r['wallet']] = dict(ts=r['ts'], seen_at=r['seen_at'], origin=r['origin'])
            origins.add(r['origin'])
    qualified = [w for w, r in buyers.items() if w in ranked
                 and ranked[w]['available_before'] < r['ts']]
    reasons = []
    if not creator or creator['status'] != 'verified':
        reasons.append('creator_identity_unresolved')
    if flow_error:reasons.append(flow_error)
    if indexed is None or not -15<=now-indexed<=30:
        reasons.append('trade_feed_stale')
    if net is None or net <= 0:
        reasons.append('verified_net_buying_not_positive')
    if len(buyers) < 2:
        reasons.append('fewer_than_two_distinct_buyers')
    return dict(buyers=buyers, qualified_wallets=qualified, net_buy_eth=str(net) if net is not None else None,
                attributed_wallet_net_eth=str(attributed_net),flow_source='canonical_pool_swap_logs',
                indexed_through=indexed,window_seconds=RULES['window'],
                observed_transactions=len(expected), attributed_transactions=len(expected & seen),
                reasons=reasons, independence='Distinct wallets and transaction senders; known creators excluded. Common funding and hidden coordination are not fully covered.')


def risk_reasons(s, now, config):
    _, _, reasons = decide(s, now, replace(config, min_score=0))
    reasons = [r for r in reasons if r != 'insufficient_launch_or_wallet_signal']
    if s.get('launch_age', math.inf) > RULES['max_launch_age']:
        reasons.append('launch_older_than_15_minutes')
    try:
        observation(s, s['_assessment_ts'], now)
        entry_amount(s)
        q = s['execution_quote']
        premium = RULES['position_size']/(q['quantity']*(1-RULES['slippage'])*s['price'])-1
        if not math.isfinite(premium) or premium > RULES['max_entry_premium']:
            reasons.append('entry_premium_above_2_percent')
        if q['usd_out']*(1-RULES['slippage'])-RULES['network_fee'] < RULES['position_size']*.9:
            reasons.append('independent_exit_depth_failed')
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        reasons.append('entry_evidence_missing_or_stale')
    return sorted(set(reasons))


def live_evidence(db, rpc, s):
    """Requote at a recent indexed block after actual decision/review latency."""
    cursor = db.execute('SELECT height FROM chain_cursor WHERE id=1').fetchone()
    if not cursor:
        raise ValueError('scanner_not_ready')
    block = rpc.block(cursor[0]); stamp = int(block['timestamp'],16)
    if not 0 <= time.time()-stamp <= 15:
        raise ValueError('scanner_not_current_enough_for_entry')
    token = db.execute('SELECT decimals FROM chain_tokens WHERE token=?', (s['token'],)).fetchone()
    if not token or token[0] is None:
        raise ValueError('token_decimals_missing')
    rate = Decimal(request_json('https://api.coinbase.com/v2/prices/ETH-USD/spot')['data']['amount'])
    if not rate.is_finite() or rate <= 0:
        raise ValueError('usd_reference_invalid')
    pool = pool_for(db, s['pool'])
    market = quotes.market(rpc,pool,token[0],cursor[0],rate,RULES['position_size']-RULES['network_fee'])
    sim = simulation.roundtrip(rpc,pool,market['execution_quote'],cursor[0])
    market['execution_quote']['quantity'] = float(Decimal(sim['token_received_atomic'])/10**token[0])
    market.update(block_hash=block['hash'],observed_at=stamp)
    if rpc.block(cursor[0])['hash'] != block['hash'] or time.time()-stamp > 30:
        raise ValueError('fresh_quote_expired_or_reorganized')
    return dict(s,market_evidence=market,trade_simulation=sim,execution_quote=market['execution_quote'],
                observed_at=stamp,price=market['price'],liquidity=market['liquidity'])


def liquidate(db, rpc, now):
    fresh = {}
    for position in db.execute('SELECT * FROM momentum_positions').fetchall():
        row = db.execute('SELECT * FROM launch_decisions WHERE token=? AND pool=? ORDER BY id DESC LIMIT 1',
                         (position['token'], position['pool'])).fetchone()
        try:
            if not row:
                raise ValueError('exit_observation_missing')
            if position['pool'] not in fresh:
                fresh[position['pool']] = live_evidence(db,rpc,json.loads(row['evidence']))
            s = fresh[position['pool']]
            value = exit_value(db, rpc, s, int(position['quantity_atomic']), s['observed_at'])
            if time.time()-s['market_evidence']['observed_at'] > 30:
                raise ValueError('exit_quote_expired_during_fetch')
        except (ValueError, KeyError, TypeError, StopIteration):
            with db:
                db.execute("UPDATE momentum_positions SET quote_status='unpriced' WHERE id=?", (position['id'],))
            continue
        filled_at = time.time()
        reason = exit_reason(value, position['cost'], filled_at-position['opened_at'])
        with db:
            if reason:
                db.execute('UPDATE momentum_accounts SET cash=cash+? WHERE arm=?', (value, position['arm']))
                db.execute('''INSERT INTO momentum_trades(arm,token,ts,side,quantity_atomic,cash_flow,pnl,reason,provenance)
                  VALUES(?,?,?,'sell',?,?,?,?,?)''', (position['arm'], position['token'], filled_at,
                    position['quantity_atomic'], value, value-position['cost'], reason,
                    encoded(dict(assessment=row['id'], block=s['market_evidence']['block'], modeled=True))))
                db.execute('DELETE FROM momentum_positions WHERE id=?', (position['id'],))
            else:
                db.execute("UPDATE momentum_positions SET mark=?,marked_at=?,quote_status='quoted' WHERE id=?",
                           (value, s['market_evidence']['observed_at'], position['id']))


def refresh_scanner_status(db,s,details,now):
    """A historical catch-up marker expires only after indexing is proven current."""
    if 'scanner_catching_up' not in s.get('risk_reasons',[]):return s
    cursor=db.execute('SELECT height FROM chain_cursor WHERE id=1').fetchone()
    block=(s.get('market_evidence') or {}).get('block')
    indexed=details.get('indexed_through')
    if cursor and isinstance(block,int) and cursor[0]>=block and isinstance(indexed,(int,float)) and -15<=now-indexed<=30:
        return dict(s,risk_reasons=[r for r in s['risk_reasons'] if r!='scanner_catching_up'])
    return s


def live_tick(db, rpc, config, now=None):
    now = time.time() if now is None else now
    capture_flows(db, now)
    liquidate(db, rpc, now)
    # A ranking frozen at the start of the 60-second signal window prevents
    # a just-computed historical result from qualifying an already-seen buy.
    cutoff = now-RULES['window']
    ranked = {r['wallet']: dict(r, available_before=cutoff) for r in rankings(db, cutoff) if r['qualified']}
    run = db.execute('SELECT * FROM momentum_run WHERE id=1').fetchone()
    rows = db.execute('''SELECT * FROM launch_decisions WHERE id IN
      (SELECT MAX(id) FROM launch_decisions WHERE ts>=? GROUP BY token) AND ts>=? ORDER BY id''',
      (now-120, run['started'])).fetchall()
    for row in rows:
        s = json.loads(row['evidence']); s['_assessment_ts'] = row['ts']
        details = signal(db, s, now, ranked)
        s = refresh_scanner_status(db,s,details,now)
        base = risk_reasons(s, now, config)+details['reasons']
        for arm in ARMS:
            if db.execute("SELECT 1 FROM momentum_decisions WHERE assessment=? AND arm=? AND action='BUY'", (row['id'], arm)).fetchone():
                continue
            reasons = list(base)
            if arm != 'simple_momentum' and len(details['qualified_wallets']) < 2:
                reasons.append('fewer_than_two_prequalified_followable_wallets')
            if arm == 'wallet_momentum_astra':
                from .astra_live import exists, entry_gate
                enabled = exists(db) and db.execute('SELECT 1 FROM astra_live WHERE id=1').fetchone()
                reasons += entry_gate(db, s, now, 'MIRROR') if enabled else ['astra_not_enabled']
            if db.execute('SELECT halted FROM account WHERE id=1').fetchone()[0]:
                reasons.append('shared_scanner_or_equity_halt')
            positions = db.execute('SELECT * FROM momentum_positions WHERE arm=?', (arm,)).fetchall()
            cash = db.execute('SELECT cash FROM momentum_accounts WHERE arm=?', (arm,)).fetchone()[0]
            conservative_equity = cash+sum(p['mark'] for p in positions if p['quote_status']=='quoted' and now-p['marked_at']<=120)
            if conservative_equity <= RULES['halt_equity'] or cash-RULES['position_size'] < RULES['reserve']:
                reasons.append('paper_capital_limit')
            if len(positions) >= RULES['max_positions']:
                reasons.append('max_positions')
            if db.execute("SELECT 1 FROM momentum_trades WHERE arm=? AND token=? AND side='buy'", (arm, s['token'])).fetchone():
                reasons.append('one_entry_per_token')
            value = None; fill_s = s; filled_at = now
            if not reasons:
                try:
                    fill_s = live_evidence(db,rpc,s)
                    amount = entry_amount(fill_s)
                    value = exit_value(db, rpc, fill_s, amount, fill_s['observed_at'])
                    filled_at = time.time()
                    if value < RULES['position_size']*.9:
                        reasons.append('independent_exit_depth_failed')
                    premium = RULES['position_size']/(fill_s['execution_quote']['quantity']*(1-RULES['slippage'])*s['price'])-1
                    if not math.isfinite(premium) or premium > RULES['max_entry_premium']:
                        reasons.append('entry_moved_above_trigger_price')
                    if fill_s['liquidity'] < config.min_liquidity:
                        reasons.append('fresh_liquidity_below_limit')
                    if (fill_s['market_evidence'].get('lp_burned_fraction') or 0) < .95:
                        reasons.append('fresh_withdrawal_protection_missing')
                    # Actual RPC/Astra waiting time counts against the signal window.
                    details = signal(db,s,filled_at,ranked)
                    reasons += details['reasons']+risk_reasons(s,filled_at,config)
                    if arm != 'simple_momentum' and len(details['qualified_wallets']) < 2:
                        reasons.append('qualified_wallet_signal_expired')
                    if arm == 'wallet_momentum_astra':
                        reasons += entry_gate(db,s,filled_at,'MIRROR')
                except (ValueError, KeyError, TypeError, StopIteration):
                    reasons.append('entry_quote_unavailable')
            with db:
                if not reasons:
                    db.execute('INSERT INTO momentum_positions(arm,token,pool,quantity_atomic,opened_at,cost,mark,marked_at,quote_status,assessment) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (arm,s['token'],s['pool'],str(amount),filled_at,RULES['position_size'],value,fill_s['observed_at'],'quoted',row['id']))
                    db.execute('UPDATE momentum_accounts SET cash=cash-? WHERE arm=?', (RULES['position_size'],arm))
                    db.execute("INSERT INTO momentum_trades(arm,token,ts,side,quantity_atomic,cash_flow,pnl,reason,provenance) VALUES(?,?,?,'buy',?,?,NULL,'momentum_entry',?)",
                               (arm,s['token'],filled_at,str(amount),-RULES['position_size'],encoded(dict(assessment=row['id'],signal=details,block=fill_s['market_evidence']['block'],block_hash=fill_s['market_evidence']['block_hash'],quote=fill_s['execution_quote'],modeled=True))))
                db.execute('''INSERT INTO momentum_decisions(assessment,arm,token,ts,action,reasons,signal)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(assessment,arm) DO UPDATE SET
                  ts=excluded.ts,action=excluded.action,reasons=excluded.reasons,signal=excluded.signal''',
                           (row['id'],arm,s['token'],now,'SKIP' if reasons else 'BUY',encoded(sorted(set(reasons))),encoded(details)))


def state(db, now=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='momentum_run'").fetchone():
        return None
    row = db.execute('SELECT * FROM momentum_run WHERE id=1').fetchone()
    if not row:
        return None
    now = time.time() if now is None else now
    result = dict(row); result['rules'] = json.loads(result['rules'])
    result['sample_counts'] = dict(db.execute('SELECT status,COUNT(*) FROM momentum_samples GROUP BY status'))
    ranked = rankings(db, now)
    result['ranked_wallets'] = len(ranked)
    result['qualified_wallets'] = sum(r['qualified'] for r in ranked)
    result['rankings'] = ranked[:25]
    result['accounts'] = []
    for a in db.execute('SELECT * FROM momentum_accounts ORDER BY arm'):
        p = [dict(r) for r in db.execute('SELECT * FROM momentum_positions WHERE arm=?', (a['arm'],))]
        for item in p:
            item['stale'] = now-item['marked_at'] > 120 or item['quote_status'] != 'quoted'
            item['stressed_value'] = 0 if item['stale'] else item['mark']
        totals = dict(db.execute("SELECT COUNT(*) fills,COALESCE(SUM(pnl),0) realized_pnl FROM momentum_trades WHERE arm=?", (a['arm'],)).fetchone())
        result['accounts'].append(dict(a, **totals, positions=p, stressed_equity=a['cash']+sum(x['stressed_value'] for x in p)))
    result['decisions'] = [dict(r) for r in db.execute('SELECT * FROM momentum_decisions ORDER BY id DESC LIMIT 30')]
    for d in result['decisions']:
        d['reasons'] = json.loads(d['reasons']); d['signal'] = json.loads(d['signal'])
    result['samples'] = [dict(r) for r in db.execute("SELECT wallet,token,status,entry_at,ended_at,return_fraction,stress_return,reason FROM momentum_samples WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 20")]
    result['trades'] = [dict(r) for r in db.execute('SELECT * FROM momentum_trades ORDER BY id DESC LIMIT 30')]
    result['priced_samples'] = [dict(r) for r in db.execute("SELECT wallet,token,entry_at,ended_at,return_fraction,reason,path FROM momentum_samples WHERE status='complete' AND replay_version=2 ORDER BY completed_at DESC LIMIT 20")]
    result['live_attribution'] = dict(db.execute('SELECT * FROM live_attribution_status WHERE id=1').fetchone() or {})
    from . import archive_replay
    result['replay']=archive_replay.state(db)
    result['limitations'] = ('Experimental quoted-fill model, not proven profit. Archive replay enters at least 30 seconds after the leader and samples every 30 seconds through the 15-minute timeout. Historical detection delay is assumed where first-seen time was not recorded. FX uses the last completed one-minute ETH/USD candle. Missing observations are retried and excluded from measured returns; separate -100% stress assumptions apply to unresolved exits. Sampling can miss intraperiod moves. Fixed fees/slippage, scoped simulations, MEV, and hidden wallet links remain limitations. Astra uses the existing fresh MIRROR approval including queue delay.')
    return result


def worker(path, config, stop, replay=False):
    db = sqlite3.connect(path, timeout=30); db.row_factory = sqlite3.Row
    rpc = None; seeded = 0
    field = 'replay' if replay else 'live'
    try:
        while not stop.is_set():
            worked = False
            try:
                if rpc is None:
                    rpc = evm.RPC(); rpc.verify()
                now = time.time()
                if replay:
                    from . import archive_replay
                    if now-seeded > 60:
                        seed_history(db, now); seeded = now
                    worked = archive_replay.step(db,rpc,now)
                else:
                    live_tick(db, rpc, config, now)
                with db:
                    db.execute(f'UPDATE momentum_run SET last_{field}=?,{field}_error=NULL WHERE id=1', (time.time(),))
            except Exception as exc:
                # Exceptions never include upstream bodies or endpoint credentials.
                with db:
                    db.execute(f'UPDATE momentum_run SET {field}_error=? WHERE id=1', ('Worker failed: '+type(exc).__name__,))
            # RPC calls already share the provider-wide rate limiter. Do not add
            # five idle seconds to every historical observation with work ready.
            stop.wait((.1 if worked else 5) if replay else 2)
    finally:
        db.close()
