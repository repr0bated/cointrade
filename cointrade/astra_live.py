"""Persistent Astra subscription queue and an additional paper-entry gate.

One automatic review per observed token; manual rechecks require new evidence.
No signing, broadcast, API-key fallback, or authority to bypass risk rules.
"""
import json
import sqlite3
import time

from . import subscription, evidence as evidence_schema, ledger

MAX_APPROVAL_AGE = 300
ENRICHMENT_WAIT = 180


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS astra_live (
        id INTEGER PRIMARY KEY CHECK(id=1), started REAL NOT NULL,
        status TEXT NOT NULL, error TEXT);
      CREATE TABLE IF NOT EXISTS astra_live_reviews (
        id INTEGER PRIMARY KEY, token TEXT NOT NULL, evidence_key TEXT,
        queued_at REAL NOT NULL, started_at REAL, finished_at REAL,
        status TEXT NOT NULL, model TEXT NOT NULL, action TEXT,
        response TEXT, evidence TEXT, usage TEXT, error TEXT,
        UNIQUE(token,evidence_key));
      CREATE INDEX IF NOT EXISTS astra_live_queue ON astra_live_reviews(status,id);
      CREATE INDEX IF NOT EXISTS astra_live_token ON astra_live_reviews(token,id DESC);
      CREATE INDEX IF NOT EXISTS launch_decisions_token_id ON launch_decisions(token,id DESC);
    ''')
    if 'refresh_requested_at' not in {r[1] for r in db.execute('PRAGMA table_info(astra_live_reviews)')}:
        db.execute('ALTER TABLE astra_live_reviews ADD COLUMN refresh_requested_at REAL')
        db.commit()


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='astra_live'").fetchone())


def start(db):
    schema(db)
    with db:
        # A restart preserves pauses, failures, prior reviews, and the original queue boundary.
        db.execute("INSERT OR IGNORE INTO astra_live VALUES(1,?,'running',NULL)", (time.time()-120,))


def enqueue(db):
    run = db.execute('SELECT * FROM astra_live WHERE id=1').fetchone()
    if not run or run['status'] != 'running':
        return
    with db:
        db.execute('''INSERT INTO astra_live_reviews(token,queued_at,status,model)
          SELECT t.token,?,'queued',? FROM chain_tokens t
          WHERE (t.first_ts>=? OR EXISTS(SELECT 1 FROM launch_decisions d
             WHERE d.token=t.token AND d.ts>=?))
          AND NOT EXISTS(SELECT 1 FROM astra_live_reviews r WHERE r.token=t.token)
          ORDER BY t.first_block DESC LIMIT 100''',
          (time.time(), subscription.MODEL, run['started'], run['started']))


def attach_policies(db, packet):
    """Describe the rules the on-chain engines actually enforce, by strategy."""
    row=db.execute('SELECT config FROM account WHERE id=1').fetchone()
    config=json.loads(row[0]) if row else {}
    # The generic screener's age/volume gates are not used by launchrisk.decide.
    limits={k:v for k,v in config.items() if k not in ('min_age_seconds','min_volume_h24')}
    limits.update(max_launch_age_seconds=86400,snipe_max_age_seconds=300,
                  snipe_min_distinct_buyers=3,mirror_min_qualified_wallets=1,
                  required_v2_lp_burned_fraction=.95)
    packet['configured_limits']=limits
    packet['policy_version']=2
    packet['strategy_policies']={'onchain_launch':dict(limits=limits,
      age_policy='Fresh launches are eligible; no minimum token age or 24-hour volume gate.')}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='momentum_run'").fetchone():
        run=db.execute('SELECT rules FROM momentum_run WHERE id=1').fetchone()
        if run:
            from . import momentum
            frozen=json.loads(run[0]);frozen.pop('safety_limits',None)
            policy=dict(limits=frozen,min_score=0,
              safety_limits={k:config[k] for k in ('min_liquidity','max_top10_share','max_snapshot_age') if k in config},
              signal_policy='Two prequalified wallets in 60 seconds, positive verified pool net buying, and all contract/liquidity checks.')
            snapshot=packet.get('evidence') or {}
            if snapshot.get('pool'):
                now=time.time();cutoff=now-momentum.RULES['window']
                ranked={r['wallet']:dict(r,available_before=cutoff) for r in momentum.rankings(db,cutoff) if r['qualified']}
                policy['signal']=dict(momentum.signal(db,snapshot,now,ranked),evaluated_at=now)
            packet['strategy_policies']['wallet_momentum_astra']=policy
    return packet


def evidence_for(db, token):
    row = db.execute('SELECT * FROM launch_decisions WHERE token=? ORDER BY id DESC LIMIT 1', (token,)).fetchone()
    if row:
        evidence = {k: json.loads(row[k]) if k in ('evidence','reasons') else row[k] for k in row.keys()}
        doc=evidence['evidence'].get('evidence_contract') or evidence_schema.build(evidence['evidence'],row['ts'],origin='legacy_adapter')
        evidence['evidence']['evidence_contract']=doc
        evidence['quality_at_review']=evidence_schema.quality(doc,time.time())
        evidence['review_requested_at'] = time.time()
        evidence['evidence_age_seconds'] = max(0,time.time()-row['ts'])
        return str(row['id']), attach_policies(db,evidence)
    token_row = db.execute('''SELECT token,first_block,first_ts,deployment_confirmed,decimals,owner
                             FROM chain_tokens WHERE token=?''', (token,)).fetchone()
    if not token_row:
        raise ValueError('Token has not been detected by the scanner')
    evidence = dict(token_row)
    evidence['pools'] = [dict(r) for r in db.execute('''SELECT pool,version,token0,token1,block,ts
        FROM chain_pools WHERE token0=? OR token1=? ORDER BY block DESC LIMIT 5''',(token,token))]
    for table, field, name in (('chain_source_checks','evidence','source_verification'),
                               ('chain_holder_checks','evidence','holder_evidence'),
                               ('chain_risks','raw','provider_risk_data')):
        cached = db.execute(f'SELECT {field},checked_at FROM {table} WHERE token=?',(token,)).fetchone()
        if cached:
            evidence[name] = dict(data=json.loads(cached[field]),checked_at=cached['checked_at'])
    evidence['evidence_contract']=evidence_schema.build(dict(evidence,token=token),time.time(),origin='legacy_adapter')
    return 'unassessed', attach_policies(db,dict(token=token, action='SKIP', ts=time.time(),
        reasons=['No complete deterministic assessment yet'], evidence=evidence))


def request(db, token):
    run = db.execute('SELECT * FROM astra_live WHERE id=1').fetchone()
    if not run or run['status'] != 'running':
        raise ValueError('Resume Astra before requesting a review')
    with db:
        db.execute('BEGIN IMMEDIATE')
        key, _ = evidence_for(db, token)
        existing = db.execute('''SELECT id,status FROM astra_live_reviews WHERE token=?
          AND (status IN ('queued','running') OR evidence_key=?) ORDER BY id DESC LIMIT 1''', (token,key)).fetchone()
        if existing:
            return dict(existing)
        row = db.execute('''INSERT INTO astra_live_reviews(token,queued_at,status,model)
          VALUES(?,?,'queued',?)''', (token,time.time(),subscription.MODEL))
        return dict(id=row.lastrowid,status='queued')


def control(db, action):
    if action not in ('pause','resume'):
        raise ValueError('Unknown Astra control')
    with db:
        db.execute('UPDATE astra_live SET status=?,error=NULL WHERE id=1',
                   ('paused' if action=='pause' else 'running',))


def claim(db):
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = db.execute('SELECT * FROM astra_live WHERE id=1').fetchone()
        if not run or run['status'] != 'running':
            return None
        # Three turns favor fresh assessed launches; every fourth drains older
        # work. A FIFO backlog must not consume a new launch's approval window.
        completed=db.execute("SELECT COUNT(*) FROM astra_live_reviews WHERE status IN ('complete','failed')").fetchone()[0]
        prefer_fresh=completed%4!=3
        now=time.time()
        row = db.execute('''SELECT r.* FROM astra_live_reviews r WHERE r.status='queued'
          AND (r.queued_at<=? OR EXISTS(SELECT 1 FROM launch_decisions d WHERE d.token=r.token))
          ORDER BY (? AND EXISTS(SELECT 1 FROM launch_decisions d WHERE d.token=r.token
            AND d.ts>=? AND json_extract(d.evidence,'$.launch_age') BETWEEN 0 AND 900)) DESC,
            EXISTS(SELECT 1 FROM launch_decisions d WHERE d.token=r.token) DESC,r.id LIMIT 1''',
          (now-ENRICHMENT_WAIT,prefer_fresh,now-120)).fetchone()
        if not row:
            return None
        key, evidence = evidence_for(db, row['token'])
        quality=evidence.get('quality_at_review') or {}
        refreshable={'STALE_QUOTE','STALE_HOLDERS','STALE_PERMISSIONS','MISSING_PROVENANCE'}
        needs=[f for f in quality.get('findings',[]) if f['code'] in refreshable and f['follow_up']!='none']
        for f in needs:ledger.request_refresh(db,row['token'],f['follow_up'],time.time(),'astra_preflight')
        pending=db.execute("SELECT 1 FROM evidence_refresh_requests WHERE token=? AND status IN ('queued','running')",(row['token'],)).fetchone()
        if needs and pending:
            started=row['refresh_requested_at']
            if started is None:
                db.execute('UPDATE astra_live_reviews SET refresh_requested_at=? WHERE id=?',(time.time(),row['id']))
                return None
            if time.time()-started<90:return None
        # A newer assessment may have been queued manually while an earlier request was waiting.
        if db.execute('SELECT 1 FROM astra_live_reviews WHERE token=? AND evidence_key=? AND id<>?',
                      (row['token'],key,row['id'])).fetchone():
            db.execute("UPDATE astra_live_reviews SET status='superseded',finished_at=? WHERE id=?", (time.time(),row['id']))
            return None
        content = json.dumps(evidence)
        if len(content.encode()) > 60000:
            db.execute("UPDATE astra_live_reviews SET status='failed',error='Evidence exceeds review size limit' WHERE id=?", (row['id'],))
            return None
        db.execute("UPDATE astra_live_reviews SET status='running',evidence_key=?,evidence=?,started_at=? WHERE id=?",
                   (key,content,time.time(),row['id']))
        return row['id'],content


def review(db, item):
    review_id, content = item
    try:
        answer, usage = subscription.run_review(content)
        with db:
            db.execute('''UPDATE astra_live_reviews SET status='complete',action=?,response=?,usage=?,finished_at=?
              WHERE id=?''', (answer['action'],json.dumps(answer),json.dumps(usage),time.time(),review_id))
            token=json.loads(content)['token']
            for f in answer.get('findings',[]):
                ledger.request_refresh(db,token,f['follow_up'],time.time(),'astra')
    except Exception as exc:
        message = str(exc)[:180] if isinstance(exc,ValueError) else type(exc).__name__
        with db:
            db.execute("UPDATE astra_live_reviews SET status='failed',error=?,finished_at=? WHERE id=?",
                       (message,time.time(),review_id))
            db.execute("UPDATE astra_live SET status='error',error=? WHERE id=1", (message,))


def state(db):
    if not exists(db):
        return None
    run = db.execute('SELECT * FROM astra_live WHERE id=1').fetchone()
    if not run:
        return None
    counts = dict(db.execute('SELECT status,COUNT(*) FROM astra_live_reviews GROUP BY status'))
    recent = [dict(r) for r in db.execute('''SELECT * FROM astra_live_reviews
      ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'complete' THEN 1
        WHEN 'failed' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,
        COALESCE(finished_at,started_at,queued_at) DESC LIMIT 50''')]
    for r in recent:
        for key in ('response','usage','evidence'):
            r[key] = json.loads(r[key]) if r[key] else None
    return dict(**dict(run), model=subscription.MODEL, reasoning='high',
        backend='codex_subscription', billing='ChatGPT subscription; no API fallback',
        counts=counts, recent=recent, mode='paper', approval_max_age_seconds=MAX_APPROVAL_AGE,
        policy='One automatic review per detected token; recheck on new evidence. Astra and risk rules must both approve entries.')


def entry_gate(db, snapshot, now, action):
    if not exists(db):
        return []
    run = db.execute('SELECT status FROM astra_live WHERE id=1').fetchone()
    if not run:
        return []
    if run['status'] != 'running':
        return ['astra_'+run['status']]
    row = db.execute('SELECT * FROM astra_live_reviews WHERE token=? ORDER BY id DESC LIMIT 1', (snapshot['token'],)).fetchone()
    if not row or row['status'] != 'complete':
        return ['astra_review_'+(row['status'] if row else 'pending')]
    if row['action'] == 'SKIP':
        return ['astra_skip']
    evidence = json.loads(row['evidence'])
    stamp = evidence.get('ts', 0)
    if not 0 <= now-stamp <= MAX_APPROVAL_AGE or not row['finished_at'] or now-row['finished_at'] > MAX_APPROVAL_AGE:
        return ['astra_review_stale']
    if (evidence.get('evidence') or {}).get('pool') != snapshot.get('pool'):
        return ['astra_review_pool_mismatch']
    if row['action'] != action or action not in ('SNIPE','MIRROR'):
        return ['astra_signal_no_longer_matches']
    return []


def worker(path, stop):
    db = sqlite3.connect(path,timeout=30)
    db.row_factory = sqlite3.Row
    try:
        schema(db)
        # Interrupted subscription requests are never silently repeated.
        with db:
            interrupted = db.execute("UPDATE astra_live_reviews SET status='failed',error='Review interrupted; request not repeated' WHERE status='running'").rowcount
            if interrupted:
                db.execute("UPDATE astra_live SET status='error',error='A review was interrupted; inspect and resume' WHERE id=1")
        while not stop.is_set():
            try:
                enqueue(db)
                item = claim(db)
                if item:
                    review(db,item)
                else:
                    stop.wait(3)
            except Exception:
                with db:
                    db.execute("UPDATE astra_live SET status='error',error='Review queue unavailable; requests stopped' WHERE id=1")
                stop.wait(3)
    finally:
        db.close()
