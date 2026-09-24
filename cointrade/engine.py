from .strategy import price_valid, screen


class Engine:
    def __init__(self, store, config, screener=screen):
        self.store, self.c = store, config
        self.screen = screener

    def sell(self, p, now, reason, execution_quote=None):
        db, c = self.store.db, self.c
        price = p['mark'] * (1 - c.slippage_bps / 10000)
        gross = price * p['quantity']
        fee = gross * c.fee_bps / 10000 + c.network_fee
        if execution_quote:
            gross=execution_quote['usd_out']*(1-c.slippage_bps/10000)
            price=gross/p['quantity']
            fee=c.network_fee  # Pool fees already included in the quote.
        proceeds = gross - fee
        db.execute("UPDATE account SET cash=cash+? WHERE id=1", (proceeds,))
        db.execute("DELETE FROM positions WHERE token=?", (p['token'],))
        db.execute("INSERT INTO trades(ts,token,side,quantity,price,fee,cash_flow,pnl,reason) VALUES(?,?,'sell',?,?,?,?,?,?)",
                   (now, p['token'], p['quantity'], price, fee, proceeds, proceeds - p['cost'], reason))
        self.store.event(now, p['token'], 'sell', [reason])

    def tick(self, snapshots, now):
        db, c, store = self.store.db, self.c, self.store
        # Entire tick is atomic, including marks, fills, and the restart watermark.
        with db:
            db.execute("BEGIN IMMEDIATE")
            last = store.account()['last_tick']
            if last is not None and now <= last:
                raise ValueError("Tick timestamp must be newer than the previous tick")
            tokens = [s.get('token') for s in snapshots]
            if any(not isinstance(t, str) or not t for t in tokens) or len(tokens) != len(set(tokens)):
                raise ValueError("Snapshots require distinct, nonempty token IDs")
            fresh = set()
            by_token={s['token']:s for s in snapshots}
            for s in snapshots:
                if price_valid(s, now, c):
                    old = db.execute("SELECT marked_at FROM positions WHERE token=?", (s['token'],)).fetchone()
                    if old is None or s['observed_at'] > old['marked_at']:
                        fresh.add(s['token'])
                        db.execute("UPDATE positions SET mark=?, marked_at=? WHERE token=?",
                                   (s['price'], s['observed_at'], s['token']))
            report = store.report(now, c)
            # Persistent circuit breaker: subsequent ticks can exit but cannot reopen.
            if report['equity'] <= c.halt_equity:
                db.execute("UPDATE account SET halted=1 WHERE id=1")
            halted = bool(store.account()['halted'])
            exited = set()
            for p in store.positions():
                if p['token'] not in fresh:
                    store.event(now, p['token'], 'hold', ['missing_fresh_quote'])
                    continue
                change = p['mark'] / p['entry_price'] - 1
                reason = ('equity_halt' if halted else 'stop_loss' if change <= -c.stop_loss
                          else 'take_profit' if change >= c.take_profit
                          else 'max_hold' if now - p['opened_at'] >= c.max_hold_seconds else None)
                if reason:
                    s=by_token[p['token']]
                    position_quote=s.get('position_quote')
                    if s.get('requires_execution_quote') and (not position_quote or position_quote.get('status')!='quoted'):
                        store.event(now,p['token'],'hold',['position_sell_quote_unavailable'])
                        continue
                    self.sell(p, now, reason,position_quote)
                    exited.add(p['token'])
            # Exit costs can push equity through the limit too.
            if store.report(now, c)['equity'] <= c.halt_equity:
                db.execute("UPDATE account SET halted=1 WHERE id=1")
            for s in sorted(snapshots, key=lambda x: self.screen(x, now, c)[0], reverse=True):
                token = s['token']
                score, reasons = self.screen(s, now, c)
                account, positions = store.account(), store.positions()
                if any(p['token'] == token for p in positions):
                    store.event(now, token, 'hold', reasons, s, score)
                    continue
                if account['halted']:
                    reasons.append('equity_halt')
                if any(p['token'] not in fresh for p in positions):
                    reasons.append('portfolio_quote_missing')
                last_sell = db.execute("SELECT MAX(ts) FROM trades WHERE token=? AND side='sell'", (token,)).fetchone()[0]
                if token in exited or (last_sell is not None and now - last_sell < c.cooldown_seconds):
                    reasons.append('cooldown')
                cost = c.position_size
                if len(positions) >= c.max_positions:
                    reasons.append('position_limit')
                if sum(p['cost'] for p in positions) + cost > c.max_exposure + 1e-9:
                    reasons.append('exposure_limit')
                if account['cash'] - cost < c.reserve - 1e-9:
                    reasons.append('cash_reserve')
                if cost <= c.network_fee:
                    reasons.append('fees_exceed_position')
                if reasons:
                    store.event(now, token, 'reject', reasons, s, score)
                    continue
                # Position budget includes the buy fee; slippage reduces units received.
                price = max(s['price'], s.get('ask', s['price'])) * (1 + c.slippage_bps / 10000)
                gross = (cost - c.network_fee) / (1 + c.fee_bps / 10000)
                fee, quantity = cost - gross, gross / price
                quote=s.get('execution_quote')
                if quote and quote.get('status')=='quoted':
                    quantity=quote['quantity']*(1-c.slippage_bps/10000)
                    if quantity<=0 or abs(quote['usd_in']-(cost-c.network_fee))>1e-6:
                        store.event(now,token,'reject',['execution_quote_size_mismatch'],s,score)
                        continue
                    fee=c.network_fee
                    price=(cost-fee)/quantity
                db.execute("UPDATE account SET cash=cash-? WHERE id=1", (cost,))
                db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                           (token, str(s.get('symbol', token))[:32], quantity, cost, price, now, s['price'], s['observed_at']))
                db.execute("INSERT INTO trades(ts,token,side,quantity,price,fee,cash_flow,pnl,reason) VALUES(?,?,'buy',?,?,?, ?,0,'momentum')",
                           (now, token, quantity, price, fee, -cost))
                store.event(now, token, 'buy', ['momentum'], s, score)
            db.execute("UPDATE account SET last_tick=? WHERE id=1", (now,))
        return store.report(now, c)
