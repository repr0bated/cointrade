"""Read-only Robinhood Chain RPC and narrowly scoped Uniswap event decoders."""
import os
import time
import threading
import hashlib
import fcntl
from pathlib import Path
from urllib.parse import urlencode

from .providers import request_json

CHAIN_ID = 4663
ZERO = '0x' + '0' * 40
ZERO_TOPIC = '0x' + '0' * 64
WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
V2 = '0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f'
V3 = '0x1f7d7550b1b028f7571e69a784071f0205fd2efa'
V4 = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
FACTORIES = [V2, V3, V4]
PAIR = '0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9'
POOL = '0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'
INIT4 = '0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
SWAP2 = '0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'
SWAP3 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
SWAP4 = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
MINT2 = '0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f'
BURN2 = '0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496'
MINT3 = '0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde'
BURN3 = '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'
MOD4 = '0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec'
SWAPS = [SWAP2, SWAP3, SWAP4]
ACTIVITY = SWAPS + [MINT2, BURN2, MINT3, BURN3, MOD4]
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0
MULTICALL = '0xca11bde05977b3631167028862be2a173976ca11'
# Runtime embedded in the deployment transaction in mds1/multicall3 README.
MULTICALL_SHA256 = '2756d7c52baee85cacb504f6ee1df7aad6809ac8d94a4a111d76991f90d36d6e'


def throttle(count=1):
    global _NEXT_REQUEST
    with _RATE_LOCK:
        # Scanner, backfills, and diagnostic processes share the endpoint's IP limit.
        directory=Path.home()/'.cache'/'cointrade';directory.mkdir(parents=True,exist_ok=True)
        fd=os.open(directory/'rpc-rate.lock',os.O_RDWR|os.O_CREAT,0o600)
        with os.fdopen(fd,'r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            now=time.monotonic()
            try:reserved=float(lock.read() or '0')
            except ValueError:reserved=0
            if reserved>now+60:reserved=0  # A reboot resets the monotonic clock.
            delay=max(0,reserved-now)
            _NEXT_REQUEST=max(reserved,now)+.14*count
            lock.seek(0);lock.truncate();lock.write(str(_NEXT_REQUEST));lock.flush()
    time.sleep(delay)


def words(data):
    if not isinstance(data, str) or not data.startswith('0x') or (len(data) - 2) % 64:
        raise ValueError('Malformed ABI words')
    return [int(data[i:i+64], 16) for i in range(2, len(data), 64)]


def address(word):
    value = int(word, 16) if isinstance(word, str) else word
    if not 0 <= value < 2**160:
        raise ValueError('Malformed ABI address')
    return f'0x{value:040x}'


def signed(word):
    return word - 2**256 if word >= 2**255 else word


def pool_created(log):
    t, w, emitter = log['topics'], words(log['data']), log['address'].lower()
    if t[0] == PAIR and emitter == V2 and len(t) == 3 and len(w) == 2:
        return dict(pool=address(w[0]), version=2, token0=address(t[1]), token1=address(t[2]), fee=3000, hooks=ZERO)
    if t[0] == POOL and emitter == V3 and len(t) == 4 and len(w) == 2:
        return dict(pool=address(w[1]), version=3, token0=address(t[1]), token1=address(t[2]), fee=int(t[3], 16), hooks=ZERO)
    if t[0] == INIT4 and emitter == V4 and len(t) == 4 and len(w) == 5:
        return dict(pool=t[1].lower(), version=4, token0=address(t[2]), token1=address(t[3]), fee=w[0], hooks=address(w[2]))
    return None


def pool_key(log):
    return log['topics'][1].lower() if log['address'].lower() == V4 else log['address'].lower()


def swap_amounts(log):
    w, topic = words(log['data']), log['topics'][0]
    if topic == SWAP2 and len(w) == 4:
        return w[0] - w[2], w[1] - w[3]
    if topic == SWAP3 and len(w) == 5:
        return signed(w[0]), signed(w[1])
    # V4's emitted deltas are caller deltas (opposite the V2/V3 pool deltas).
    if topic == SWAP4 and len(w) == 6:
        return -signed(w[0]), -signed(w[1])
    raise ValueError('Unsupported swap log')


def transfers(logs, token, wallet):
    total = 0
    for log in logs:
        topics = log['topics']
        if log['address'].lower() != token or len(topics) != 3 or topics[0] != TRANSFER:
            continue
        values = words(log['data'])
        if len(values) != 1:
            continue
        if address(topics[1]) == wallet:
            total -= values[0]
        if address(topics[2]) == wallet:
            total += values[0]
    return total


class CallReverted(ValueError):
    """A completed eth_call reverted; distinct from transport or missing data."""


class RPC:
    def __init__(self):
        self.url = os.environ.get('ROBINHOOD_RPC_URL', 'https://rpc.mainnet.chain.robinhood.com')
        self.provider = 'configured_rpc' if os.environ.get('ROBINHOOD_RPC_URL') else 'robinhood_public'
        if os.environ.get('COINTRADE_RPC_PROVIDER') == 'goldsky':
            key = Path('/home/jeremy/.config/cointrade/goldsky-rpc-key').read_text().strip()
            if not key:
                raise ValueError('Goldsky endpoint key is empty')
            self.url = 'https://edge.goldsky.com/standard/evm/4663?' + urlencode({'key':key})
            self.provider = 'goldsky'
        if not self.url.startswith('https://'):
            raise ValueError('ROBINHOOD_RPC_URL must use HTTPS')
        self.last = 0.0
        self.calls = 0

    def call(self, method, params):
        if method not in {'eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber', 'eth_getLogs',
                          'eth_getTransactionReceipt', 'eth_getCode', 'eth_call', 'eth_getStorageAt',
                          'debug_traceTransaction', 'eth_simulateV1', 'eth_getBlockReceipts',
                          'debug_traceBlockByNumber'}:
            raise ValueError('RPC method is not in the read-only allowlist')
        throttle()
        self.last = time.monotonic()
        self.calls += 1
        r = request_json(self.url, {'jsonrpc': '2.0', 'id': self.calls, 'method': method, 'params': params})
        if isinstance(r,dict) and isinstance(r.get('error'),dict) and r['error'].get('code')==-32012:
            raise ValueError('RPC range too large')
        if method=='eth_call' and isinstance(r,dict) and isinstance(r.get('error'),dict):
            error=r['error']
            if error.get('code')==3 or 'execution reverted' in str(error.get('message','')).lower():
                # Preserve the category, never expose the provider's error body.
                raise CallReverted('RPC eth_call reverted')
        if not isinstance(r, dict) or 'error' in r or r.get('result') is None:
            raise ValueError(f'RPC {method} failed; cursor was not advanced')
        return r['result']

    def block(self, height):
        return self.call('eth_getBlockByNumber', [hex(height), False])

    def batch(self, method, params):
        if method not in {'eth_getBlockByNumber', 'eth_getTransactionReceipt', 'eth_call', 'eth_getCode'}:
            raise ValueError('Batch method is not read-only')
        if not params:
            return []
        if len(params) > 20:
            return self.batch(method, params[:20]) + self.batch(method, params[20:])
        throttle(len(params))
        self.last = time.monotonic()
        self.calls += len(params)
        try:
            result = request_json(self.url, [dict(jsonrpc='2.0', id=i, method=method, params=p)
                                             for i, p in enumerate(params)])
        except ValueError as exc:
            if 'Response too large' in str(exc) and len(params) > 1:
                mid = len(params)//2
                return self.batch(method, params[:mid]) + self.batch(method, params[mid:])
            raise
        if not isinstance(result, list) or len(result) != len(params):
            raise ValueError('RPC batch incomplete; cursor preserved')
        by_id = {r.get('id'): r for r in result}
        if len(by_id) != len(params) or any(i not in by_id or by_id[i].get('result') is None or 'error' in by_id[i] for i in range(len(params))):
            raise ValueError('RPC batch failed; cursor preserved')
        return [by_id[i]['result'] for i in range(len(params))]

    def logs(self, start, end, topics, addresses=None):
        query = {'fromBlock': hex(start), 'toBlock': hex(end), 'topics': topics}
        if addresses is not None:
            if not addresses:
                return []
            query['address'] = addresses
        try:
            return self.call('eth_getLogs', [query])
        except ValueError as exc:
            if 'Response too large' not in str(exc) and 'range too large' not in str(exc):
                raise
            if start < end:
                middle = (start + end) // 2
                return (self.logs(start, middle, topics, addresses)
                        + self.logs(middle + 1, end, topics, addresses))
            if addresses and len(addresses) > 1:
                middle = len(addresses) // 2
                return (self.logs(start, end, topics, addresses[:middle])
                        + self.logs(start, end, topics, addresses[middle:]))
            raise ValueError('Single-block RPC response exceeds limit; cursor preserved') from None

    def read(self, contract, selector, height):
        return self.call('eth_call', [{'to': contract, 'data': selector}, hex(height)])

    def reads(self, calls, height):
        """Bounded read aggregation, pinned to a block and verified Multicall runtime.

        None means an individual call reverted. Never substitutes a successful zero.
        """
        from .simulation import Dynamic, pack, blob
        from .quotes import word
        if not calls:
            return []
        if len(calls)>250:
            return self.reads(calls[:250],height)+self.reads(calls[250:],height)
        if not getattr(self,'_multicall_verified',False):
            code=self.call('eth_getCode',[MULTICALL,hex(height)])
            if hashlib.sha256(bytes.fromhex(code[2:])).hexdigest()!=MULTICALL_SHA256:
                raise ValueError('Multicall runtime does not match published deployment')
            self._multicall_verified=True
        tuples=[Dynamic(pack([word(target),word(1),blob(data[2:])])) for target,data in calls]
        data='0x82ad56cb'+pack([Dynamic(word(len(tuples))+pack(tuples))])
        raw=bytes.fromhex(self.read(MULTICALL,data,height)[2:])
        def uint(offset):
            if offset<0 or offset%32 or offset+32>len(raw):
                raise ValueError('Malformed multicall response')
            return int.from_bytes(raw[offset:offset+32])
        start=uint(0);count=uint(start)
        if count!=len(calls):raise ValueError('Incomplete multicall response')
        base=start+32;out=[]
        for i in range(count):
            item=base+uint(base+32*i);success=uint(item)
            pos=item+uint(item+32);size=uint(pos)
            if success not in (0,1) or pos+32+size>len(raw):raise ValueError('Malformed multicall result')
            out.append('0x'+raw[pos+32:pos+32+size].hex() if success else None)
        return out

    def verify(self):
        if int(self.call('eth_chainId', []), 16) != CHAIN_ID:
            raise ValueError('Wrong RPC chain; expected Robinhood mainnet 4663')
        for factory in FACTORIES:
            if self.call('eth_getCode', [factory, 'latest']) == '0x':
                raise ValueError('Expected Uniswap contract has no deployed code')
