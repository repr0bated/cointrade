"""Reconstruct complete transaction-origin cash flows from receipts and call traces."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from . import evm, wallets
from .store import Store


def native_delta(trace,wallet):
    if not isinstance(trace,dict) or trace.get('error'):
        raise ValueError('Failed or missing root trace')
    delta=0
    def visit(frame):
        nonlocal delta
        if frame.get('error'):
            return
        kind=frame.get('type','').upper()
        if kind in ('CALL','CREATE','CREATE2','SELFDESTRUCT'):
            value=int(frame.get('value','0x0'),16)
            if frame.get('from','').lower()==wallet:delta-=value
            if frame.get('to','').lower()==wallet:delta+=value
        for child in frame.get('calls',[]):visit(child)
    visit(trace)
    return delta


def flow(receipt,trace):
    if int(receipt.get('status','0x0'),16)!=1:
        raise ValueError('Transaction reverted')
    origin=receipt['from'].lower()
    if trace.get('from','').lower()!=origin:
        raise ValueError('Trace origin mismatch')
    if any(l.get('topics') and l['topics'][0] in evm.ACTIVITY[len(evm.SWAPS):] for l in receipt['logs']):
        raise ValueError('Mixed liquidity and swap transaction')
    assets={l['address'].lower() for l in receipt['logs'] if len(l['topics'])==3 and l['topics'][0]==evm.TRANSFER}
    from .simulation import V2_ROUTER,V3_ROUTER,V4_ROUTER,PERMIT2
    excluded={evm.WETH,evm.V4,V2_ROUTER,V3_ROUTER,V4_ROUTER,PERMIT2,evm.ZERO,''}
    excluded.update(l['address'].lower() for l in receipt['logs'] if l.get('topics') and l['topics'][0] in evm.SWAPS)
    candidates={origin,(receipt.get('to') or '').lower()}-excluded
    matches=[]
    for wallet in candidates:
        net={t:evm.transfers(receipt['logs'],t,wallet) for t in assets}
        quote=native_delta(trace,wallet)+net.pop(evm.WETH,0)
        tokens={t:q for t,q in net.items() if q}
        if len(tokens)!=1:continue
        token,quantity=next(iter(tokens.items()))
        if quantity*quote<0:
            if wallet==origin:return wallet,token,quantity,quote
            matches.append((wallet,token,quantity,quote))
    if len(matches)!=1:
        raise ValueError('Cannot isolate one holder with opposing token and ETH/WETH flows')
    return matches[0]


def schema(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS chain_receipts(tx TEXT PRIMARY KEY,raw TEXT);
    CREATE TABLE IF NOT EXISTS chain_traces(tx TEXT PRIMARY KEY,raw TEXT);
    CREATE TABLE IF NOT EXISTS wallet_reviews(tx TEXT PRIMARY KEY,ts REAL,status TEXT,reason TEXT,trace TEXT);
    CREATE TABLE IF NOT EXISTS wallet_decoder_version(id INTEGER PRIMARY KEY,version INTEGER);
    CREATE INDEX IF NOT EXISTS chain_events_order ON chain_events(block,log_index);
    CREATE INDEX IF NOT EXISTS chain_events_address_block ON chain_events(address,block);
    CREATE TABLE IF NOT EXISTS live_trade_observations(
      tx TEXT PRIMARY KEY,block INTEGER,ts REAL,status TEXT,wallet TEXT,token TEXT,side TEXT,reason TEXT);
    CREATE INDEX IF NOT EXISTS live_trade_token_time ON live_trade_observations(token,ts);
    CREATE TABLE IF NOT EXISTS live_attribution_status(id INTEGER PRIMARY KEY CHECK(id=1),
      updated_at REAL,reviewed INTEGER DEFAULT 0,error TEXT);
    CREATE INDEX IF NOT EXISTS launch_decisions_time_token ON launch_decisions(ts,token);
    ''')


def save_recent(db,row,receipt,trace):
    """Receipt-bound attribution, independent of historical FIFO bookkeeping."""
    if receipt.get('transactionHash')!=row['tx'] or receipt.get('blockHash')!=row['block_hash']:
        raise ValueError('Recent receipt provenance mismatch')
    if int(receipt['blockNumber'],16)!=row['block']:
        raise ValueError('Recent receipt height mismatch')
    wallet=asset=side=None;status='complete';reason=None
    try:
        wallet,asset,quantity,quote=flow(receipt,trace)
        if not db.execute('SELECT 1 FROM chain_swaps WHERE tx=? AND token=?',(row['tx'],asset)).fetchone():
            raise ValueError('Wallet asset not in indexed swap pools')
        side='buy' if quantity>0 else 'sell'
    except ValueError as exc:status='ambiguous';reason=str(exc)
    with db:
        db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(row['tx'],json.dumps(receipt)))
        db.execute('INSERT OR IGNORE INTO chain_traces VALUES(?,?)',(row['tx'],json.dumps(trace)))
        db.execute('INSERT OR IGNORE INTO live_trade_observations VALUES(?,?,?,?,?,?,?,?)',
                   (row['tx'],row['block'],row['ts'],status,wallet,asset,side,reason))
    return status


def fast_tick(db,rpc,now=None,limit=12):
    now=time.time() if now is None else now
    # Begin at pool discovery, before slow contract enrichment. Spread the
    # bounded work across tokens needing two verified recent buyers.
    rows=db.execute('''WITH candidates AS (
      SELECT s.tx,MIN(s.ts) ts,MIN(e.block) block,MIN(e.block_hash) block_hash,
        MIN(s.token) token,MIN(CASE s.side WHEN 'buy' THEN 0 ELSE 1 END) sell
      FROM chain_swaps s JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
      LEFT JOIN live_trade_observations o ON o.tx=s.tx
      WHERE s.ts BETWEEN ? AND ? AND o.tx IS NULL
        AND (s.token IN(SELECT token FROM launch_decisions WHERE ts>=? GROUP BY token)
          OR EXISTS(SELECT 1 FROM chain_pools p WHERE p.pool=s.pool AND p.ts>=?))
        AND e.block<=(SELECT height FROM chain_cursor WHERE id=1)
      GROUP BY s.tx), ranked AS (
        SELECT c.*,ROW_NUMBER() OVER(PARTITION BY token ORDER BY sell,ts DESC) token_rank,
          (SELECT COUNT(DISTINCT wallet) FROM live_trade_observations o WHERE o.token=c.token
            AND o.ts>=? AND o.ts<=? AND o.status='complete' AND o.side='buy') buyers,
          EXISTS(SELECT 1 FROM launch_decisions d WHERE d.token=c.token AND d.ts>=?) assessed
        FROM candidates c)
      SELECT * FROM ranked ORDER BY (buyers>=2),sell,assessed DESC,token_rank,ts DESC LIMIT ?''',
      (now-90,now,now-120,now-900,now-60,now,now-120,limit)).fetchall()
    blocks={}
    for r in rows:blocks.setdefault(r['block'],[]).append(r)
    reviewed=0
    for height,items in list(blocks.items())[:4]:
        receipts={};traces={}
        for r in items:
            old=db.execute('SELECT raw FROM chain_receipts WHERE tx=?',(r['tx'],)).fetchone()
            trace=db.execute('SELECT raw FROM chain_traces WHERE tx=?',(r['tx'],)).fetchone()
            if old:receipts[r['tx']]=json.loads(old[0])
            if trace:traces[r['tx']]=json.loads(trace[0])
        if len(items)>1 and (len(receipts)<len(items) or len(traces)<len(items)):
            try:
                batch=rpc.call('eth_getBlockReceipts',[hex(height)])
                traced=rpc.call('debug_traceBlockByNumber',[hex(height),{'tracer':'callTracer'}])
                receipts.update({r['transactionHash']:r for r in batch})
                traces.update({t['txHash']:t['result'] for t in traced if isinstance(t.get('result'),dict)})
            except ValueError:
                pass  # Bounded individual reads also handle oversized block responses.
        for r in items:
            receipt=receipts.get(r['tx'])
            if receipt is None:receipt=rpc.call('eth_getTransactionReceipt',[r['tx']])
            trace=traces.get(r['tx'])
            if trace is None:trace=rpc.call('debug_traceTransaction',[r['tx'],{'tracer':'callTracer'}])
            save_recent(db,r,receipt,trace);reviewed+=1
    with db:
        db.execute('''INSERT INTO live_attribution_status VALUES(1,?,?,NULL)
          ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,
          reviewed=live_attribution_status.reviewed+excluded.reviewed,error=NULL''',(time.time(),reviewed))
    return reviewed


def fast_worker(path,config,monitor):
    store=Store(path,config,'robinhood');rpc=evm.RPC()
    try:
        schema(store.db)
        while not monitor.stop.is_set():
            try:
                count=fast_tick(store.db,rpc)
                monitor.update(live_attribution_error=None,live_attribution_updated=time.time())
            except Exception as exc:
                count=0
                message=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__
                monitor.update(live_attribution_error=message)
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO live_attribution_status VALUES(1,?,COALESCE((SELECT reviewed FROM live_attribution_status WHERE id=1),0),?)',(time.time(),message))
            monitor.stop.wait(.5 if count else 2)
    finally:store.db.close()


def get_trace(db,rpc,tx):
    cached=db.execute('SELECT raw FROM chain_traces WHERE tx=?',(tx,)).fetchone()
    if cached:return json.loads(cached['raw'])
    trace=rpc.call('debug_traceTransaction',[tx,{'tracer':'callTracer'}])
    with db:db.execute('INSERT OR IGNORE INTO chain_traces VALUES(?,?)',(tx,json.dumps(trace)))
    return trace


def recent_buyers(db,rpc,token,height,observed,limit=4):
    """Prioritize a bounded set of recent trades without changing FIFO history order."""
    rows=db.execute('''SELECT s.tx,MIN(s.ts) ts,MIN(e.block) block,MIN(e.block_hash) block_hash
      FROM chain_swaps s JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
      LEFT JOIN live_trade_observations o ON o.tx=s.tx
      WHERE s.token=? AND s.ts BETWEEN ? AND ? AND e.block<=? AND o.tx IS NULL
      GROUP BY s.tx ORDER BY block DESC LIMIT ?''',(token,observed-300,observed,height,limit)).fetchall()
    errors=[]
    for row in rows:
        try:
            cached=db.execute('SELECT raw FROM chain_receipts WHERE tx=?',(row['tx'],)).fetchone()
            receipt=json.loads(cached['raw']) if cached else rpc.call('eth_getTransactionReceipt',[row['tx']])
            if receipt['blockHash']!=row['block_hash']:raise ValueError('Recent receipt block mismatch')
            trace=get_trace(db,rpc,row['tx'])
        except (ValueError,KeyError,TypeError) as exc:
            errors.append(str(exc)[:160]);continue
        wallet=asset=side=None;status='complete';reason=None
        try:
            wallet,asset,quantity,quote=flow(receipt,trace)
            if not db.execute('SELECT 1 FROM chain_swaps WHERE tx=? AND token=?',(row['tx'],asset)).fetchone():
                raise ValueError('Wallet asset not in indexed swap pools')
            side='buy' if quantity>0 else 'sell'
        except ValueError as exc:status='ambiguous';reason=str(exc)
        with db:
            db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(row['tx'],json.dumps(receipt)))
            db.execute('INSERT OR IGNORE INTO live_trade_observations VALUES(?,?,?,?,?,?,?,?)',
              (row['tx'],row['block'],row['ts'],status,wallet,asset,side,reason))
    args=(token,observed-300,observed,height)
    buyers={r[0] for r in db.execute('''SELECT wallet FROM live_trade_observations
      WHERE token=? AND ts BETWEEN ? AND ? AND block<=? AND status='complete' AND side='buy' ''',args)}
    buyers.update(r[0] for r in db.execute('''SELECT DISTINCT s.wallet FROM chain_swaps s
      JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
      WHERE s.token=? AND s.ts BETWEEN ? AND ? AND e.block<=? AND s.attributed=1 AND s.side='buy' ''',args))
    counts=db.execute('''SELECT COUNT(DISTINCT s.tx),COUNT(DISTINCT CASE WHEN o.tx IS NOT NULL OR w.tx IS NOT NULL THEN s.tx END),
      COUNT(DISTINCT CASE WHEN o.status='ambiguous' OR w.status='ambiguous' THEN s.tx END)
      FROM chain_swaps s JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
      LEFT JOIN live_trade_observations o ON o.tx=s.tx LEFT JOIN wallet_reviews w ON w.tx=s.tx
      WHERE s.token=? AND s.ts BETWEEN ? AND ? AND e.block<=?''',args).fetchone()
    return sorted(w for w in buyers if w),dict(window_seconds=300,block=height,
      transactions=counts[0],reviewed=counts[1],pending=counts[0]-counts[1],ambiguous=counts[2],
      errors=errors,scope='Verified recent buyers are a lower bound while transactions remain pending or ambiguous. Historical P&L backfill runs separately.')


def initialize(db):
    schema(db)
    # Versioned rebuild only replaces derived accounting; immutable events remain intact.
    if not db.execute('SELECT 1 FROM wallet_decoder_version WHERE id=1').fetchone():
        with db:
            for name in ('wallet_flows','wallet_lots','wallet_outcomes','wallet_unknown','wallet_reviews'):
                db.execute('DELETE FROM '+name)
            db.execute('UPDATE chain_swaps SET attributed=0')
            db.execute('INSERT INTO wallet_decoder_version VALUES(1,2)')


def pending(db,limit=1):
    return db.execute('''SELECT s.tx,s.ts,e.block,e.log_index,e.block_hash
      FROM chain_swaps s JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
      LEFT JOIN wallet_reviews w ON w.tx=s.tx WHERE w.tx IS NULL
      ORDER BY e.block,e.log_index LIMIT ?''',(limit,)).fetchall()


def prefetch(db,rpc,executor):
    """Fetch blocks concurrently; FIFO accounting still runs in chain order."""
    rows=pending(db,96)
    blocks={}
    for row in rows:
        cached=db.execute('''SELECT 1 FROM chain_receipts r JOIN chain_traces t ON t.tx=r.tx
          WHERE r.tx=?''',(row['tx'],)).fetchone()
        if not cached:blocks[row['block']]=row['block_hash']
    def fetch(item):
        height,expected=item
        # Each thread owns its RPC object; the shared limiter applies to all workers.
        source=evm.RPC()
        receipts=source.call('eth_getBlockReceipts',[hex(height)])
        traces=source.call('debug_traceBlockByNumber',[hex(height),{'tracer':'callTracer'}])
        if not receipts or any(r['blockHash']!=expected for r in receipts):
            raise ValueError('Prefetched block no longer matches indexed block')
        return receipts,traces
    futures=[executor.submit(fetch,item) for item in list(blocks.items())[:48]]
    for future in futures:
        try:receipts,traces=future.result()
        except ValueError:continue  # Individual bounded reads remain the fallback.
        targets={r['transactionHash'] for r in receipts}
        with db:
            for receipt in receipts:
                db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(receipt['transactionHash'],json.dumps(receipt)))
            for item in traces:
                if item.get('txHash') in targets and isinstance(item.get('result'),dict):
                    db.execute('INSERT OR IGNORE INTO chain_traces VALUES(?,?)',(item['txHash'],json.dumps(item['result'])))


def process_next(db,rpc):
    rows=pending(db)
    row=rows[0] if rows else None
    if not row:return False
    tx=row['tx']
    targets={r[0] for r in db.execute('''SELECT DISTINCT s.tx FROM chain_swaps s
      JOIN chain_events e ON e.tx=s.tx AND e.log_index=s.log_index
      WHERE e.block=? AND s.tx NOT IN (SELECT tx FROM chain_traces)''',(row['block'],))}
    if len(targets)>1:
        try:
            receipts=rpc.call('eth_getBlockReceipts',[hex(row['block'])])
            traces=rpc.call('debug_traceBlockByNumber',[hex(row['block']),{'tracer':'callTracer'}])
        except ValueError:
            receipts,traces=[],[]  # Fall back to individual bounded responses below.
        with db:
            for receipt in receipts:
                if receipt['transactionHash'] in targets:
                    db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(receipt['transactionHash'],json.dumps(receipt)))
            for item in traces:
                if item.get('txHash') in targets and isinstance(item.get('result'),dict):
                    db.execute('INSERT OR IGNORE INTO chain_traces VALUES(?,?)',(item['txHash'],json.dumps(item['result'])))
    cached=db.execute('SELECT raw FROM chain_receipts WHERE tx=?',(tx,)).fetchone()
    receipt=json.loads(cached['raw']) if cached else rpc.call('eth_getTransactionReceipt',[tx])
    expected=db.execute('SELECT block_hash FROM chain_events WHERE tx=? LIMIT 1',(tx,)).fetchone()[0]
    if receipt['blockHash']!=expected:raise ValueError('Receipt no longer matches indexed block')
    trace=get_trace(db,rpc,tx)
    status,reason='complete',None
    try:
        wallet,token,quantity,quote=flow(receipt,trace)
        if not db.execute('SELECT 1 FROM chain_swaps WHERE tx=? AND token=?',(tx,token)).fetchone():
            raise ValueError('Wallet asset not in indexed swap pools')
    except ValueError as exc:
        status,reason='ambiguous',str(exc)
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM wallet_reviews WHERE tx=?',(tx,)).fetchone():return True
        db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(tx,json.dumps(receipt)))
        db.execute('UPDATE chain_swaps SET wallet=? WHERE tx=?',(receipt['from'].lower(),tx))
        if status=='complete':
            side='buy' if quantity>0 else 'sell'
            rank=db.execute("SELECT COUNT(DISTINCT wallet) FROM wallet_flows WHERE token=? AND side='buy' AND ts<=?",(token,row['ts'])).fetchone()[0]+1
            gas=int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16)
            wallets.record(db,tx,wallet,token,row['ts'],str(quantity),str(Decimal(quote)/10**18),str(Decimal(gas)/10**18),rank)
            db.execute("UPDATE wallet_flows SET attribution='receipt_and_call_trace' WHERE tx=?",(tx,))
            db.execute('UPDATE chain_swaps SET attributed=1,wallet=? WHERE tx=? AND token=? AND side=?',(wallet,tx,token,side))
        else:
            first=db.execute('SELECT token,wallet FROM chain_swaps WHERE tx=? LIMIT 1',(tx,)).fetchone()
            db.execute('INSERT OR IGNORE INTO wallet_unknown VALUES(?,?,?,?)',(tx,first['wallet'],first['token'],reason))
        db.execute('INSERT INTO wallet_reviews VALUES(?,?,?,?,?)',(tx,time.time(),status,reason,json.dumps(trace)))
    return True


def worker(path,config,monitor):
    store=Store(path,config,'robinhood');rpc=evm.RPC()
    # Leave RPC capacity for current buyers, execution quotes, and replay.
    executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='wallet-read')
    try:
        initialize(store.db)
        while not monitor.stop.is_set():
            try:
                tables={r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'wallet_history_jobs' in tables and 'heartbeat' in {r[1] for r in store.db.execute('PRAGMA table_info(wallet_history_jobs)')}:
                    if store.db.execute("SELECT 1 FROM wallet_history_jobs WHERE status IN ('scanning','transactions') AND heartbeat>?",(time.time()-90,)).fetchone():
                        monitor.update(wallet_status='targeted_history_audit')
                        monitor.stop.wait(2)
                        continue
                monitor.update(wallet_status='index_backfill')
                row=pending(store.db)
                if row and not store.db.execute('''SELECT 1 FROM chain_receipts r JOIN chain_traces t ON t.tx=r.tx
                  WHERE r.tx=?''',(row[0]['tx'],)).fetchone():
                    prefetch(store.db,rpc,executor)
                if not process_next(store.db,rpc):monitor.stop.wait(2)
                monitor.update(wallet_error=None)
            except Exception as exc:
                monitor.update(wallet_error=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__)
                monitor.stop.wait(10)
    finally:
        executor.shutdown(wait=False,cancel_futures=True)
        store.db.close()
