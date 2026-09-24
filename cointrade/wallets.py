"""Observed, attributable WETH cash flows only; unknown cost basis never wins."""
from decimal import Decimal, localcontext
import statistics

D = Decimal


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS wallet_flows (
        tx TEXT PRIMARY KEY, wallet TEXT, token TEXT, ts REAL, side TEXT,
        quantity TEXT, quote TEXT, gas TEXT, entry_rank INTEGER, attribution TEXT);
      CREATE TABLE IF NOT EXISTS wallet_lots (
        id INTEGER PRIMARY KEY, wallet TEXT, token TEXT, quantity TEXT, cost TEXT,
        initial_cost TEXT, opened_at REAL, entry_rank INTEGER, realized TEXT DEFAULT '0');
      CREATE TABLE IF NOT EXISTS wallet_outcomes (
        id INTEGER PRIMARY KEY, wallet TEXT, token TEXT, ts REAL, pnl TEXT,
        return_pct REAL, hold_seconds REAL, entry_rank INTEGER);
      CREATE TABLE IF NOT EXISTS wallet_unknown (
        tx TEXT PRIMARY KEY, wallet TEXT, token TEXT, reason TEXT);
    ''')


def record(db, tx, wallet, token, ts, quantity, quote, gas, rank):
    """Signed wallet deltas: positive token/negative quote = buy. Caller transaction owns commit."""
    with localcontext() as ctx:
        ctx.prec = 80
        quantity, quote, gas = D(quantity), D(quote), D(gas)
        if not all(v.is_finite() for v in (quantity, quote, gas)) or gas < 0 or quantity * quote >= 0:
            raise ValueError('Invalid wallet cash flow')
        side = 'buy' if quantity > 0 else 'sell'
        inserted = db.execute('INSERT OR IGNORE INTO wallet_flows VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (tx, wallet, token, ts, side, str(abs(quantity)), str(abs(quote)), str(gas), rank, 'direct_weth_transfers')).rowcount
        if not inserted:
            return
        if side == 'buy':
            cost = -quote + gas
            db.execute('INSERT INTO wallet_lots(wallet,token,quantity,cost,initial_cost,opened_at,entry_rank) VALUES(?,?,?,?,?,?,?)',
                       (wallet, token, str(quantity), str(cost), str(cost), ts, rank))
            return
        remaining = -quantity
        lots = db.execute('SELECT * FROM wallet_lots WHERE wallet=? AND token=? ORDER BY id', (wallet, token)).fetchall()
        known = sum((D(lot['quantity']) for lot in lots), D(0))
        if known < remaining:
            # A partial history or a transferred-in balance makes FIFO attribution uncertain.
            db.execute('INSERT OR IGNORE INTO wallet_unknown VALUES(?,?,?,?)', (tx, wallet, token, 'unmatched_sell_basis'))
            db.execute('DELETE FROM wallet_lots WHERE wallet=? AND token=?', (wallet, token))
            return
        proceeds_per_unit = (quote - gas) / remaining
        for lot in lots:
            if remaining <= 0:
                break
            held = D(lot['quantity'])
            matched = min(held, remaining)
            cost = D(lot['cost']) * matched / held
            realized = D(lot['realized']) + matched * proceeds_per_unit - cost
            if matched == held:
                initial = D(lot['initial_cost'])
                db.execute('INSERT INTO wallet_outcomes(wallet,token,ts,pnl,return_pct,hold_seconds,entry_rank) VALUES(?,?,?,?,?,?,?)',
                           (wallet, token, ts, str(realized), float(realized / initial * 100) if initial else 0,
                            ts - lot['opened_at'], lot['entry_rank']))
                db.execute('DELETE FROM wallet_lots WHERE id=?', (lot['id'],))
            else:
                db.execute('UPDATE wallet_lots SET quantity=?,cost=?,realized=? WHERE id=?',
                           (str(held-matched), str(D(lot['cost'])-cost), str(realized), lot['id']))
            remaining -= matched


def profile(db, wallet):
    rows = db.execute('SELECT * FROM wallet_outcomes WHERE wallet=?', (wallet,)).fetchall()
    n = len(rows)
    wins = sum(D(r['pnl']) > 0 for r in rows)
    returns = [r['return_pct'] for r in rows]
    distinct = len({r['token'] for r in rows})
    unknown = db.execute('SELECT COUNT(*) FROM wallet_unknown WHERE wallet=?', (wallet,)).fetchone()[0]
    opens = db.execute('SELECT COUNT(*) FROM wallet_lots WHERE wallet=?', (wallet,)).fetchone()[0]
    entries = db.execute("SELECT COUNT(DISTINCT token) FROM wallet_flows WHERE wallet=? AND side='buy'", (wallet,)).fetchone()[0]
    median = statistics.median(returns) if returns else None
    # Shrink a small sample toward neutral; require diverse closed outcomes.
    score = round(max(0, min(100, 100 * (wins + 2) / (n + 4)
                          + (max(-15, min(15, median / 2)) if median is not None else 0)
                          - min(20, unknown * 2))), 2)
    eligible = n >= 10 and distinct >= 5 and score >= 70 and median > 0 and unknown == 0
    return dict(wallet=wallet, observed_launches_entered=entries, closed_lots=n, distinct_closed_tokens=distinct,
                wins=wins, losses=n-wins, median_return_pct=median,
                median_entry_rank=statistics.median([r['entry_rank'] for r in rows]) if rows else None,
                mean_hold_seconds=statistics.mean([r['hold_seconds'] for r in rows]) if rows else None,
                realized_pnl_weth=str(sum((D(r['pnl']) for r in rows), D(0))),
                open_lots=opens, unknown_basis_or_attribution=unknown, score=score,
                mirror_eligible=eligible, confidence='observed_history_only' if eligible else 'insufficient_or_unreliable_history')
