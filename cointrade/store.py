import json
import sqlite3
from dataclasses import asdict
from pathlib import Path


class Store:
    def __init__(self, path, config, source):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK(id=1), cash REAL, halted INTEGER,
            config TEXT, source TEXT, last_tick REAL);
          CREATE TABLE IF NOT EXISTS positions (
            token TEXT PRIMARY KEY, symbol TEXT, quantity REAL, cost REAL,
            entry_price REAL, opened_at REAL, mark REAL, marked_at REAL);
          CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, ts REAL, token TEXT, side TEXT,
            quantity REAL, price REAL, fee REAL, cash_flow REAL,
            pnl REAL, reason TEXT);
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, ts REAL, token TEXT, action TEXT,
            score REAL, reasons TEXT, snapshot TEXT);
          CREATE TABLE IF NOT EXISTS ai_calls (
            id INTEGER PRIMARY KEY, ts TEXT, reserved_cents INTEGER,
            actual_cost REAL, model TEXT, status TEXT, response TEXT);
          CREATE TABLE IF NOT EXISTS ai_routes (
            call_id INTEGER PRIMARY KEY, task TEXT, requested_model TEXT, tier TEXT);
        ''')
        encoded = json.dumps(asdict(config), sort_keys=True)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO account VALUES (1, ?, 0, ?, ?, NULL)",
                            (config.bankroll, encoded, source))
        account = self.account()
        if account['config'] != encoded or account['source'] != source:
            self.db.close()
            raise ValueError("Database config/source differs; use its original settings or a new --db path")

    def account(self):
        return dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())

    def positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions ORDER BY token")]

    def event(self, ts, token, action, reasons, snapshot=None, score=0):
        self.db.execute("INSERT INTO events(ts, token, action, score, reasons, snapshot) VALUES(?,?,?,?,?,?)",
                        (ts, token, action, score, json.dumps(reasons), json.dumps(snapshot)))

    def report(self, now, config):
        account, positions = self.account(), self.positions()
        for p in positions:
            p['stale'] = now - p['marked_at'] > config.max_snapshot_age
            p['unrealized_pnl'] = p['quantity'] * p['mark'] - p['cost']
        totals = self.db.execute("SELECT COUNT(*) AS fills, COALESCE(SUM(fee),0) AS fees, COALESCE(SUM(pnl),0) AS realized_pnl FROM trades").fetchone()
        return dict(mode="paper", as_of=now, source=account['source'], cash=account['cash'],
                    equity=account['cash'] + sum(p['quantity'] * p['mark'] for p in positions),
                    halted=bool(account['halted']), positions=positions,
                    valuation="last observed prices; excludes future exit costs; stale marks flagged",
                    **dict(totals))
