"""Local read-only dashboard and optional read-only-chain/paper scanner worker."""
from datetime import datetime, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .config import Config
from .onchain import Scanner, schema
from .routing import select
from .store import Store
from .wallets import profile

WEB_ROOT = Path(__file__).with_name('web')


def parsed(value, default):
    try:
        return json.loads(value) if value is not None else default
    except (ValueError, TypeError):
        return default


class Monitor:
    def __init__(self, enabled=False):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state = dict(enabled=enabled, status='starting' if enabled else 'idle',
                          started_at=time.time() if enabled else None, updated_at=None,
                          last_success=None, error=None, head=None, cursor=None,
                          lag_blocks=None, rpc_calls=0, cycle_seconds=None)

    def update(self, **values):
        with self.lock:
            self.state.update(values, updated_at=time.time())

    def snapshot(self):
        with self.lock:
            return dict(self.state)


def scan_worker(path, config, monitor, lookback=100, confirmations=2, batch_blocks=1000, interval=2):
    store, scanner = None, None
    failures = 0
    try:
        store = Store(path, config, 'robinhood')
        while not monitor.stop.is_set():
            started = time.monotonic()
            try:
                monitor.update(status='scanning' if scanner else 'starting')
                if scanner is None:
                    scanner = Scanner(store, config, lookback=lookback, confirmations=confirmations,
                                      batch_blocks=batch_blocks)
                scanner.ingest()
                from .onchain import status
                result = status(store,config,scanner.head)
                monitor.update(provider=getattr(scanner.rpc,'provider','configured_rpc'))
                cursor = result['cursor']
                height = cursor['height'] if cursor else None
                lag = max(0, scanner.head - height) if height is not None else None
                elapsed = time.monotonic() - started
                failures = 0
                monitor.update(status='live' if lag is not None and lag <= confirmations + 20 else 'catching_up',
                               last_success=time.time(), error=None, head=scanner.head, cursor=height,
                               lag_blocks=lag, rpc_calls=scanner.rpc.calls, cycle_seconds=round(elapsed, 2))
                monitor.stop.wait(interval)
            except Exception as exc:
                # Do not leak RPC URLs, credentials, or raw upstream response bodies to the GUI.
                # RPC wrapper's ValueErrors are sanitized; other errors expose only the type.
                failures += 1
                message = str(exc) if isinstance(exc, ValueError) else f'Scanner failed ({type(exc).__name__})'
                monitor.update(status='error', error=message[:300],
                               rpc_calls=scanner.rpc.calls if scanner else 0,
                               head=scanner.head if scanner else None,
                               cycle_seconds=round(time.monotonic()-started, 2))
                if 'reorganization' in message.lower() or 'settings differ' in message.lower():
                    break
                monitor.stop.wait(min(60, 2 ** min(failures, 6)))
    finally:
        if store:
            store.db.close()
        if monitor.snapshot()['status'] != 'error':
            monitor.update(status='stopped')


def enrichment_worker(path, config, monitor):
    store = Store(path, config, 'robinhood')
    scanner = None
    try:
        while not monitor.stop.is_set():
            try:
                monitor.update(enrichment_status='checking',enrichment_error=None)
                if scanner is None:
                    scanner=Scanner(store,config)
                scanner.head=int(scanner.rpc.call('eth_blockNumber',[]),16)
                scanner.evaluate()
                monitor.update(enrichment_status='current',enrichment_error=None)
            except Exception as exc:
                monitor.update(enrichment_status='error',enrichment_error=str(exc)[:200] if isinstance(exc,ValueError) else type(exc).__name__)
            monitor.stop.wait(1)
    finally:
        store.db.close()


class Dashboard:
    def __init__(self, path, config, monitor):
        self.path, self.config, self.monitor = Path(path).resolve(), config, monitor
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0.0

    def state(self):
        # Browsers share a one-second snapshot cache; each read uses its own transaction.
        with self.lock:
            if self.cached is not None and time.monotonic() - self.cached_at < 1:
                return self.cached
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                result = self._read(db)
            self.cached, self.cached_at = result, time.monotonic()
            return result

    def _read(self, db):
        now = time.time()

        def rows(sql, args=()):
            return [dict(r) for r in db.execute(sql, args)]

        def scalar(sql, args=()):
            return db.execute(sql, args).fetchone()[0]

        account = rows('SELECT * FROM account WHERE id=1')[0]
        positions = rows('SELECT * FROM positions ORDER BY opened_at DESC')
        for p in positions:
            p['stale'] = now - p['marked_at'] > self.config.max_snapshot_age
            p['unrealized_pnl'] = p['quantity'] * p['mark'] - p['cost']
        totals = rows('SELECT COUNT(*) fills, COALESCE(SUM(fee),0) fees, COALESCE(SUM(pnl),0) realized_pnl FROM trades')[0]
        paper = dict(cash=account['cash'], equity=account['cash'] + sum(p['quantity']*p['mark'] for p in positions),
                     halted=bool(account['halted']), positions=positions, **totals,
                     valuation='Last observed marks; future exit costs excluded. Stale marks are flagged.')
        cursors = rows('SELECT * FROM chain_cursor WHERE id=1')
        cursor = cursors[0] if cursors else None
        pool_rows = rows('SELECT * FROM chain_pools ORDER BY block DESC LIMIT 100')
        risks = {r['token']: parsed(r['raw'], {}) for r in db.execute('SELECT token,raw FROM chain_risks')}
        for p in pool_rows:
            p['symbol0'] = risks.get(p['token0'], {}).get('token_symbol')
            p['symbol1'] = risks.get(p['token1'], {}).get('token_symbol')
        activity = rows('''SELECT e.tx,e.log_index,e.block,e.kind,e.address,e.raw,
                          s.side,s.token,s.pool,s.wallet,s.attributed
                          FROM chain_events e LEFT JOIN chain_swaps s
                          ON e.tx=s.tx AND e.log_index=s.log_index
                          ORDER BY e.block DESC,e.log_index DESC LIMIT 100''')
        for event in activity:
            event['id'] = f"{event['tx']}:{event['log_index']}"
            event['details'] = parsed(event.pop('raw'), {})
            if event['attributed'] is not None:
                event['attributed'] = bool(event['attributed'])
        decisions = rows('SELECT * FROM launch_decisions ORDER BY id DESC LIMIT 100')
        for d in decisions:
            d['reasons'] = parsed(d['reasons'], [])
            d['evidence'] = parsed(d['evidence'], {})
            from .launchrisk import candidate_score
            from . import evidence
            doc=d['evidence'].get('evidence_contract') or evidence.build(d['evidence'],d['ts'],origin='legacy_adapter')
            d['evidence_contract']=doc
            d['evidence_quality']=evidence.quality(doc,now,self.config.max_snapshot_age)
            d['recorded_score']=d['score']
            d['score_details']=d['evidence'].get('score_details') or candidate_score(d['evidence'],d['ts'],self.config)
            d['score']=d['score_details']['value']
            d['score_note']='Current evidence heuristic applied to the saved assessment; historical action and recorded score are preserved.'
        addresses = rows('''SELECT wallet, COUNT(*) n FROM chain_swaps WHERE wallet IS NOT NULL GROUP BY wallet
                            ORDER BY n DESC,wallet LIMIT 50''')
        wallet_rows = [profile(db, r['wallet']) for r in addresses]
        # Eligibility cannot be inferred from the displayed top-50 subset.
        candidates = rows('''SELECT wallet FROM wallet_outcomes GROUP BY wallet
                             HAVING COUNT(*)>=10 AND COUNT(DISTINCT token)>=5''')
        qualified = sum(profile(db, r['wallet'])['mirror_eligible'] for r in candidates)
        counts = {key: scalar(f'SELECT COUNT(*) FROM {table}') for key, table in (
            ('tokens','chain_tokens'),('pools','chain_pools'),('events','chain_events'),
            ('swaps','chain_swaps'),('decisions','launch_decisions'))}
        counts.update(wallets=scalar('SELECT COUNT(DISTINCT wallet) FROM chain_swaps'), qualified_wallets=qualified)
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        ai = dict(calls=scalar('SELECT COUNT(*) FROM ai_calls'),
                  reserved_today_usd=scalar('SELECT COALESCE(SUM(reserved_cents),0)/100.0 FROM ai_calls WHERE substr(ts,1,10)=?', (stamp[:10],)),
                  reserved_month_usd=scalar('SELECT COALESCE(SUM(reserved_cents),0)/100.0 FROM ai_calls WHERE substr(ts,1,7)=?', (stamp[:7],)),
                  actual_cost_usd=scalar('SELECT COALESCE(SUM(actual_cost),0) FROM ai_calls'),
                  unknown_cost_calls=scalar('SELECT COUNT(*) FROM ai_calls WHERE actual_cost IS NULL'),
                  daily_limit_usd=.1, monthly_limit_usd=3,
                  routes=[select(t, tier).describe() for t, tier in (
                      ('risk_rules',None),('unusual_activity',None),('pattern_analysis','medium'),
                      ('pattern_analysis','high'),('code_improvement',None))],
                  recent=rows('''SELECT c.*,r.task,r.requested_model,r.tier FROM ai_calls c
                                  LEFT JOIN ai_routes r ON c.id=r.call_id ORDER BY c.id DESC LIMIT 30'''))
        from .experiment import state as experiment_state
        ai['experiment'] = experiment_state(db)
        from .subscription import state as subscription_state
        ai['subscription_experiment'] = subscription_state(db)
        from .astra_live import state as live_astra_state, entry_gate
        ai['astra_live'] = live_astra_state(db)
        for d in decisions:
            d['astra_gate'] = entry_gate(db, d['evidence'], now, d['action'])
        paper['settings'] = dict(bankroll=self.config.bankroll, position_size=self.config.position_size,
            max_positions=self.config.max_positions, max_exposure=self.config.max_exposure,
            reserve=self.config.reserve, stop_loss=self.config.stop_loss, take_profit=self.config.take_profit,
            slippage_bps=self.config.slippage_bps, network_fee=self.config.network_fee)
        histogram = []
        if cursor:
            end = cursor['height']
            start = max(0, end - 1199)
            bins = dict(db.execute('SELECT CAST((block-?)/50 AS INTEGER),COUNT(*) FROM chain_events WHERE block>=? AND block<=? GROUP BY 1', (start,start,end)))
            histogram = [dict(block_from=start+i*50,block_to=min(end,start+(i+1)*50-1),count=bins.get(i,0))
                         for i in range((end-start)//50+1)]
        latest=rows('SELECT evidence,ts FROM launch_decisions WHERE id IN (SELECT MAX(id) FROM launch_decisions GROUP BY token)')
        verification={'verified':0,'unverified':0,'unavailable':0}
        holders_verified=quoted=0
        for d in latest:
            e=parsed(d['evidence'],{})
            verification[e.get('source_verification',{}).get('status','unavailable')]+=1
            holders_verified+=bool((e.get('holder_evidence') or {}).get('source')=='verified_transfer_ledger')
            quoted+=bool((e.get('execution_quote') or {}).get('status')=='quoted')
        quality=dict(source_status=verification,holders_verified=holders_verified,quotes_executable=quoted,
          wallet_flows=scalar('SELECT COUNT(*) FROM wallet_flows'),
          trace_resolved=scalar("SELECT COUNT(*) FROM wallet_reviews WHERE status='complete'"),
          ambiguous_transactions=scalar("SELECT COUNT(*) FROM wallet_reviews WHERE status='ambiguous'"),
          latest_assessment_at=max((d['ts'] for d in latest),default=None))
        quality['wallet_transactions_pending']=scalar('SELECT COUNT(DISTINCT tx) FROM chain_swaps WHERE tx NOT IN (SELECT tx FROM wallet_reviews)')
        quality['token_metadata_pending']=scalar('SELECT COUNT(*) FROM chain_tokens WHERE code_hash IS NULL')
        history=dict(jobs=[],audits=[])
        if scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='wallet_history_jobs'"):
            history['jobs']=rows('SELECT * FROM wallet_history_jobs ORDER BY id DESC LIMIT 5')
            for job in history['jobs']:job['wallets']=parsed(job['wallets'],[])
            for row in rows('''SELECT a.* FROM wallet_history_audits a JOIN wallet_history_jobs j ON j.id=a.job
              WHERE j.status='complete' ORDER BY a.checked_at DESC LIMIT 20'''):
                report=parsed(row['report'],{})
                report.pop('sales',None)
                history['audits'].append(report)
        from . import ledger
        quality['schema']=ledger.state(db)
        from . import momentum
        return dict(generated_at=now, scanner=self.monitor.snapshot(),quality=quality,
                    chain=dict(name='Robinhood Chain',chain_id=4663,cursor=cursor,
                               coverage='Canonical Uniswap V2/V3/V4 and ERC20 mint logs since the indexed start block. V3/V4 observation only.'),
                    counts=counts,paper=paper,activity=activity,pools=pool_rows,decisions=decisions,
                    wallets=wallet_rows,wallet_history=history,trades=rows('SELECT * FROM trades ORDER BY id DESC LIMIT 100'),
                    ai=ai,histogram=histogram,momentum=momentum.state(db,now))


def handler_for(dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, status, body, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == '/api/state':
                    body = json.dumps(dashboard.state(), allow_nan=False).encode()
                    self.send(200, body, 'application/json; charset=utf-8')
                elif path == '/api/health':
                    self.send(200, b'{"status":"ok"}', 'application/json')
                elif path in ('/', '/index.html', '/app.js', '/styles.css'):
                    name = 'index.html' if path == '/' else path[1:]
                    types = {'index.html':'text/html', 'app.js':'text/javascript', 'styles.css':'text/css'}
                    self.send(200, (WEB_ROOT/name).read_bytes(), types[name]+'; charset=utf-8')
                else:
                    self.send(404, b'Not found', 'text/plain')
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send(503, b'{"error":"Dashboard data is temporarily unavailable"}', 'application/json')

        def do_POST(self):
            # Custom header + JSON force cross-origin browsers to preflight, which this server rejects.
            if self.headers.get('X-Cointrade-Request') != 'terminal' or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                self.send(403, b'{"error":"Local terminal request required"}', 'application/json')
                return
            if urlsplit(self.path).path != '/api/astra':
                self.send(404, b'{"error":"Not found"}', 'application/json')
                return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 2048:
                    raise ValueError('Invalid request size')
                payload = json.loads(self.rfile.read(length))
                from . import astra_live
                with closing(sqlite3.connect(dashboard.path, timeout=10)) as db:
                    db.row_factory = sqlite3.Row
                    if not astra_live.exists(db):
                        raise ValueError('Astra live worker is not enabled')
                    if payload.get('action') == 'review':
                        token = payload.get('token')
                        if not isinstance(token,str) or len(token)!=42 or not token.startswith('0x'):
                            raise ValueError('Invalid token address')
                        result = astra_live.request(db,token.lower())
                    else:
                        astra_live.control(db,payload.get('action'))
                        result = {'status':'ok'}
                with dashboard.lock:
                    dashboard.cached = None
                self.send(200,json.dumps(result).encode(),'application/json')
            except (ValueError,TypeError,AttributeError) as exc:
                self.send(400,json.dumps({'error':str(exc)[:180]}).encode(),'application/json')
            except Exception:
                self.send(503,b'{"error":"Astra control unavailable"}','application/json')

    return Handler


def serve(path, config, host='127.0.0.1', port=8765, scan=False, lookback=100, interval=2, astra_experiment=False, astra_subscription=False, astra_live=False, momentum_experiment=False):
    if not 1 <= port <= 65535:
        raise ValueError('Port must be 1..65535')
    if sum((astra_experiment,astra_subscription,astra_live)) > 1:
        raise ValueError('Choose one Astra backend')
    store = Store(path, config, 'robinhood')
    schema(store.db)
    from . import experiment
    experiment.schema(store.db)
    if momentum_experiment:
        if not scan:
            raise ValueError('Momentum experiment requires --scan')
        from . import momentum
        momentum.start(store.db, config)
    if astra_experiment:
        experiment.start(store.db)
    if astra_subscription:
        from . import subscription
        subscription.start(store.db)
    if astra_live:
        from . import astra_live as live_astra
        live_astra.start(store.db)
        store.db.execute("UPDATE astra_experiment SET status='cancelled' WHERE id=1")
        store.db.commit()
    store.db.executescript('''
      CREATE INDEX IF NOT EXISTS chain_events_block ON chain_events(block DESC,log_index DESC);
      CREATE INDEX IF NOT EXISTS chain_swaps_wallet ON chain_swaps(wallet);
      CREATE INDEX IF NOT EXISTS chain_swaps_token_time ON chain_swaps(token,side,ts);
      CREATE INDEX IF NOT EXISTS wallet_outcomes_wallet ON wallet_outcomes(wallet);
      CREATE INDEX IF NOT EXISTS wallet_lots_wallet_token ON wallet_lots(wallet,token);
      CREATE INDEX IF NOT EXISTS wallet_unknown_wallet ON wallet_unknown(wallet);
    ''')
    store.db.close()
    monitor = Monitor(scan)
    dashboard = Dashboard(path, config, monitor)
    server = ThreadingHTTPServer((host, port), handler_for(dashboard))
    server.daemon_threads = True
    if astra_experiment:
        threading.Thread(target=experiment.worker,args=(path,config,monitor.stop),
                         daemon=True,name='astra-reviews').start()
    if astra_subscription:
        threading.Thread(target=subscription.worker,args=(path,monitor.stop),
                         daemon=True,name='astra-subscription-reviews').start()
    if astra_live:
        threading.Thread(target=live_astra.worker,args=(path,monitor.stop),
                         daemon=True,name='astra-live-reviews').start()
    if scan:
        from . import ledger
        threading.Thread(target=ledger.worker,args=(path,monitor.stop),daemon=True,name='relational-index').start()
        worker = threading.Thread(target=scan_worker, args=(path,config,monitor),
                                  kwargs=dict(lookback=lookback,interval=interval),daemon=True,name='chain-scanner')
        worker.start()
        threading.Thread(target=enrichment_worker,args=(path,config,monitor),daemon=True,name='data-enrichment').start()
        from . import attribution
        threading.Thread(target=attribution.worker,args=(path,config,monitor),daemon=True,name='wallet-traces').start()
        threading.Thread(target=attribution.fast_worker,args=(path,config,monitor),daemon=True,name='live-wallet-traces').start()
        from . import metadata
        threading.Thread(target=metadata.worker,args=(path,config,monitor),daemon=True,name='token-metadata').start()
    if momentum_experiment:
        for replay in (False, True):
            threading.Thread(target=momentum.worker,args=(path,config,monitor.stop,replay),
                             daemon=True,name='momentum-replay' if replay else 'momentum-live').start()
    print(f'Cointrade dashboard: http://{host}:{port} | paper only | scanner {"enabled" if scan else "off"}', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        server.server_close()
