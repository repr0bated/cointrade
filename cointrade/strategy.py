"""Deliberately simple, unvalidated hypothesis; never a profitability claim."""
import math


def number(value, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def price_valid(s, now, config):
    return (isinstance(s.get("token"), str) and bool(s["token"])
            and number(s.get("price")) and s["price"] > 0
            and number(s.get("observed_at"))
            and -15 <= now - s["observed_at"] <= config.max_snapshot_age)


def screen(s, now, config):
    reasons = []
    if not price_valid(s, now, config):
        reasons.append("invalid_or_stale_quote")
    if s.get("chain") != "solana":
        reasons.append("unsupported_chain")
    if s.get("standard_token") is not True:
        reasons.append("unsupported_or_unknown_token_program")
    for field in ("mint_authority", "freeze_authority"):
        if field not in s or s[field] is not None:
            reasons.append(field + "_active_or_unknown")
    for field, floor in (("liquidity", config.min_liquidity), ("volume_h24", config.min_volume_h24), ("age_seconds", config.min_age_seconds)):
        if not number(s.get(field)) or s[field] < floor:
            reasons.append(field + "_low_or_unknown")
    if not number(s.get("top10_share")) or s["top10_share"] > config.max_top10_share:
        reasons.append("top10_concentration_high_or_unknown")
    buys, sells, change = s.get("buys_m5"), s.get("sells_m5"), s.get("change_m5")
    if not number(buys) or not number(sells) or not number(change, -100):
        return 0, reasons + ["missing_activity"]
    count = buys + sells
    ratio = buys / count if count else 0
    score = round(40 * min(count / 50, 1) + 40 * ratio + (20 if 0 < change <= 30 else 0), 2)
    if count < 20 or ratio < 0.55 or not 0 < change <= 30:
        reasons.append("weak_or_overheated_momentum")
    if score < config.min_score:
        reasons.append("score_below_threshold")
    return score, reasons


def screen_coinbase(s, now, config):
    reasons = []
    if s.get('venue') != 'coinbase' or s.get('token') not in ('BTC-USD', 'ETH-USD', 'SOL-USD'):
        reasons.append('unsupported_exchange_product')
    if not price_valid(s, now, config):
        reasons.append('invalid_or_stale_quote')
    if not number(s.get('spread_bps')) or s['spread_bps'] > 20:
        reasons.append('spread_high_or_unknown')
    if not number(s.get('volume_h24')) or s['volume_h24'] < config.min_volume_h24:
        reasons.append('volume_low_or_unknown')
    if not number(s.get('candle_end')) or not 0 <= now - s['candle_end'] <= 120:
        reasons.append('stale_or_incomplete_candles')
    fast, slow, rising = s.get('sma_fast'), s.get('sma_slow'), s.get('rising_fraction')
    if not number(fast) or not number(slow) or slow == 0 or not number(rising) or rising > 1:
        return 0, reasons + ['missing_trend']
    score = round(50 + 50 * rising, 2)
    if fast / slow < 1.0005 or rising < .5:
        reasons.append('weak_trend')
    if score < config.min_score:
        reasons.append('score_below_threshold')
    return score, reasons
