import argparse
from dataclasses import asdict
import json
import math
import sqlite3
import sys
import time

from .config import Config
from .engine import Engine
from .providers import discover, fetch_snapshot, fetch_coinbase
from .store import Store
from .strategy import screen, screen_coinbase
from .routing import TASKS, select


def output(value):
    print(json.dumps(value, indent=2, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description='On-chain launch research and paper trading; no live orders')
    parser.add_argument('--db', default='data/paper.sqlite')
    parser.add_argument('--config', help='JSON risk configuration; immutable for an existing database')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('config', help='Print default risk configuration')
    replay = sub.add_parser('replay', help='Replay chronological JSONL batches offline')
    replay.add_argument('file')
    scan = sub.add_parser('scan', help='Poll real market data and simulate fills')
    scan.add_argument('--token', action='append', default=[], help='Solana mint address; repeatable')
    scan.add_argument('--discover', type=int, default=0, metavar='N', help='Include up to N latest token profiles (max 10)')
    scan.add_argument('--cycles', type=int, default=1)
    scan.add_argument('--interval', type=float, default=60)
    exchange = sub.add_parser('coinbase', help='Simulate BTC/ETH/SOL trades using public Coinbase prices')
    exchange.add_argument('--product', action='append', choices=['BTC-USD', 'ETH-USD', 'SOL-USD'])
    exchange.add_argument('--cycles', type=int, default=1)
    exchange.add_argument('--interval', type=float, default=60)
    chain = sub.add_parser('onchain', help='Detect Robinhood Chain launches and track subsequent activity')
    chain.add_argument('--lookback', type=int, default=1000)
    chain.add_argument('--confirmations', type=int, default=2)
    chain.add_argument('--batch-blocks', type=int, default=200)
    chain.add_argument('--cycles', type=int, default=1)
    chain.add_argument('--interval', type=float, default=2)
    sub.add_parser('onchain-status', help='Chain cursor, launch and wallet coverage, paper portfolio')
    sub.add_parser('launches', help='Recent discovered pools and launch decisions')
    sub.add_parser('wallets', help='Observed wallet profiles and evidence confidence')
    gui = sub.add_parser('gui', help='Local live dashboard for the Robinhood Chain experiment')
    gui.add_argument('--host', default='127.0.0.1')
    gui.add_argument('--port', type=int, default=8765)
    gui.add_argument('--scan', action='store_true', help='Run chain polling and paper simulation alongside the GUI')
    gui.add_argument('--astra-experiment', action='store_true', help='Run/resume Astra High launch reviews with a persistent $3 total allocation')
    gui.add_argument('--astra-subscription', action='store_true', help='Run a bounded ten-review Astra High comparison through the signed-in Codex subscription')
    gui.add_argument('--astra-live', action='store_true', help='Continuously review detected tokens using Astra High on the ChatGPT subscription; gate paper entries')
    gui.add_argument('--momentum-experiment', action='store_true', help='Run isolated followable-wallet momentum paper accounts and historical replay')
    gui.add_argument('--lookback', type=int, default=100)
    gui.add_argument('--interval', type=float, default=2)
    sub.add_parser('status', help='Portfolio and accounting summary')
    events = sub.add_parser('events', help='Recent decisions with reasons and input snapshots')
    events.add_argument('--limit', type=int, default=20)
    sub.add_parser('trades', help='All simulated fills as JSON')
    sub.add_parser('halt', help='Persistently halt new entries; future scans may exit positions')
    analysis = sub.add_parser('analyze', help='Explicit OpenRouter analysis; requires an API key')
    analysis.add_argument('--task', choices=TASKS, default='wallet_summary')
    analysis.add_argument('--tier', choices=['low', 'medium', 'high'])
    analysis.add_argument('--input', help='JSON evidence file; explicit contents are sent to the selected model')
    route = sub.add_parser('ai-route', help='Inspect local routing without an API call')
    route.add_argument('task', choices=TASKS)
    route.add_argument('--tier', choices=['low', 'medium', 'high'])
    args = parser.parse_args()
    store = None
    try:
        c = Config.load(args.config)
        if args.command == 'ai-route':
            output(select(args.task, args.tier).describe())
            return
        if args.command == 'config':
            output(asdict(c))
            return
        if args.command == 'gui':
            from .dashboard import serve
            from pathlib import Path
            if not 0 <= args.lookback <= 100000 or not math.isfinite(args.interval) or args.interval < 1:
                raise ValueError('Use lookback 0..100000 and interval >= 1 second')
            if args.config is None and Path(args.db).is_file():
                with sqlite3.connect(args.db) as db:
                    row = db.execute('SELECT config FROM account WHERE id=1').fetchone()
                    if row:
                        c = Config(**json.loads(row[0]))
            serve(args.db,c,args.host,args.port,args.scan,args.lookback,args.interval,args.astra_experiment,args.astra_subscription,args.astra_live,args.momentum_experiment)
            return
        source = 'replay' if args.command == 'replay' else 'market'
        if args.command == 'coinbase':
            source = 'coinbase'
        if args.command == 'onchain':
            source = 'robinhood'
        # Read-only/report commands adopt the existing database's source label.
        if args.command not in ('replay', 'scan', 'coinbase', 'onchain'):
            from pathlib import Path
            if not Path(args.db).is_file():
                raise ValueError('No database yet; run replay or scan first')
            with sqlite3.connect(args.db) as db:
                row = db.execute('SELECT source,config FROM account WHERE id=1').fetchone()
            if row is None:
                raise ValueError('Database has no account')
            source = row[0]
            if args.config is None:
                c = Config(**json.loads(row[1]))
        if args.command == 'scan':
            if not 0 <= args.discover <= 10 or args.cycles < 1 or not math.isfinite(args.interval) or args.interval < 30:
                raise ValueError('Use discover 0..10, cycles >= 1, interval >= 30 seconds')
            if len(args.token) > 20:
                raise ValueError('Watchlist is limited to 20 tokens per invocation')
        if args.command == 'coinbase' and (args.cycles < 1 or not math.isfinite(args.interval) or args.interval < 30):
            raise ValueError('Use cycles >= 1 and interval >= 30 seconds')
        if args.command == 'onchain' and (args.cycles < 1 or not math.isfinite(args.interval) or args.interval < 1):
            raise ValueError('Use cycles >= 1 and interval >= 1 second')
        store = Store(args.db, c, source)
        engine = Engine(store, c, screen_coinbase if source == 'coinbase' else screen)
        if args.command == 'onchain':
            from .onchain import Scanner
            scanner = Scanner(store, c, lookback=args.lookback, confirmations=args.confirmations, batch_blocks=args.batch_blocks)
            for cycle in range(args.cycles):
                output(scanner.tick())
                if cycle + 1 < args.cycles:
                    time.sleep(args.interval)
        elif args.command in ('onchain-status', 'launches', 'wallets'):
            if source != 'robinhood':
                raise ValueError('Choose a Robinhood Chain database')
            if args.command == 'onchain-status':
                from .onchain import status
                output(status(store, c))
            elif args.command == 'launches':
                output({'pools': [dict(r) for r in store.db.execute('SELECT * FROM chain_pools ORDER BY block DESC LIMIT 20')],
                        'decisions': [dict(r) for r in store.db.execute('SELECT * FROM launch_decisions ORDER BY id DESC LIMIT 20')]})
            else:
                from .wallets import profile
                addresses = store.db.execute('SELECT DISTINCT wallet FROM chain_swaps ORDER BY wallet').fetchall()
                output([profile(store.db, r['wallet']) for r in addresses])
        elif args.command == 'replay':
            count = 0
            with open(args.file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    batch = json.loads(line)
                    ts = batch['ts']
                    if type(ts) not in (int, float) or not math.isfinite(ts):
                        raise ValueError('Invalid replay timestamp')
                    last = store.account()['last_tick']
                    if last is not None and ts <= last:
                        continue
                    engine.tick(batch['snapshots'], ts)
                    count += 1
            report = store.report(store.account()['last_tick'] or time.time(), c)
            output({'processed_batches': count, **report})
        elif args.command in ('scan', 'coinbase'):
            for cycle in range(args.cycles):
                requested = (args.product or ['BTC-USD', 'ETH-USD', 'SOL-USD']) if source == 'coinbase' else args.token
                tokens = list(dict.fromkeys([p['token'] for p in store.positions()] + requested))
                if source != 'coinbase' and args.discover:
                    try:
                        tokens = list(dict.fromkeys(tokens + discover(args.discover)))
                    except (ValueError, KeyError, TypeError):
                        with store.db:
                            store.event(time.time(), '', 'provider_error', ['discovery_unavailable'])
                if not tokens:
                    raise ValueError('Provide --token MINT or --discover N; no tokens available')
                snapshots = []
                for token in tokens:
                    try:
                        fetch = fetch_coinbase if source == 'coinbase' else fetch_snapshot
                        snapshots.append(fetch(token, time.time()))
                    except (ValueError, KeyError, TypeError):
                        with store.db:
                            store.event(time.time(), token, 'provider_error', ['market_data_unavailable'])
                    time.sleep(0.25)
                output(engine.tick(snapshots, time.time()))
                if cycle + 1 < args.cycles:
                    time.sleep(args.interval)
        elif args.command == 'status':
            output(store.report(time.time(), c))
        elif args.command in ('events', 'trades'):
            if args.command == 'events':
                if not 1 <= args.limit <= 1000:
                    raise ValueError('Event limit must be 1..1000')
                rows = store.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (args.limit,))
            else:
                rows = store.db.execute('SELECT * FROM trades ORDER BY id')
            output([dict(r) for r in rows])
        elif args.command == 'halt':
            with store.db:
                store.db.execute('UPDATE account SET halted=1 WHERE id=1')
                store.event(time.time(), '', 'halt', ['manual_halt'])
            output({'halted': True, 'message': 'New entries disabled; scan fresh quotes to simulate exits'})
        elif args.command == 'analyze':
            from .ai import analyze, analyze_evidence
            if args.input:
                with open(args.input) as f:
                    evidence = json.load(f)
                output(analyze_evidence(store, evidence, args.task, args.tier))
            else:
                output(analyze(store, store.report(time.time(), c), args.task, args.tier))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as e:
        print(f'cointrade: {e}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Stopped; committed paper account state is preserved.', file=sys.stderr)
    finally:
        if store:
            store.db.close()
