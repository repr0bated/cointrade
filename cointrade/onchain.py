"""Resumable confirmed-log indexer: detect launches, observe wallets, paper trade."""
from decimal import Decimal, localcontext
import hashlib
import json
import time

from . import evm, wallets, enrichment, attribution, quotes, contracts, simulation, blockscout, evidence, ledger
from .engine import Engine
from .launchrisk import assess, decide, screen_launch, security, candidate_score
from .providers import request_json


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS chain_cursor (
        id INTEGER PRIMARY KEY CHECK(id=1), chain_id INTEGER, start_block INTEGER,
        height INTEGER, hash TEXT, confirmations INTEGER);
      CREATE TABLE IF NOT EXISTS chain_events (
        tx TEXT, log_index INTEGER, block INTEGER, block_hash TEXT, kind TEXT,
        address TEXT, raw TEXT, PRIMARY KEY(tx,log_index));
      CREATE TABLE IF NOT EXISTS chain_tokens (
        token TEXT PRIMARY KEY, first_block INTEGER, first_ts REAL,
        deployment_confirmed INTEGER, decimals INTEGER, code_hash TEXT,
        owner TEXT, metadata TEXT);
      CREATE TABLE IF NOT EXISTS chain_pools (
        pool TEXT PRIMARY KEY, version INTEGER, token0 TEXT, token1 TEXT, fee INTEGER,
        hooks TEXT, block INTEGER, ts REAL, launch_tx_sender TEXT, tx TEXT);
      CREATE TABLE IF NOT EXISTS chain_swaps (
        tx TEXT, log_index INTEGER, pool TEXT, token TEXT, wallet TEXT, ts REAL,
        side TEXT, token_amount TEXT, quote_amount TEXT, attributed INTEGER,
        PRIMARY KEY(tx,log_index));
      CREATE INDEX IF NOT EXISTS chain_swaps_pool_time ON chain_swaps(pool,ts);
      CREATE TABLE IF NOT EXISTS chain_risks (
        token TEXT PRIMARY KEY, checked_at REAL, raw TEXT);
      CREATE TABLE IF NOT EXISTS launch_decisions (
        id INTEGER PRIMARY KEY, ts REAL, token TEXT, pool TEXT, action TEXT,
        score REAL, reasons TEXT, evidence TEXT);
      CREATE TABLE IF NOT EXISTS chain_holder_checks(token TEXT PRIMARY KEY, checked_at REAL, evidence TEXT);
      CREATE TABLE IF NOT EXISTS chain_creators(token TEXT PRIMARY KEY,deployer TEXT,initiator TEXT,tx TEXT,status TEXT);
      CREATE TABLE IF NOT EXISTS chain_source_checks(token TEXT PRIMARY KEY,checked_at REAL,evidence TEXT);
      CREATE TABLE IF NOT EXISTS chain_source_documents(token TEXT PRIMARY KEY,provider TEXT,checked_at REAL,raw TEXT);
    ''')
    wallets.schema(db)
    attribution.schema(db)
    ledger.schema(db)


def token_metadata(rpc, token, height, ts):
    code = rpc.call('eth_getCode', [token, hex(height)])
    if code == '0x':
        return None
    # Past absence establishes deployment in this block, including internal CREATE2.
    # Failure of an archive read does not magically establish deployment time.
    try:
        deployed = rpc.call('eth_getCode', [token, hex(max(0, height - 1))]) == '0x'
    except ValueError:
        deployed = False
    decimals, owner = None, None
    try:
        result = evm.words(rpc.read(token, '0x313ce567', height))
        if len(result) == 1 and 0 <= result[0] <= 36:
            decimals = result[0]
    except ValueError:
        pass
    try:
        result = evm.words(rpc.read(token, '0x8da5cb5b', height))
        if len(result) == 1:
            owner = evm.address(result[0])
    except ValueError:
        pass
    return dict(token=token, first_block=height, first_ts=ts, deployment_confirmed=int(deployed),
                decimals=decimals, code_hash=hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
                owner=owner, metadata=json.dumps({'bytecode': code, 'creator': None,
                    'creator_note': 'launch transaction sender is recorded separately; not proof of token creator'}))


class Scanner:
    def __init__(self, store, config, rpc=None, lookback=1000, confirmations=2, batch_blocks=200):
        if not 0 <= lookback <= 100000 or not 1 <= confirmations <= 1000 or not 1 <= batch_blocks <= 1000:
            raise ValueError('Use lookback 0..100000, confirmations 1..1000, batch blocks 1..1000')
        self.store, self.config, self.rpc = store, config, rpc or evm.RPC()
        self.lookback, self.confirmations, self.batch_blocks = lookback, confirmations, batch_blocks
        self.head = 0
        schema(store.db)
        self.rpc.verify()
        old = store.db.execute('SELECT * FROM chain_cursor WHERE id=1').fetchone()
        if old and (old['chain_id'] != evm.CHAIN_ID or old['confirmations'] != confirmations):
            raise ValueError('Chain or confirmation settings differ from this database')

    def ingest(self):
        db, rpc = self.store.db, self.rpc
        self.head = int(rpc.call('eth_blockNumber', []), 16)
        target = self.head - self.confirmations
        if target < 1:
            return
        cursor = db.execute('SELECT * FROM chain_cursor WHERE id=1').fetchone()
        if cursor is None:
            anchor = max(0, target - self.lookback - 1)
            block = rpc.block(anchor)
            with db:
                db.execute('INSERT INTO chain_cursor VALUES(1,?,?,?,?,?)',
                           (evm.CHAIN_ID, anchor + 1, anchor, block['hash'], self.confirmations))
            cursor = db.execute('SELECT * FROM chain_cursor WHERE id=1').fetchone()
        if rpc.block(cursor['height'])['hash'] != cursor['hash']:
            with db:
                db.execute('UPDATE account SET halted=1 WHERE id=1')
            raise ValueError('Chain reorganization detected. Paper account halted; use a new database to reindex canonical history.')
        start, end = cursor['height'] + 1, min(target, cursor['height'] + self.batch_blocks)
        if start > end:
            return
        checkpoint = rpc.block(end)
        headers = {end: checkpoint}

        def timestamp(height):
            if height not in headers:
                headers[height] = rpc.block(height)
            return int(headers[height]['timestamp'], 16)

        creation = rpc.logs(start, end, [[evm.PAIR, evm.POOL, evm.INIT4]], evm.FACTORIES)
        mints = rpc.logs(start, end, [evm.TRANSFER, evm.ZERO_TOPIC])
        # ERC721 Transfer has four topics; require the ERC20 event shape.
        mints = [l for l in mints if len(l['topics']) == 3 and len(evm.words(l['data'])) == 1
                 and l['address'].lower() != evm.WETH]
        new_pools = []
        receipts = {}

        def prefetch(logs, fetch_receipts=False):
            # Batch independent reads to avoid hundreds of serial network round trips.
            if not hasattr(rpc, 'batch'):
                return
            for log in logs:
                h = int(log['blockNumber'],16)
                if log.get('blockTimestamp') and int(log['blockTimestamp'],16)>0:
                    hinted = {'hash':log['blockHash'],'timestamp':log['blockTimestamp']}
                    if h in headers and headers[h]['hash'] != hinted['hash']:
                        raise ValueError('Conflicting log block hashes; cursor preserved')
                    headers.setdefault(h,hinted)
            heights = sorted({int(l['blockNumber'],16) for l in logs} - headers.keys())
            headers.update(zip(heights, rpc.batch('eth_getBlockByNumber', [[hex(h),False] for h in heights])))
            if fetch_receipts:
                txs = sorted({l['transactionHash'] for l in logs} - receipts.keys())
                receipts.update(zip(txs, rpc.batch('eth_getTransactionReceipt', [[tx] for tx in txs])))

        prefetch(creation + mints)

        def receipt(tx):
            if tx not in receipts:
                receipts[tx] = rpc.call('eth_getTransactionReceipt', [tx])
            return receipts[tx]

        for log in creation:
            pool = evm.pool_created(log)
            if pool:
                height = int(log['blockNumber'], 16)
                pool.update(block=height, ts=timestamp(height),
                            launch_tx_sender=None, tx=log['transactionHash'])
                new_pools.append(pool)
        existing = {r['token']: dict(r) for r in db.execute('SELECT * FROM chain_tokens')}
        candidates = {}
        for log in mints:
            token = log['address'].lower()
            height = int(log['blockNumber'], 16)
            candidates[token] = min(height, candidates.get(token, height))
        for p in new_pools:
            for token in (p['token0'], p['token1']):
                if token not in (evm.ZERO, evm.WETH):
                    candidates[token] = min(p['block'], candidates.get(token, p['block']))
        metadata = {}
        for token, height in candidates.items():
            if token not in existing:
                metadata[token]=dict(token=token,first_block=height,first_ts=timestamp(height),
                    deployment_confirmed=0,decimals=None,code_hash=None,owner=None,metadata=json.dumps({'pending':True}))
        # Follow discovered pools for their first day, and any held paper positions.
        held = {p['token'] for p in self.store.positions()}
        now_chain = timestamp(end)
        pools = {r['pool']: dict(r) for r in db.execute('SELECT * FROM chain_pools')
                 if now_chain - r['ts'] <= 86400 or r['token0'] in held or r['token1'] in held}
        pools.update({p['pool']: p for p in new_pools})
        addresses = sorted({evm.V4 if p['version'] == 4 else p['pool'] for p in pools.values()})
        activity = []
        for i in range(0, len(addresses), 50):
            activity.extend(rpc.logs(start, end, [evm.ACTIVITY], addresses[i:i+50]))
        activity = [l for l in activity if evm.pool_key(l) in pools]
        prefetch(activity)
        for log in activity:
            timestamp(int(log['blockNumber'], 16))
        all_logs = {(l['transactionHash'], int(l['logIndex'], 16)): l for l in creation + mints + activity}
        for l in all_logs.values():
            height = int(l['blockNumber'], 16)
            timestamp(height)
            if l.get('removed') or headers[height]['hash'] != l['blockHash']:
                raise ValueError('Inconsistent or removed logs; cursor preserved')
        for r in receipts.values():
            height = int(r['blockNumber'], 16)
            timestamp(height)
            if r['blockHash'] != headers[height]['hash']:
                raise ValueError('Receipt/block mismatch; cursor preserved')
        if rpc.block(end)['hash'] != checkpoint['hash']:
            raise ValueError('Chain changed during fetch; cursor preserved')
        with db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT height FROM chain_cursor WHERE id=1').fetchone()[0]
            if current != cursor['height']:
                raise ValueError('Another scanner advanced this database')
            for number, header in headers.items():
                ledger.block(db,number,header['hash'],header)
            for info in metadata.values():
                db.execute('INSERT OR IGNORE INTO chain_tokens VALUES(?,?,?,?,?,?,?,?)', tuple(info.values()))
            for tx,r in receipts.items():
                db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(tx,json.dumps(r)))
            for p in new_pools:
                db.execute('INSERT OR IGNORE INTO chain_pools VALUES(?,?,?,?,?,?,?,?,?,?)',
                           tuple(p[k] for k in ('pool','version','token0','token1','fee','hooks','block','ts','launch_tx_sender','tx')))
            for (_, index), log in sorted(all_logs.items(), key=lambda item: (int(item[1]['blockNumber'], 16), item[0][1])):
                topic = log['topics'][0]
                kind = ('pool_created' if topic in (evm.PAIR, evm.POOL, evm.INIT4) else 'token_mint'
                        if topic == evm.TRANSFER else 'swap' if topic in evm.SWAPS else 'liquidity_change')
                inserted = db.execute('INSERT OR IGNORE INTO chain_events VALUES(?,?,?,?,?,?,?)',
                                      (log['transactionHash'], index, int(log['blockNumber'], 16), log['blockHash'], kind,
                                       log['address'].lower(), json.dumps(log))).rowcount
                if inserted and topic in evm.SWAPS:
                    self.observe_swap(log, pools[evm.pool_key(log)], receipts.get(log['transactionHash']),
                                      timestamp(int(log['blockNumber'], 16)))
            db.execute('UPDATE chain_cursor SET height=?,hash=? WHERE id=1', (end, checkpoint['hash']))

    def observe_swap(self, log, pool, receipt, ts):
        db = self.store.db
        amounts = evm.swap_amounts(log)
        quote_idx = 0 if pool['token0'] in (evm.ZERO, evm.WETH) else 1 if pool['token1'] in (evm.ZERO, evm.WETH) else None
        if quote_idx is None or amounts[0] * amounts[1] >= 0:
            return
        token = pool['token1'] if quote_idx == 0 else pool['token0']
        quote_token = pool['token0'] if quote_idx == 0 else pool['token1']
        token_delta, quote_delta = -amounts[1-quote_idx], -amounts[quote_idx]
        wallet = receipt['from'].lower() if receipt else None
        side = 'buy' if token_delta > 0 else 'sell'
        attributed = False  # Rebuilt once per transaction by the trace worker.
        db.execute('INSERT OR IGNORE INTO chain_swaps VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (log['transactionHash'], int(log['logIndex'],16), pool['pool'], token, wallet, ts, side,
                    str(abs(token_delta)), str(abs(quote_delta)), int(attributed)))
    def snapshots(self, now):
        db, rpc = self.store.db, self.rpc
        cursor = db.execute('SELECT * FROM chain_cursor WHERE id=1').fetchone()
        if not cursor:
            return []
        # Enrich current on-chain state even during catch-up, but block paper entries.
        height = max(cursor['height'], self.head-self.confirmations)
        block = rpc.block(height)
        observed = int(block['timestamp'], 16)
        if not -15 <= now - observed <= self.config.max_snapshot_age:
            return []
        lagging = self.head-cursor['height'] > self.batch_blocks
        pools = [dict(r) for r in db.execute('''SELECT p.* FROM chain_pools p LEFT JOIN
          (SELECT pool,MAX(ts) checked FROM launch_decisions GROUP BY pool) d ON d.pool=p.pool
          WHERE (p.ts>=? OR p.token0 IN (SELECT token FROM positions) OR p.token1 IN (SELECT token FROM positions))
          AND p.block<=? AND (p.token0 IN (?,?) OR p.token1 IN (?,?))
          ORDER BY (p.token0 IN (SELECT token FROM positions) OR p.token1 IN (SELECT token FROM positions)) DESC,
          EXISTS(SELECT 1 FROM evidence_refresh_requests r WHERE r.status='queued' AND r.token IN (p.token0,p.token1)) DESC,
          CASE WHEN d.checked<? AND p.ts>=? AND EXISTS (
            SELECT 1 FROM chain_swaps s WHERE s.pool=p.pool AND s.ts>d.checked AND s.ts>=?
          ) THEN 0 ELSE 1 END,
          COALESCE(d.checked,0),p.block DESC LIMIT 1''',
          (now-86400,height,evm.ZERO,evm.WETH,evm.ZERO,evm.WETH,now-20,now-900,now-300))]
        snapshots, seen = [], set()
        held_tokens={p['token'] for p in self.store.positions()}
        eth_usd = None
        for pool in pools:
            quote_tokens = (evm.ZERO, evm.WETH)
            if not any(t in quote_tokens for t in (pool['token0'],pool['token1'])):
                continue
            qi = 0 if pool['token0'] in quote_tokens else 1
            token = pool['token1'] if qi == 0 else pool['token0']
            if token in seen or (now - pool['ts'] > 86400 and token not in held_tokens):
                continue
            seen.add(token)
            row = db.execute('SELECT * FROM chain_tokens WHERE token=?', (token,)).fetchone()
            if not row or row['first_block']>height:
                continue
            if row['code_hash'] is None:
                info=token_metadata(rpc,token,row['first_block'],row['first_ts'])
                if not info:continue
                with db:
                    db.execute('UPDATE chain_tokens SET deployment_confirmed=?,decimals=?,code_hash=?,owner=?,metadata=? WHERE token=?',
                      (info['deployment_confirmed'],info['decimals'],info['code_hash'],info['owner'],info['metadata'],token))
                row=db.execute('SELECT * FROM chain_tokens WHERE token=?',(token,)).fetchone()
            if pool['launch_tx_sender'] is None:
                cached_receipt=db.execute('SELECT raw FROM chain_receipts WHERE tx=?',(pool['tx'],)).fetchone()
                launch_receipt=json.loads(cached_receipt['raw']) if cached_receipt else rpc.call('eth_getTransactionReceipt',[pool['tx']])
                with db:
                    db.execute('UPDATE chain_pools SET launch_tx_sender=? WHERE pool=?',(launch_receipt['from'].lower(),pool['pool']))
                    db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(pool['tx'],json.dumps(launch_receipt)))
            requested=ledger.refresh_actions(db,token,now)
            holder_balances=[]
            risk_row = db.execute('SELECT * FROM chain_risks WHERE token=?', (token,)).fetchone()
            if risk_row and now - risk_row['checked_at'] < 60 and 'refresh_permissions' not in requested:
                raw = json.loads(risk_row['raw'])
                checked = risk_row['checked_at']
            else:
                try:
                    raw = security(token)
                except (ValueError, KeyError, TypeError) as exc:
                    raw = {'_fetch_error':str(exc)[:160]}
                checked = time.time()
                with db:
                    db.execute('INSERT OR REPLACE INTO chain_risks VALUES(?,?,?)', (token, checked, json.dumps(raw)))
            risk = assess(raw, pool, checked)
            data_errors = []
            if raw.get('_fetch_error'):
                data_errors.append('security provider: '+raw['_fetch_error'])
            holder_evidence = None
            if row['deployment_confirmed']:
                cached = db.execute('SELECT * FROM chain_holder_checks WHERE token=?',(token,)).fetchone()
                if cached and now-cached['checked_at'] < 60 and 'refresh_holders' not in requested:
                    holder_evidence=json.loads(cached['evidence'])
                else:
                    try:
                        holder_evidence=enrichment.holders(rpc,token,row['first_block'],height,include_balances=True)
                        holder_balances=holder_evidence.pop('verified_balances')
                        holder_evidence.update(observed_at=observed,block_hash=block['hash'])
                    except (ValueError,KeyError,IndexError,TypeError) as exc:
                        holder_evidence={'error':str(exc)[:160]}
                    with db:
                        db.execute('INSERT OR REPLACE INTO chain_holder_checks VALUES(?,?,?)',(token,time.time(),json.dumps(holder_evidence)))
                if 'top10_share' in holder_evidence:
                    risk['top10_share']=holder_evidence['top10_share']
                else:
                    data_errors.append('holders: '+holder_evidence.get('error','not verified'))
            price,market_evidence=None,None
            quote_observed=observed
            if eth_usd is None:
                try:
                    eth_usd=Decimal(request_json('https://api.coinbase.com/v2/prices/ETH-USD/spot')['data']['amount'])
                except (ValueError,KeyError,TypeError):
                    data_errors.append('ETH/USD reference unavailable')
            if row['decimals'] is not None and eth_usd:
                try:
                    if pool['version']==4:
                        creations=db.execute("SELECT raw FROM chain_events WHERE tx=? AND kind='pool_created'",(pool['tx'],)).fetchall()
                        creation=next(json.loads(r['raw']) for r in creations if json.loads(r['raw'])['topics'][1].lower()==pool['pool'])
                        pool['tick_spacing']=evm.signed(evm.words(creation['data'])[1])
                    market_evidence=quotes.market(rpc,pool,row['decimals'],height,eth_usd,self.config.position_size-self.config.network_fee)
                    price=market_evidence['price']
                    risk['liquidity']=market_evidence['liquidity']
                    lp_burn=market_evidence.get('lp_burned_fraction')
                    if lp_burn is not None and lp_burn>=.95:
                        risk['reasons']=[r for r in risk['reasons'] if r!='liquidity_withdrawal_risk_unresolved']
                    elif pool['version']==2 and lp_burn is not None:
                        risk['reasons']=[r for r in risk['reasons'] if r!='liquidity_withdrawal_risk_unresolved']
                        risk['reasons'].append('v2_lp_burn_below_95_percent')
                except (ValueError,KeyError,TypeError,IndexError,StopIteration) as exc:
                    data_errors.append('market: '+str(exc)[:160])
            trade_simulation=None
            if market_evidence and market_evidence.get('execution_quote',{}).get('status')=='quoted':
                try:
                    trade_simulation=simulation.roundtrip(rpc,pool,market_evidence['execution_quote'],height)
                except (ValueError,KeyError,TypeError,IndexError) as exc:
                    trade_simulation={'status':'failed','error':str(exc)[:160],'block':height}
                if trade_simulation['status']=='passed':
                    market_evidence['execution_quote']['quantity']=float(Decimal(trade_simulation['token_received_atomic'])/10**row['decimals'])
                    proven={'cannot_buy_unknown','cannot_sell_all_unknown','is_honeypot_unknown'}
                    if trade_simulation['buy_transfer_shortfall']<=.03:proven.add('buy_tax_data_unavailable')
                    else:risk['reasons'].append('simulated_buy_shortfall_above_limit')
                    if trade_simulation['sell_transfer_shortfall']<=.03:proven.add('sell_tax_data_unavailable')
                    else:risk['reasons'].append('simulated_sell_shortfall_above_limit')
                    risk['reasons']=[r for r in risk['reasons'] if r not in proven]
                    if trade_simulation['roundtrip_loss_fraction']>.10:risk['reasons'].append('simulated_roundtrip_loss_above_10_percent')
                else:risk['reasons'].append('buy_sell_simulation_failed')
            creator_evidence=db.execute('SELECT * FROM chain_creators WHERE token=?',(token,)).fetchone()
            if not creator_evidence and row['deployment_confirmed']:
                mint=db.execute("SELECT tx FROM chain_events WHERE address=? AND block=? AND kind='token_mint' LIMIT 1",(token,row['first_block'])).fetchone()
                if mint:
                    try:
                        origin=contracts.deployment_origin(attribution.get_trace(db,rpc,mint['tx']),token)
                        with db:
                            db.execute('INSERT OR IGNORE INTO chain_creators VALUES(?,?,?,?,?)',(token,origin['deployer'] if origin else None,origin['initiator'] if origin else None,mint['tx'],'verified' if origin else 'not_found'))
                        creator_evidence=db.execute('SELECT * FROM chain_creators WHERE token=?',(token,)).fetchone()
                    except ValueError as exc:data_errors.append('deployment trace: '+str(exc))
            creator_history={'scope':'indexed_window_only','prior_rugs':'not_classified'}
            if creator_evidence and creator_evidence['status']=='verified':
                risk['creator']=creator_evidence['deployer']
                creator_history.update(deployer=creator_evidence['deployer'],initiator=creator_evidence['initiator'],
                    observed_deployments=db.execute('SELECT COUNT(*) FROM chain_creators WHERE initiator=?',(creator_evidence['initiator'],)).fetchone()[0])
            else:creator_history['status']='deployment_origin_not_available'
            source_verification={'status':risk['source_status'],'provider':'goplus','checked_at':checked}
            if risk['source_status']!='verified':
                cached=db.execute('SELECT * FROM chain_source_checks WHERE token=?',(token,)).fetchone()
                old_evidence=json.loads(cached['evidence']) if cached else {}
                upgraded=blockscout.key() and old_evidence.get('provider')!='blockscout'
                if cached and not upgraded and 'refresh_source' not in requested and now-cached['checked_at']<(3600 if old_evidence.get('status')=='verified' else 300):
                    fallback=json.loads(cached['evidence'])
                    fallback.setdefault('checked_at',cached['checked_at'])
                else:
                    fallback=contracts.verify_source(rpc,token,height,db)
                    fallback.update(checked_at=time.time(),block_hash=block['hash'] if fallback.get('block')==height else None)
                    with db:
                        db.execute('INSERT OR REPLACE INTO chain_source_checks VALUES(?,?,?)',(token,time.time(),json.dumps(fallback)))
                if fallback['status']=='verified':
                    source_verification=fallback
                    risk['reasons']=[r for r in risk['reasons'] if not r.startswith('source_')]
                else:
                    source_verification['fallback']=fallback
            recent,buyer_evidence=attribution.recent_buyers(db,rpc,token,height,observed,limit=12 if 'complete_buyers' in requested else 4)
            buyer_evidence.update(observed_at=observed,block_hash=block['hash'])
            for measurement in (market_evidence,trade_simulation):
                if measurement is not None:measurement.update(observed_at=observed,block_hash=block['hash'])
            qualified = sum(wallets.profile(db, wallet)['mirror_eligible'] for wallet in recent)
            s = dict(token=token, symbol=raw.get('token_symbol') or json.loads(row['metadata']).get('symbol') or token[:10], pool=pool['pool'],pool_version=pool['version'],
                     price=price, observed_at=quote_observed, liquidity=risk['liquidity'], top10_share=risk['top10_share'],
                     risk_reasons=risk['reasons'], risk_checked_at=checked,risk_checks=risk['checks'],
                     launch_age=observed-row['first_ts'], deployment_confirmed=bool(row['deployment_confirmed']),
                     distinct_buyers=len(recent), qualified_wallet_buys=qualified,
                     buyer_evidence=buyer_evidence,
                     creator=risk['creator'], creator_history=creator_history)
            s.update(holder_evidence=holder_evidence,market_evidence=market_evidence,data_errors=data_errors,
                     source_verification=source_verification,trade_simulation=trade_simulation,execution_quote=(market_evidence or {}).get('execution_quote'))
            s['requires_execution_quote']=True
            held=db.execute('SELECT quantity FROM positions WHERE token=?',(token,)).fetchone()
            if held and eth_usd and trade_simulation and trade_simulation['status']=='passed':
                try:
                    qty=int(Decimal(str(held['quantity']))*10**row['decimals'])
                    out=quotes.exact_input(rpc,pool,qi!=0,qty,height)
                    s['position_quote']={'status':'quoted','usd_out':float(Decimal(out)/10**18*eth_usd)*(1-trade_simulation['sell_transfer_shortfall']),'block':height}
                except (ValueError,TypeError,KeyError):
                    s['position_quote']={'status':'unavailable','block':height}
            if lagging:
                s['risk_reasons'].append('scanner_catching_up')
            if not market_evidence or market_evidence.get('execution_quote',{}).get('status')!='quoted':
                s['risk_reasons'].append('executable_pool_quote_unavailable')
            if rpc.block(height)['hash']!=block['hash']:
                raise ValueError('Assessment block changed during collection; evidence discarded')
            with db:
                ledger.block(db,height,block['hash'],block)
                ledger.balances(db,token,block['hash'],holder_balances)
            snapshots.append(s)
        return snapshots

    def tick(self):
        self.ingest()
        return self.evaluate()

    def evaluate(self):
        snapshots = self.snapshots(time.time())
        now = time.time()
        with self.store.db:
            for s in snapshots:
                s['evidence_contract']=evidence.build(s,now)
                s['evidence_quality']=evidence.quality(s['evidence_contract'],now,self.config.max_snapshot_age)
                s['score_details']=candidate_score(s,now,self.config)
                action, score, reasons = decide(s, now, self.config)
                decision=self.store.db.execute('INSERT INTO launch_decisions(ts,token,pool,action,score,reasons,evidence) VALUES(?,?,?,?,?,?,?)',
                                      (now, s['token'], s['pool'], action, score, json.dumps(reasons), json.dumps(s)))
                ledger.save_evidence(self.store.db,decision.lastrowid,s['evidence_contract'],s['evidence_quality'])
        for s in snapshots:
            ledger.finish_refresh(self.store.db,s['token'],s['evidence_quality'],now)
        from .astra_live import entry_gate
        def astra_screen(snapshot, stamp, config):
            action, score, reasons = decide(snapshot, stamp, config)
            return score, reasons + entry_gate(self.store.db, snapshot, stamp, action)
        Engine(self.store, self.config, astra_screen).tick(snapshots, now)
        return status(self.store, self.config, self.head)


def status(store, config, head=None):
    db = store.db
    cursor = db.execute('SELECT * FROM chain_cursor WHERE id=1').fetchone()
    counts = {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
              for name in ('chain_tokens', 'chain_pools', 'chain_events', 'chain_swaps', 'wallet_outcomes')}
    counts['wallets_observed'] = db.execute('SELECT COUNT(DISTINCT wallet) FROM chain_swaps').fetchone()[0]
    return dict(chain='robinhood', chain_id=evm.CHAIN_ID, head=head,
                cursor=dict(cursor) if cursor else None, counts=counts,
                paper=store.report(time.time(), config),
                coverage='Canonical Uniswap V2/V3/V4 pool logs and ERC20 mint events since start_block; not every launchpad or internal deployment')
