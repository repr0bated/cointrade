"""Fill launch metadata independently of slower market and security assessments."""
import hashlib
import json
import time
from . import evm
from .store import Store


def decode_text(raw):
    if not raw:return None
    try:
        data=bytes.fromhex(raw[2:])
        if len(data)==32:return data.rstrip(b'\0').decode('utf-8')[:120]
        start=int.from_bytes(data[:32]);size=int.from_bytes(data[start:start+32])
        if start!=32 or size>4096 or start+32+size>len(data):return None
        return data[start+32:start+32+size].decode('utf-8')[:120]
    except (ValueError,UnicodeError):return None


def fill(db,rpc):
    rows=db.execute('''SELECT t.* FROM chain_tokens t WHERE code_hash IS NULL
      ORDER BY EXISTS(SELECT 1 FROM chain_pools p WHERE p.token0=t.token OR p.token1=t.token) DESC,
      first_block LIMIT 8''').fetchall()
    if not rows:return 0
    height=db.execute('SELECT height FROM chain_cursor WHERE id=1').fetchone()[0]
    params=[]
    for r in rows:
        params.extend([[r['token'],hex(r['first_block'])],[r['token'],hex(max(0,r['first_block']-1))]])
    codes=rpc.batch('eth_getCode',params)
    selectors=['0x313ce567','0x8da5cb5b','0x95d89b41','0x06fdde03']
    values=rpc.reads([(r['token'],s) for r in rows for s in selectors],height)
    count=0
    with db:
        for i,r in enumerate(rows):
            code,previous=codes[2*i:2*i+2];dec,owner,symbol,name=values[4*i:4*i+4]
            if code=='0x':continue
            try:decimals=evm.words(dec) if dec else []
            except ValueError:decimals=[]
            try:owners=evm.words(owner) if owner else []
            except ValueError:owners=[]
            decimals=decimals[0] if len(decimals)==1 and 0<=decimals[0]<=36 else None
            owner=evm.address(owners[0]) if len(owners)==1 and owners[0]<2**160 else None
            metadata=dict(bytecode=code,bytecode_block=r['first_block'],metadata_block=height,
                          symbol=decode_text(symbol),name=decode_text(name),checked_at=time.time())
            count+=db.execute('''UPDATE chain_tokens SET deployment_confirmed=?,decimals=?,code_hash=?,owner=?,metadata=?
              WHERE token=? AND code_hash IS NULL''',(int(previous=='0x'),decimals,
              hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),owner,json.dumps(metadata),r['token'])).rowcount
    return count


def worker(path,config,monitor):
    store=Store(path,config,'robinhood');rpc=evm.RPC()
    try:
        while not monitor.stop.is_set():
            try:
                count=fill(store.db,rpc)
                monitor.update(metadata_error=None)
                monitor.stop.wait(2 if count else 10)
            except Exception as exc:
                monitor.update(metadata_error=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__)
                monitor.stop.wait(10)
    finally:store.db.close()
