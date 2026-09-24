"""Additive relational index of observed chain records and decision evidence.

This is an indexed-window dataset, not a complete chain replica. Unknown fields
stay NULL. Backfill is bounded and resumable; conflicting block identities stop it.
"""
import hashlib
import json
import sqlite3
import time
from . import evidence


def schema(db):
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS data_blocks(
      hash TEXT PRIMARY KEY, chain_id INTEGER NOT NULL CHECK(chain_id=4663),
      number INTEGER NOT NULL UNIQUE, parent_hash TEXT, timestamp REAL,
      source TEXT NOT NULL, recorded_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS data_accounts(address TEXT PRIMARY KEY,
      is_contract INTEGER CHECK(is_contract IN (0,1)), classification_block INTEGER);
    CREATE TABLE IF NOT EXISTS data_transactions(
      tx TEXT PRIMARY KEY, block_hash TEXT NOT NULL REFERENCES data_blocks(hash),
      tx_index INTEGER, sender TEXT REFERENCES data_accounts(address),
      recipient TEXT REFERENCES data_accounts(address), status INTEGER,
      gas_used_atomic TEXT, gas_price_atomic TEXT, receipt_gas_fee_wei TEXT, value_wei TEXT,
      receipt_available INTEGER NOT NULL DEFAULT 0 CHECK(receipt_available IN (0,1)));
    CREATE TABLE IF NOT EXISTS data_logs(
      tx TEXT NOT NULL REFERENCES data_transactions(tx), log_index INTEGER NOT NULL,
      address TEXT NOT NULL REFERENCES data_accounts(address), kind TEXT NOT NULL,
      raw_sha256 TEXT NOT NULL, PRIMARY KEY(tx,log_index));
    CREATE TABLE IF NOT EXISTS data_contracts(
      address TEXT PRIMARY KEY REFERENCES data_accounts(address), runtime_code TEXT NOT NULL,
      code_sha256 TEXT NOT NULL, observed_block INTEGER, recorded_at REAL NOT NULL,
      source_status TEXT, source_provider TEXT, source_checked_at REAL, abi_json TEXT);
    CREATE TABLE IF NOT EXISTS data_balances(
      address TEXT NOT NULL REFERENCES data_accounts(address),
      token TEXT NOT NULL REFERENCES data_accounts(address),
      block_hash TEXT NOT NULL REFERENCES data_blocks(hash), amount_atomic TEXT NOT NULL,
      source TEXT NOT NULL CHECK(source='transfer_ledger_and_balanceOf'),
      PRIMARY KEY(address,token,block_hash));
    CREATE TABLE IF NOT EXISTS data_sync(
      source TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL DEFAULT 0,
      updated_at REAL, error TEXT);
    CREATE TABLE IF NOT EXISTS evidence_measurements(
      decision_id INTEGER NOT NULL REFERENCES launch_decisions(id), metric_id TEXT NOT NULL,
      schema_version INTEGER NOT NULL, value_json TEXT NOT NULL, status TEXT NOT NULL,
      reason TEXT, source TEXT, observed_at REAL, block_number INTEGER,
      block_hash TEXT REFERENCES data_blocks(hash), unit TEXT NOT NULL, scope TEXT NOT NULL,
      PRIMARY KEY(decision_id,metric_id));
    CREATE TABLE IF NOT EXISTS evidence_findings(
      decision_id INTEGER NOT NULL REFERENCES launch_decisions(id), ordinal INTEGER NOT NULL,
      code TEXT NOT NULL, evidence_ids TEXT NOT NULL, detail TEXT NOT NULL,
      follow_up TEXT NOT NULL, severity TEXT NOT NULL, PRIMARY KEY(decision_id,ordinal));
    CREATE TABLE IF NOT EXISTS evidence_refresh_requests(
      token TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
      requested_at REAL NOT NULL, attempted_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
      requested_by TEXT NOT NULL, detail TEXT, PRIMARY KEY(token,action));
    CREATE INDEX IF NOT EXISTS evidence_refresh_queue ON evidence_refresh_requests(status,requested_at);
    INSERT OR IGNORE INTO data_sync(source) VALUES('chain_events'),('chain_receipts');
    ''')


def integer(value):
    return int(value,16) if isinstance(value,str) and value.startswith('0x') else int(value) if value is not None else None


def account(db,address):
    if address:
        address=address.lower()
        db.execute('INSERT OR IGNORE INTO data_accounts(address) VALUES(?)',(address,))
    return address


def block(db,number,hash_,header=None):
    if hash_ is None:return
    header=header or {};parent=header.get('parentHash');stamp=integer(header.get('timestamp'))
    old=db.execute('SELECT * FROM data_blocks WHERE hash=? OR number=?',(hash_,number)).fetchone()
    if old and (old['hash']!=hash_ or old['number']!=number or
        (old['parent_hash'] and parent and old['parent_hash']!=parent) or
        (old['timestamp'] is not None and stamp is not None and old['timestamp']!=stamp)):
        raise ValueError('Relational block identity conflict; backfill cursor preserved')
    db.execute('''INSERT INTO data_blocks VALUES(?,4663,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET
      parent_hash=COALESCE(data_blocks.parent_hash,excluded.parent_hash),
      timestamp=COALESCE(data_blocks.timestamp,excluded.timestamp),
      source=CASE WHEN excluded.parent_hash IS NOT NULL THEN excluded.source ELSE data_blocks.source END''',
      (hash_,number,parent,stamp,'rpc_header' if parent else 'rpc_log_or_observation',time.time()))


def transaction(db,tx,hash_,index=None):
    old=db.execute('SELECT block_hash FROM data_transactions WHERE tx=?',(tx,)).fetchone()
    if old and old['block_hash']!=hash_:raise ValueError('Relational transaction/block conflict')
    db.execute('''INSERT INTO data_transactions(tx,block_hash,tx_index) VALUES(?,?,?)
      ON CONFLICT(tx) DO UPDATE SET tx_index=COALESCE(data_transactions.tx_index,excluded.tx_index)''',(tx,hash_,index))


def log(db,row):
    raw=json.loads(row['raw']);hash_=row['block_hash'];tx=row['tx']
    if raw.get('blockHash')!=hash_ or raw.get('transactionHash')!=tx or integer(raw.get('blockNumber'))!=row['block'] or raw.get('removed'):
        raise ValueError('Relational log identity mismatch')
    stamp=integer(raw.get('blockTimestamp'))
    # Some historical providers emit 0x0 as a missing log timestamp. It is not a
    # conflicting observation of the block's actual time; preserve the raw log.
    block(db,row['block'],hash_,{'timestamp':stamp if stamp and stamp>0 else None})
    transaction(db,tx,hash_,integer(raw.get('transactionIndex')))
    address=account(db,row['address'])
    digest=hashlib.sha256(row['raw'].encode()).hexdigest()
    old=db.execute('SELECT raw_sha256 FROM data_logs WHERE tx=? AND log_index=?',(tx,row['log_index'])).fetchone()
    if old and old[0]!=digest:raise ValueError('Relational log content conflict')
    db.execute('INSERT OR IGNORE INTO data_logs VALUES(?,?,?,?,?)',(tx,row['log_index'],address,row['kind'],digest))


def receipt(db,tx,raw):
    r=json.loads(raw)
    if r.get('transactionHash')!=tx or not r.get('blockHash'):raise ValueError('Relational receipt identity mismatch')
    block(db,integer(r['blockNumber']),r['blockHash'])
    transaction(db,tx,r['blockHash'],integer(r.get('transactionIndex')))
    sender=account(db,r.get('from'));recipient=account(db,r.get('to'))
    gas=integer(r.get('gasUsed'));price=integer(r.get('effectiveGasPrice'))
    # Preserve exact integers. Receipt gas cost excludes any separately charged L1 fee.
    fee=str(gas*price) if gas is not None and price is not None else None
    db.execute('''UPDATE data_transactions SET sender=?,recipient=?,status=?,gas_used_atomic=?,
      gas_price_atomic=?,receipt_gas_fee_wei=?,receipt_available=1 WHERE tx=?''',
      (sender,recipient,integer(r.get('status')),str(gas) if gas is not None else None,
       str(price) if price is not None else None,fee,tx))


def balances(db,token,hash_,values):
    account(db,token)
    for address,amount in values:
        if type(amount) is not int or amount<0:raise ValueError('Invalid atomic holder balance')
        account(db,address)
        db.execute('INSERT OR IGNORE INTO data_balances VALUES(?,?,?,?,?)',
          (address,token,hash_,str(amount),'transfer_ledger_and_balanceOf'))


def contract(db,row):
    meta=json.loads(row['metadata'] or '{}');code=meta.get('bytecode')
    if not code or code=='0x':return
    account(db,row['token'])
    db.execute('UPDATE data_accounts SET is_contract=1,classification_block=? WHERE address=?',(meta.get('bytecode_block',row['first_block']),row['token']))
    source=db.execute('SELECT checked_at,evidence FROM chain_source_checks WHERE token=?',(row['token'],)).fetchone()
    s=json.loads(source['evidence']) if source else {}
    doc=db.execute('SELECT raw FROM chain_source_documents WHERE token=?',(row['token'],)).fetchone()
    abi=json.loads(doc[0]).get('abi') if doc else None
    db.execute('''INSERT INTO data_contracts VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET
      runtime_code=excluded.runtime_code,code_sha256=excluded.code_sha256,observed_block=excluded.observed_block,
      recorded_at=excluded.recorded_at,source_status=excluded.source_status,source_provider=excluded.source_provider,
      source_checked_at=excluded.source_checked_at,abi_json=excluded.abi_json''',
      (row['token'],code,hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
       meta.get('bytecode_block',row['first_block']),time.time(),s.get('status'),s.get('provider'),
       source['checked_at'] if source else None,json.dumps(abi) if abi is not None else None))


def save_evidence(db,decision_id,doc,quality):
    # Invalid observations remain in the raw assessment and findings, never typed measurement rows.
    if quality['valid']:
        for key,m in doc['measurements'].items():
            if m['block_hash'] is not None:
                block(db,m['block_number'],m['block_hash'])
            db.execute('INSERT OR IGNORE INTO evidence_measurements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              (decision_id,key,evidence.VERSION,json.dumps(m['value'],allow_nan=False),m['status'],m['reason'],m['source'],
               m['observed_at'],m['block_number'],m['block_hash'],m['unit'],m['scope']))
    for i,f in enumerate(quality['findings']):
        db.execute('INSERT OR IGNORE INTO evidence_findings VALUES(?,?,?,?,?,?,?)',
          (decision_id,i,f['code'],json.dumps(f['evidence_ids']),f['detail'],f['follow_up'],f['severity']))


def request_refresh(db,token,action,now,requested_by='validator'):
    if action not in evidence.ACTIONS or action=='none':return False
    row=db.execute('SELECT * FROM evidence_refresh_requests WHERE token=? AND action=?',(token,action)).fetchone()
    if row and (row['status'] in ('queued','running') or row['attempts']>=3 or now-(row['attempted_at'] or row['requested_at'])<300):return False
    db.execute('''INSERT INTO evidence_refresh_requests(token,action,status,requested_at,requested_by)
      VALUES(?,?,'queued',?,?) ON CONFLICT(token,action) DO UPDATE SET
      status='queued',requested_at=excluded.requested_at,requested_by=excluded.requested_by,detail=NULL''',
      (token,action,now,requested_by))
    return True


def refresh_actions(db,token,now):
    rows=db.execute("SELECT action FROM evidence_refresh_requests WHERE token=? AND status='queued' AND attempts<3",(token,)).fetchall()
    with db:
        db.execute("UPDATE evidence_refresh_requests SET status='running',attempted_at=?,attempts=attempts+1 WHERE token=? AND status='queued' AND attempts<3",(now,token))
    return {r[0] for r in rows}


def finish_refresh(db,token,quality,now):
    unresolved={f['follow_up'] for f in quality['findings']} if quality['valid'] else set(evidence.ACTIONS)
    with db:
        for r in db.execute("SELECT action FROM evidence_refresh_requests WHERE token=? AND status='running'",(token,)).fetchall():
            still=r['action'] in unresolved
            db.execute('UPDATE evidence_refresh_requests SET status=?,detail=? WHERE token=? AND action=?',
              ('unavailable' if still else 'completed','Collector attempted; evidence remains unresolved' if still else 'Requested evidence refreshed',token,r['action']))
        for f in quality['findings']:request_refresh(db,token,f['follow_up'],now)


def sync(db,limit=500):
    for table,callback in [('chain_events',log),('chain_receipts',lambda db,r:receipt(db,r['tx'],r['raw']))]:
        with db:
            db.execute('INSERT OR IGNORE INTO data_sync(source) VALUES(?)',(table,))
            cursor=db.execute('SELECT last_rowid FROM data_sync WHERE source=?',(table,)).fetchone()[0]
            rows=db.execute(f'SELECT rowid AS source_rowid,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,limit)).fetchall()
            for row in rows:callback(db,row)
            if rows:db.execute('UPDATE data_sync SET last_rowid=?,updated_at=?,error=NULL WHERE source=?',(rows[-1]['source_rowid'],time.time(),table))
    with db:
        rows=db.execute('''SELECT t.* FROM chain_tokens t LEFT JOIN data_contracts c ON c.address=t.token
          LEFT JOIN chain_source_checks s ON s.token=t.token WHERE t.code_hash IS NOT NULL
          AND (c.address IS NULL OR COALESCE(s.checked_at,0)>COALESCE(c.source_checked_at,0)) LIMIT 20''').fetchall()
        for row in rows:contract(db,row)


def state(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='data_blocks'").fetchone():return None
    counts={name:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for name,table in
      [('blocks','data_blocks'),('transactions','data_transactions'),('accounts','data_accounts'),('contracts','data_contracts'),
       ('balances','data_balances'),('logs','data_logs'),('measurements','evidence_measurements')]}
    cursors=[]
    for r in db.execute('SELECT * FROM data_sync'):
        item=dict(r);table=r['source']
        if table in ('chain_events','chain_receipts'):
            item['pending']=db.execute(f'SELECT COUNT(*) FROM {table} WHERE rowid>?',(r['last_rowid'],)).fetchone()[0]
        cursors.append(item)
    receipts=db.execute('SELECT COUNT(*) FROM data_transactions WHERE receipt_available=1').fetchone()[0]
    return dict(version=1,counts=counts,receipts_available=receipts,sync=cursors,
      refresh_counts=dict(db.execute('SELECT status,COUNT(*) FROM evidence_refresh_requests GROUP BY status')),
      refresh_recent=[dict(r) for r in db.execute('SELECT * FROM evidence_refresh_requests ORDER BY COALESCE(attempted_at,requested_at) DESC LIMIT 15')],
      coverage='Observed canonical pool and mint logs, cached receipts, and verified holder balances; not full-chain or complete wallet history.')


def worker(path,stop):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row
    try:
        schema(db)
        with db:db.execute("UPDATE evidence_refresh_requests SET status='unavailable',detail='Collector interrupted; bounded retry allowed after cooldown' WHERE status='running'")
        while not stop.is_set():
            try:
                sync(db)
                with db:
                    db.execute("UPDATE evidence_refresh_requests SET status='unavailable',detail='Collector timed out; unresolved evidence remains blocking' WHERE status='running' AND attempted_at<?",(time.time()-300,))
                    db.execute("UPDATE evidence_refresh_requests SET status='unavailable',attempted_at=?,detail='No eligible pool assessment completed within ten minutes' WHERE status='queued' AND requested_at<?",(time.time(),time.time()-600))
            except Exception as exc:
                with db:db.execute('UPDATE data_sync SET error=?',(str(exc)[:180] if isinstance(exc,ValueError) else type(exc).__name__,))
                stop.wait(5)
            stop.wait(.5)
    finally:db.close()
