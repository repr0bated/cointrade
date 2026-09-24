import json
import math
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
ADDRESS = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def request_json(url, payload=None, headers=None, timeout=20):
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    request = Request(url, data=data, headers={
        'User-Agent': 'cointrade/0.1', 'Content-Type': 'application/json', **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('Response too large')
        return json.loads(raw)
    except HTTPError as e:
        # Do not expose credential-bearing RPC URLs or provider error bodies.
        raise ValueError(f'Provider HTTP {e.code}') from None
    except (URLError, TimeoutError, OSError):
        raise ValueError('Provider connection failed') from None


def rpc(method, params):
    endpoint = os.environ.get('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
    if not endpoint.startswith('https://'):
        raise ValueError('SOLANA_RPC_URL must use HTTPS')
    result = request_json(endpoint, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    if not isinstance(result, dict) or 'error' in result or 'result' not in result:
        raise ValueError(f'RPC {method} failed')
    return result['result']


def safety(token):
    result = rpc('getAccountInfo', [token, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
    value = result['value']
    if not value or value['owner'] != TOKEN_PROGRAM:
        return {'standard_token': False}
    parsed = value['data']['parsed']
    if parsed['type'] != 'mint':
        return {'standard_token': False}
    info = parsed['info']
    supply = int(info['supply'])
    if supply <= 0:
        raise ValueError('Empty token supply')
    time.sleep(0.25)
    largest = rpc('getTokenLargestAccounts', [token, {'commitment': 'confirmed'}])['value']
    if not largest:
        raise ValueError('Missing concentration data')
    amounts = sorted((int(row['amount']) for row in largest), reverse=True)
    if any(n < 0 for n in amounts):
        raise ValueError('Invalid token balances')
    return {'standard_token': True, 'mint_authority': info['mintAuthority'],
            'freeze_authority': info['freezeAuthority'], 'top10_share': sum(amounts[:10]) / supply}


def fetch_snapshot(token, now):
    if not ADDRESS.fullmatch(token):
        raise ValueError('Invalid Solana token address')
    pairs = request_json(f'https://api.dexscreener.com/token-pairs/v1/solana/{token}')
    candidates = [p for p in pairs if p.get('chainId') == 'solana'
                  and p.get('baseToken', {}).get('address') == token and p.get('priceUsd')]
    if not candidates:
        raise ValueError('No USD-priced base-token pool found')
    p = max(candidates, key=lambda x: float((x.get('liquidity') or {}).get('usd', 0)))
    created = p.get('pairCreatedAt')
    tx = (p.get('txns') or {}).get('m5') or {}
    snapshot = dict(token=token, symbol=p['baseToken'].get('symbol', token), chain='solana',
                    observed_at=now, price=float(p['priceUsd']), pair=p['pairAddress'],
                    liquidity=(p.get('liquidity') or {}).get('usd'),
                    volume_h24=(p.get('volume') or {}).get('h24'),
                    age_seconds=max(0, now - created / 1000) if created else None,
                    buys_m5=tx.get('buys'), sells_m5=tx.get('sells'),
                    change_m5=(p.get('priceChange') or {}).get('m5'))
    try:
        snapshot.update(safety(token))
    except (ValueError, KeyError, TypeError):
        snapshot['safety_error'] = 'RPC safety data unavailable'
    return snapshot


def discover(limit):
    rows = request_json('https://api.dexscreener.com/token-profiles/latest/v1')
    tokens = []
    for row in rows:
        token = row.get('tokenAddress', '')
        if row.get('chainId') == 'solana' and ADDRESS.fullmatch(token) and token not in tokens:
            tokens.append(token)
    return tokens[:limit]


def fetch_coinbase(product, now):
    if product not in ('BTC-USD', 'ETH-USD', 'SOL-USD'):
        raise ValueError('Coinbase prototype supports BTC-USD, ETH-USD, and SOL-USD')
    root = f'https://api.exchange.coinbase.com/products/{product}'
    ticker = request_json(root + '/ticker')
    observed = datetime.fromisoformat(ticker['time'].replace('Z', '+00:00')).timestamp()
    bid, ask = float(ticker['bid']), float(ticker['ask'])
    if not 0 < bid <= ask or not all(math.isfinite(x) for x in (bid, ask)):
        raise ValueError('Invalid bid/ask')
    end = int(now // 60) * 60
    query = urlencode({'granularity': 60,
                       'start': datetime.fromtimestamp(end - 1800, timezone.utc).isoformat(),
                       'end': datetime.fromtimestamp(end, timezone.utc).isoformat()})
    rows = request_json(root + '/candles?' + query)
    # Closed candles only: never use a partially formed future signal.
    candles = sorted((r for r in rows if r[0] + 60 <= now), key=lambda r: r[0])[-20:]
    snapshot = dict(token=product, symbol=product, venue='coinbase', observed_at=observed,
                    price=bid, bid=bid, ask=ask, spread_bps=(ask / bid - 1) * 10000,
                    volume_h24=float(ticker['volume']) * bid, received_at=now)
    if len(candles) == 20 and all(candles[i][0] - candles[i-1][0] == 60 for i in range(1, 20)):
        closes = [float(r[4]) for r in candles]
        if all(x > 0 and math.isfinite(x) for x in closes):
            snapshot.update(sma_fast=sum(closes[-5:]) / 5, sma_slow=sum(closes) / 20,
                            candle_end=candles[-1][0] + 60,
                            rising_fraction=sum(b > a for a, b in zip(closes, closes[1:])) / 19)
    return snapshot
