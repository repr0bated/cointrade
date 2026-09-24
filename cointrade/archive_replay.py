"""Resumable archive sampling for delayed follower returns (measurement v2).

One entry or exit observation per step, shared by identical pool/time cases.
No dependence on later GUI assessments; no signing or broadcast operations.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import time
from urllib.parse import urlencode

from . import evm, quotes, simulation
from .providers import request_json

VERSION = 2
ACTIVE_CASES = 4
CASE_QUANTUM = 4
POLICY = dict(version=VERSION, exit_interval_seconds=30, block_rounding='first_at_or_after',
              entry_delay_seconds=30, max_entry_latency_seconds=120,
              historical_fx='last_closed_coinbase_60s_candle', max_fx_age_seconds=120,
              quote_retry_limit=3, incomplete_exit_stress_return=-1)


def dump(x):
    return json.dumps(x,sort_keys=True,allow_nan=False)


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS momentum_replay_version(id INTEGER PRIMARY KEY CHECK(id=1),
        version INTEGER,started REAL,policy TEXT,policy_hash TEXT);
      CREATE TABLE IF NOT EXISTS momentum_sample_archive(version INTEGER,tx TEXT,
        archived_at REAL,record TEXT,PRIMARY KEY(version,tx));
      CREATE TABLE IF NOT EXISTS momentum_archive_headers(number INTEGER PRIMARY KEY,hash TEXT,ts INTEGER);
      CREATE INDEX IF NOT EXISTS momentum_archive_time ON momentum_archive_headers(ts,number);
      CREATE INDEX IF NOT EXISTS data_blocks_timestamp ON data_blocks(timestamp,number);
      CREATE TABLE IF NOT EXISTS momentum_fx_candles(start INTEGER PRIMARY KEY,close TEXT,available_at INTEGER,source TEXT);
      CREATE TABLE IF NOT EXISTS momentum_fx_fetches(start INTEGER PRIMARY KEY,checked_at REAL,status TEXT);
      CREATE TABLE IF NOT EXISTS momentum_replay_cases(key TEXT PRIMARY KEY,token TEXT,pool TEXT,
        anchor_block INTEGER,entry_target REAL,status TEXT,entry_at REAL,entry_evidence TEXT,
        quantity_atomic TEXT,next_target REAL,ended_at REAL,completed_at REAL,
        return_fraction REAL,stress_return REAL,reason TEXT,path TEXT DEFAULT '[]',
        retries INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,has_gaps INTEGER DEFAULT 0);
      CREATE INDEX IF NOT EXISTS momentum_replay_ready ON momentum_replay_cases(status,retry_at,entry_target);
      CREATE TABLE IF NOT EXISTS momentum_replay_members(tx TEXT PRIMARY KEY,case_key TEXT);
      CREATE TABLE IF NOT EXISTS momentum_replay_slots(
        slot INTEGER PRIMARY KEY,case_key TEXT UNIQUE NOT NULL,visited REAL DEFAULT 0,
        observations INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS momentum_replay_pool_schedule(
        pool TEXT PRIMARY KEY,last_started REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS momentum_replay_focus(
        wallet TEXT PRIMARY KEY,selected_at REAL NOT NULL,finished_at REAL);
      CREATE INDEX IF NOT EXISTS momentum_replay_pool_pending ON momentum_replay_cases(pool,status,entry_target);
      CREATE INDEX IF NOT EXISTS momentum_replay_members_case ON momentum_replay_members(case_key);
    ''')
    columns={r[1] for r in db.execute('PRAGMA table_info(momentum_samples)')}
    for name,kind in (('replay_version','INTEGER DEFAULT 1'),('stress_return','REAL')):
        if name not in columns:
            db.execute(f'ALTER TABLE momentum_samples ADD COLUMN {name} {kind}')
    if 'observations' not in {r[1] for r in db.execute('PRAGMA table_info(momentum_replay_slots)')}:
        db.execute('ALTER TABLE momentum_replay_slots ADD COLUMN observations INTEGER DEFAULT 0')
        db.execute('''UPDATE momentum_replay_slots SET observations=MIN(?,
          (SELECT json_array_length(path) FROM momentum_replay_cases WHERE key=case_key))''',(CASE_QUANTUM,))
    # Existing pools enter the scheduling queue at cohort availability time.
    # Newly enrolled pools join at the back, so continuous arrivals cannot
    # starve a partially measured case forever.
    db.execute('''INSERT OR IGNORE INTO momentum_replay_pool_schedule
      SELECT c.pool,MIN(s.queued_at) FROM momentum_replay_cases c
      JOIN momentum_replay_members m ON m.case_key=c.key
      JOIN momentum_samples s ON s.tx=m.tx GROUP BY c.pool''')
    db.commit()


def initialize(db, now=None):
    schema(db)
    now=time.time() if now is None else now
    policy=dump(POLICY); digest=hashlib.sha256(policy.encode()).hexdigest()
    row=db.execute('SELECT * FROM momentum_replay_version WHERE id=1').fetchone()
    if row and (row['version']!=VERSION or row['policy_hash']!=digest):
        raise ValueError('Archive replay measurement policy differs')
    if row:
        return
    # Preserve superseded diagnostics. Trading rules, accounts, and fills stay intact.
    records=[dict(r) for r in db.execute('SELECT * FROM momentum_samples')]
    with db:
        for r in records:
            db.execute('INSERT OR IGNORE INTO momentum_sample_archive VALUES(1,?,?,?)',(r['tx'],now,dump(r)))
        db.execute("UPDATE momentum_samples SET status='queued',entry_id=NULL,entry_at=NULL,ended_at=NULL,completed_at=NULL,return_fraction=NULL,stress_return=NULL,reason=NULL,path=NULL,replay_version=?",(VERSION,))
        db.execute('INSERT INTO momentum_replay_version VALUES(1,?,?,?,?)',(VERSION,now,policy,digest))


def header(db,rpc,height):
    cached=db.execute('SELECT * FROM momentum_archive_headers WHERE number=?',(height,)).fetchone()
    if cached:
        return dict(cached)
    raw=rpc.block(height)
    if int(raw['number'],16)!=height:
        raise ValueError('Archive header number mismatch')
    result=dict(number=height,hash=raw['hash'],ts=int(raw['timestamp'],16))
    indexed=db.execute('SELECT hash FROM data_blocks WHERE number=?',(height,)).fetchone()
    if indexed and indexed[0]!=result['hash']:
        raise ValueError('Archive header does not match indexed chain')
    with db:db.execute('INSERT OR IGNORE INTO momentum_archive_headers VALUES(?,?,?)',(height,result['hash'],result['ts']))
    return result


def block_at(db,rpc,target,anchor):
    target=math.ceil(target)
    lower=header(db,rpc,anchor)
    if lower['ts']>=target:
        # Anchor is the leader's block. Entries are always strictly later.
        if lower['ts']>target:raise ValueError('Historical target precedes anchor')
        return lower
    before=db.execute('SELECT number FROM data_blocks WHERE timestamp<? AND number>=? ORDER BY timestamp DESC,number DESC LIMIT 1',(target,anchor)).fetchone()
    if before:
        candidate=header(db,rpc,before[0])
        if candidate['ts']<target:lower=candidate
    upper_row=db.execute('SELECT number FROM data_blocks WHERE timestamp>=? AND number>? ORDER BY timestamp,number LIMIT 1',(target,lower['number'])).fetchone()
    high=upper_row[0] if upper_row else db.execute('SELECT height FROM chain_cursor WHERE id=1').fetchone()[0]
    upper=header(db,rpc,high)
    if upper['ts']<target:raise ValueError('Target block not indexed yet')
    while lower['number']+1<upper['number']:
        mid=header(db,rpc,(lower['number']+upper['number'])//2)
        if mid['ts']<target:lower=mid
        else:upper=mid
    return upper


def fx_at(db,target):
    # Never use the close of the candle containing the target: it is future data.
    minute=int(target//60)*60-60
    row=db.execute('SELECT * FROM momentum_fx_candles WHERE start=?',(minute,)).fetchone()
    if not row:
        bucket=minute//3600*3600
        fetched=db.execute('SELECT * FROM momentum_fx_fetches WHERE start=?',(bucket,)).fetchone()
        if not fetched or time.time()-fetched['checked_at']>300:
            start=datetime.fromtimestamp(bucket,timezone.utc).isoformat()
            end=datetime.fromtimestamp(bucket+3600,timezone.utc).isoformat()
            url='https://api.exchange.coinbase.com/products/ETH-USD/candles?'+urlencode(dict(start=start,end=end,granularity=60))
            rows=request_json(url)
            if not isinstance(rows,list):raise ValueError('Historical FX response invalid')
            parsed=[]
            for r in rows:
                if not isinstance(r,list) or len(r)<5:continue
                stamp=int(r[0]);value=Decimal(str(r[4]))
                if stamp%60 or not value.is_finite() or value<=0:continue
                if bucket<=stamp<bucket+3600:
                    parsed.append((stamp,str(value),stamp+60,'coinbase_ETH-USD_closed_60s_candle'))
            with db:
                db.executemany('INSERT OR IGNORE INTO momentum_fx_candles VALUES(?,?,?,?)',parsed)
                db.execute('INSERT OR REPLACE INTO momentum_fx_fetches VALUES(?,?,?)',(bucket,time.time(),'available' if parsed else 'empty'))
        row=db.execute('SELECT * FROM momentum_fx_candles WHERE start=?',(minute,)).fetchone()
    if not row or not 0<=target-row['available_at']<=POLICY['max_fx_age_seconds']:
        raise ValueError('Historical FX candle unavailable')
    return Decimal(row['close']),dict(row)


def archive_evidence(db,rpc,case,target):
    from .momentum import pool_for,RULES
    block=block_at(db,rpc,target,case['anchor_block'])
    rate,fx=fx_at(db,block['ts'])
    pool=pool_for(db,case['pool'])
    # Follower execution needs atomic swap amounts, not token metadata or a TVL
    # lens. Those optional reads can revert even when the actual route works.
    qi=0 if pool['token0'] in (evm.ZERO,evm.WETH) else 1
    amount=int(Decimal(str(RULES['position_size']-RULES['network_fee']))/rate*10**18)
    bought=quotes.exact_input(rpc,pool,qi==0,amount,block['number'])
    returned=quotes.exact_input(rpc,pool,qi!=0,bought,block['number'])
    quote=dict(status='quoted',source='canonical_archive_exact_input',block=block['number'],
               quote_in_atomic=str(amount),token_out_atomic=str(bought),quote_out_atomic=str(returned),
               usd_in=float(Decimal(amount)/10**18*rate),usd_out=float(Decimal(returned)/10**18*rate),fee_included=True)
    sim=simulation.roundtrip(rpc,pool,quote,block['number'])
    market=dict(source='canonical_archive_exact_input',block=block['number'],
                observed_at=block['ts'],block_hash=block['hash'],execution_quote=quote)
    if rpc.block(block['number'])['hash']!=block['hash']:
        raise ValueError('Archive quote block hash mismatch')
    return dict(token=case['token'],pool=case['pool'],observed_at=block['ts'],
                market_evidence=market,execution_quote=quote,trade_simulation=sim,fx_evidence=fx)


def terminal_samples(db,case,now):
    with db:
        db.execute('''UPDATE momentum_samples SET status=?,entry_at=?,ended_at=?,completed_at=?,
          return_fraction=?,stress_return=?,reason=?,path=?,replay_version=?
          WHERE tx IN(SELECT tx FROM momentum_replay_members WHERE case_key=?)''',
          (case['status'],case['entry_at'],case['ended_at'],now,case['return_fraction'],
           case['stress_return'],case['reason'],case['path'],VERSION,case['key']))


def enroll(db,now,limit=100):
    """Bind first observed buys to their actual transaction pool, not a later winner."""
    from .momentum import RULES
    rows=db.execute('''SELECT s.* FROM momentum_samples s LEFT JOIN momentum_replay_members m ON m.tx=s.tx
      WHERE m.tx IS NULL AND s.status='queued' ORDER BY leader_ts,tx LIMIT ?''',(limit,)).fetchall()
    for r in rows:
        pools=db.execute('''SELECT DISTINCT p.pool,p.token0,p.token1,e.block,e.block_hash
          FROM chain_swaps s JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
          JOIN chain_pools p ON p.pool=s.pool
          WHERE s.tx=? AND s.token=? AND p.block<=e.block''',(r['tx'],r['token'])).fetchall()
        native=[p for p in pools if (p['token0'] in (evm.ZERO,evm.WETH)) != (p['token1'] in (evm.ZERO,evm.WETH))]
        if len(native)!=1:
            with db:db.execute("UPDATE momentum_samples SET status='unresolved',reason='leader_pool_ambiguous_or_missing',completed_at=?,replay_version=? WHERE tx=?",(now,VERSION,r['tx']))
            continue
        p=native[0]
        observed=db.execute('SELECT seen_at FROM momentum_flows WHERE tx=?',(r['tx'],)).fetchone()
        # For historical rows without first-seen times, the 30s delay is an explicit assumption.
        target=max(r['leader_ts']+RULES['replay_delay'],observed[0] if observed else 0)
        if target>r['leader_ts']+RULES['replay_entry_deadline']:
            with db:db.execute("UPDATE momentum_samples SET status='entry_rejected',reason='observed_after_entry_deadline',completed_at=?,replay_version=? WHERE tx=?",(now,VERSION,r['tx']))
            continue
        target=math.ceil(target)
        key=hashlib.sha256(dump((VERSION,p['pool'],target)).encode()).hexdigest()
        with db:
            db.execute('''INSERT OR IGNORE INTO momentum_replay_cases
              (key,token,pool,anchor_block,entry_target,status,next_target)
              VALUES(?,?,?,?,?,'entry',?)''',(key,r['token'],p['pool'],p['block'],target,target))
            db.execute('INSERT INTO momentum_replay_members VALUES(?,?)',(r['tx'],key))
            db.execute('INSERT OR IGNORE INTO momentum_replay_pool_schedule VALUES(?,?)',(p['pool'],now))
            db.execute("UPDATE momentum_samples SET status='replaying',replay_version=? WHERE tx=?",(VERSION,r['tx']))
        case=db.execute('SELECT * FROM momentum_replay_cases WHERE key=?',(key,)).fetchone()
        if case['completed_at']:terminal_samples(db,case,now)


def known_execution_failure(exc):
    if isinstance(exc,evm.CallReverted):return True
    message=str(exc)
    return any(message.startswith(s) for s in ('Pool has no reserves','Pool quote returned zero output',
        'Round-trip simulation reverted','Simulation buy delivered no tokens',
        'Simulation sell delivered no quote currency','entry_simulation_costs_failed',
        'independent_exit_depth_failed','entry_quantity_zero'))


def focus_wallet(db,now):
    """Finish a repeat wallet's history; selection never uses its returns."""
    from .momentum import RULES
    active=db.execute('SELECT wallet FROM momentum_replay_focus WHERE finished_at IS NULL ORDER BY selected_at LIMIT 1').fetchone()
    if active:
        if db.execute("SELECT 1 FROM momentum_samples WHERE wallet=? AND status IN ('queued','replaying') LIMIT 1",(active[0],)).fetchone():
            return active[0]
        with db:db.execute('UPDATE momentum_replay_focus SET finished_at=? WHERE wallet=?',(now,active[0]))
    row=db.execute('''SELECT wallet FROM momentum_samples
      WHERE wallet NOT IN (SELECT wallet FROM momentum_replay_focus)
      GROUP BY wallet HAVING COUNT(*)>=? AND COUNT(DISTINCT token)>=?
        AND SUM(status IN ('queued','replaying'))>0
      ORDER BY MIN(leader_ts),wallet LIMIT 1''',(RULES['min_samples'],RULES['min_tokens'])).fetchone()
    if row:
        with db:db.execute('INSERT INTO momentum_replay_focus VALUES(?,?,NULL)',(row[0],now))
        return row[0]
    return None


def next_case(db,now):
    """Rotate bounded in-progress cases across pools, independent of returns.

    A pool with many early buyers must not monopolize the entire backfill.
    Every selected case still samples its full, unchanged 30-second path.
    Assignments persist across restarts; a retry never advances a target.
    """
    focus=focus_wallet(db,now)
    with db:
        db.execute('''UPDATE momentum_replay_pool_schedule SET last_started=? WHERE pool IN(
          SELECT c.pool FROM momentum_replay_slots s JOIN momentum_replay_cases c ON c.key=s.case_key
          WHERE c.status NOT IN ('entry','exit') OR s.observations>=?)''',(now,CASE_QUANTUM))
        db.execute("DELETE FROM momentum_replay_slots WHERE case_key NOT IN (SELECT key FROM momentum_replay_cases WHERE status IN ('entry','exit'))")
        db.execute('DELETE FROM momentum_replay_slots WHERE observations>=?',(CASE_QUANTUM,))
        # Also handle cases restored without an enrollment row (e.g. recovery).
        db.execute('''INSERT OR IGNORE INTO momentum_replay_pool_schedule
          SELECT pool,? FROM momentum_replay_cases GROUP BY pool''',(now,))
        used={r[0] for r in db.execute('SELECT slot FROM momentum_replay_slots')}
        for slot in range(ACTIVE_CASES):
            if slot in used:continue
            row=db.execute('''SELECT c.* FROM momentum_replay_cases c
              LEFT JOIN momentum_replay_slots s ON s.case_key=c.key
              LEFT JOIN momentum_replay_pool_schedule p ON p.pool=c.pool
              WHERE s.case_key IS NULL AND c.status IN ('entry','exit')
                AND c.pool NOT IN (SELECT a.pool FROM momentum_replay_slots s
                  JOIN momentum_replay_cases a ON a.key=s.case_key)
              ORDER BY CASE WHEN ?<3 AND EXISTS(
                SELECT 1 FROM momentum_replay_members m JOIN momentum_samples f ON f.tx=m.tx
                WHERE m.case_key=c.key AND f.wallet=?) THEN 0 ELSE 1 END,
                p.last_started,c.entry_target,c.key LIMIT 1''',(slot,focus)).fetchone()
            if not row:break
            db.execute('INSERT INTO momentum_replay_slots(slot,case_key) VALUES(?,?)',(slot,row['key']))
            db.execute('INSERT OR REPLACE INTO momentum_replay_pool_schedule VALUES(?,?)',(row['pool'],now))
    return db.execute('''SELECT c.* FROM momentum_replay_slots s
      JOIN momentum_replay_cases c ON c.key=s.case_key WHERE c.retry_at<=?
      ORDER BY s.visited,s.slot LIMIT 1''',(now,)).fetchone()


def step(db,rpc,now=None):
    """At most one chain observation. Progress survives restart and provider failure."""
    from .momentum import RULES,entry_amount,exit_value,exit_reason
    now=time.time() if now is None else now
    enroll(db,now)
    row=next_case(db,now)
    if not row:return False
    c=dict(row);path=json.loads(c['path']);target=c['next_target']
    try:
        evidence=archive_evidence(db,rpc,c,target)
        stamp=evidence['observed_at']
        if c['status']=='entry':
            if stamp-c['entry_target']>5:raise ValueError('Historical block timing gap')
            amount=entry_amount(evidence)
            initial=exit_value(db,rpc,evidence,amount,stamp)
            if initial<RULES['position_size']*.9:raise ValueError('independent_exit_depth_failed')
            c.update(status='exit',entry_at=stamp,entry_evidence=dump(evidence),quantity_atomic=str(amount),
                     next_target=stamp+POLICY['exit_interval_seconds'],reason=None,retries=0,retry_at=0)
            path.append(dict(kind='entry',target=target,ts=stamp,block=evidence['market_evidence']['block'],
                             block_hash=evidence['market_evidence']['block_hash'],cost=RULES['position_size'],
                             quantity_atomic=str(amount),fx=evidence['fx_evidence']))
        else:
            value=exit_value(db,rpc,evidence,int(c['quantity_atomic']),stamp)
            why=exit_reason(value,RULES['position_size'],stamp-c['entry_at'])
            path.append(dict(kind='exit_quote',target=target,ts=stamp,block=evidence['market_evidence']['block'],
                             block_hash=evidence['market_evidence']['block_hash'],net_exit_usd=value,fx=evidence['fx_evidence']))
            c.update(retries=0,retry_at=0,reason=None)
            if why:
                # Returns following a missing interval are diagnostics, not a fully observed path.
                c.update(status='incomplete' if c['has_gaps'] else 'complete',ended_at=stamp,
                         completed_at=now,return_fraction=value/RULES['position_size']-1,
                         stress_return=-1 if c['has_gaps'] else None,reason=why)
            else:c['next_target']=min(c['entry_at']+RULES['max_hold'],target+POLICY['exit_interval_seconds'])
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        reason=str(exc)[:180] if isinstance(exc,ValueError) else 'Archive response incomplete'
        definite=known_execution_failure(exc)
        if c['status']=='entry' and definite:
            c.update(status='entry_rejected',reason=reason,completed_at=now)
        elif not definite and c['retries']<POLICY['quote_retry_limit']:
            c.update(retries=c['retries']+1,retry_at=now+min(120,5*2**c['retries']),reason=reason)
        else:
            path.append(dict(kind='unavailable',target=target,reason=reason,
                             cause='execution_failure' if definite else 'data_unavailable'))
            if c['status']=='entry':
                c.update(status='unresolved',reason=reason,completed_at=now)
            elif target>=c['entry_at']+RULES['max_hold']:
                c.update(status='unresolved',ended_at=target,completed_at=now,return_fraction=None,
                         stress_return=-1.,reason='Exit unavailable at timeout: '+reason)
            else:
                c.update(next_target=target+POLICY['exit_interval_seconds'],has_gaps=1,
                         retries=0,retry_at=0,reason=reason)
    c['path']=dump(path)
    with db:
        db.execute('UPDATE momentum_replay_slots SET visited=?,observations=observations+1 WHERE case_key=?',(time.time(),c['key']))
        db.execute('''UPDATE momentum_replay_cases SET status=:status,entry_at=:entry_at,
          entry_evidence=:entry_evidence,quantity_atomic=:quantity_atomic,next_target=:next_target,
          ended_at=:ended_at,completed_at=:completed_at,return_fraction=:return_fraction,
          stress_return=:stress_return,reason=:reason,path=:path,retries=:retries,
          retry_at=:retry_at,has_gaps=:has_gaps WHERE key=:key''',c)
    if c['completed_at']:terminal_samples(db,c,time.time())
    return True


def state(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='momentum_replay_version'").fetchone():return None
    row=db.execute('SELECT * FROM momentum_replay_version WHERE id=1').fetchone()
    if not row:return None
    r=dict(row);r['policy']=json.loads(r['policy'])
    r['case_counts']=dict(db.execute('SELECT status,COUNT(*) FROM momentum_replay_cases GROUP BY status'))
    r['fx_candles']=db.execute('SELECT COUNT(*) FROM momentum_fx_candles').fetchone()[0]
    r['archive_headers']=db.execute('SELECT COUNT(*) FROM momentum_archive_headers').fetchone()[0]
    r['superseded_samples']=db.execute('SELECT COUNT(*) FROM momentum_sample_archive').fetchone()[0]
    r['scheduling']='Three slots prioritize one repeat wallet’s full history; one keeps broader coverage moving. Pools yield after four observations. Selection never uses returns.'
    focus=db.execute('SELECT * FROM momentum_replay_focus ORDER BY (finished_at IS NULL) DESC,selected_at DESC LIMIT 1').fetchone()
    r['focus']=None
    if focus:
        counts=dict(db.execute('SELECT status,COUNT(*) FROM momentum_samples WHERE wallet=? GROUP BY status',(focus['wallet'],)))
        r['focus']=dict(focus,counts=counts,total=sum(counts.values()),measured=counts.get('complete',0),
                        pending=counts.get('queued',0)+counts.get('replaying',0),
                        selection='Earliest observed repeat wallet with at least ten entries across five tokens; returns are not used for selection.')
    r['active']=[dict(x) for x in db.execute('''SELECT c.token,c.pool,c.entry_target,c.status,c.entry_at,
      c.next_target,c.retries,c.reason FROM momentum_replay_slots s
      JOIN momentum_replay_cases c ON c.key=s.case_key WHERE c.status IN ('entry','exit') ORDER BY s.slot''')]
    return r
