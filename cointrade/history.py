"""Resumable wallet audit: ERC20 transfer history, transaction traces, and balance reconciliation.

This deliberately does not claim to enumerate native-only transactions or lifetime history.
Run with python -m cointrade.history --db ... --hours 24 --wallet 0x... (repeatable).
"""
import argparse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
import time
from . import attribution, blockscout, evm


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS wallet_history_jobs(
        id INTEGER PRIMARY KEY,wallets TEXT,start_block INTEGER,end_block INTEGER,
        start_time INTEGER,end_time INTEGER,end_hash TEXT,cursor INTEGER,status TEXT,error TEXT);
      CREATE TABLE IF NOT EXISTS wallet_history_events(
        job INTEGER,wallet TEXT,tx TEXT,log_index INTEGER,block INTEGER,tx_index INTEGER,
        token TEXT,delta TEXT,raw TEXT,PRIMARY KEY(job,wallet,tx,log_index));
      CREATE TABLE IF NOT EXISTS wallet_history_audits(
        job INTEGER,wallet TEXT,checked_at REAL,report TEXT,PRIMARY KEY(job,wallet));
    ''')
    if 'heartbeat' not in {r[1] for r in db.execute('PRAGMA table_info(wallet_history_jobs)')}:
        with db:db.execute('ALTER TABLE wallet_history_jobs ADD COLUMN heartbeat REAL DEFAULT 0')


def boundary(rpc,head,timestamp):
    """Find the first block at/after a timestamp; do not assume a fixed block time."""
    lo,hi=0,head
    while lo<hi:
        mid=(lo+hi)//2
        if int(rpc.block(mid)['timestamp'],16)<timestamp:lo=mid+1
        else:hi=mid
    return lo


def start(db,rpc,wallets,hours):
    end=int(rpc.call('eth_blockNumber',[]),16)-2;block=rpc.block(end)
    timestamp=int(block['timestamp'],16);first=boundary(rpc,end,timestamp-int(hours*3600))
    begin=rpc.block(first)
    with db:
        row=db.execute('''INSERT INTO wallet_history_jobs(wallets,start_block,end_block,start_time,end_time,end_hash,cursor,status)
          VALUES(?,?,?,?,?,?,?,'scanning')''',(json.dumps(wallets),first,end,int(begin['timestamp'],16),timestamp,block['hash'],first-1))
    return row.lastrowid


def scan_range(rpc,begin,end,wallets):
    topics=['0x'+w[2:].zfill(64) for w in wallets]
    incoming=rpc.logs(begin,end,[evm.TRANSFER,None,topics])
    outgoing=rpc.logs(begin,end,[evm.TRANSFER,topics])
    return {(r['transactionHash'],int(r['logIndex'],16)):r for r in incoming+outgoing}.values()


def scan(db,rpc,job,executor):
    wallets=json.loads(job['wallets']);end=job['end_block']
    while job['cursor']<end:
        ranges=[(n,min(n+9999,end)) for n in range(job['cursor']+1,min(job['cursor']+80001,end+1),10000)]
        futures=[executor.submit(scan_range,evm.RPC(),a,b,wallets) for a,b in ranges]
        # Persist only completed contiguous ranges, even if a later request fails.
        for (a,b),future in zip(ranges,futures):
            logs=list(future.result())
            with db:
                for log in logs:
                    if log.get('removed'):raise ValueError('Removed wallet transfer log')
                    if len(log.get('topics',[]))!=3:continue  # ERC721 Transfer is not ERC20.
                    values=evm.words(log['data'])
                    if len(values)!=1:raise ValueError('Malformed ERC20 transfer amount')
                    sender=evm.address(log['topics'][1]);recipient=evm.address(log['topics'][2])
                    for wallet in wallets:
                        delta=values[0]*(int(wallet==recipient)-int(wallet==sender))
                        if wallet not in (sender,recipient):continue
                        db.execute('INSERT OR IGNORE INTO wallet_history_events VALUES(?,?,?,?,?,?,?,?,?)',
                          (job['id'],wallet,log['transactionHash'],int(log['logIndex'],16),int(log['blockNumber'],16),
                           int(log['transactionIndex'],16),log['address'].lower(),str(delta),json.dumps(log)))
                db.execute('UPDATE wallet_history_jobs SET cursor=?,heartbeat=? WHERE id=?',(b,time.time(),job['id']))
            print(json.dumps({'job':job['id'],'scanned_through':b,'end_block':end}),flush=True)
        job=db.execute('SELECT * FROM wallet_history_jobs WHERE id=?',(job['id'],)).fetchone()
    if rpc.block(end)['hash']!=job['end_hash']:raise ValueError('Wallet history boundary reorganized')
    with db:db.execute("UPDATE wallet_history_jobs SET status='transactions' WHERE id=?",(job['id'],))


def transactions(db,job,executor):
    rows=db.execute('''SELECT tx,MIN(block) block,MIN(tx_index) tx_index,MIN(raw) raw
      FROM wallet_history_events WHERE job=? GROUP BY tx ORDER BY block,tx_index''',(job['id'],)).fetchall()
    def fetch(row):
        rpc=evm.RPC()
        receipt=rpc.call('eth_getTransactionReceipt',[row['tx']])
        trace=rpc.call('debug_traceTransaction',[row['tx'],{'tracer':'callTracer'}])
        expected=json.loads(row['raw'])['blockHash']
        if receipt['blockHash']!=expected:raise ValueError('Wallet receipt block mismatch')
        return row['tx'],receipt,trace
    missing=[r for r in rows if not db.execute('''SELECT 1 FROM chain_receipts r JOIN chain_traces t ON t.tx=r.tx
      WHERE r.tx=?''',(r['tx'],)).fetchone()]
    for offset in range(0,len(missing),24):
        for tx,receipt,trace in executor.map(fetch,missing[offset:offset+24]):
            with db:
                db.execute('INSERT OR IGNORE INTO chain_receipts VALUES(?,?)',(tx,json.dumps(receipt)))
                db.execute('INSERT OR IGNORE INTO chain_traces VALUES(?,?)',(tx,json.dumps(trace)))
        print(json.dumps({'job':job['id'],'transactions_fetched':min(offset+24,len(missing)),
                          'transactions_needed':len(missing),'transactions_total':len(rows)}),flush=True)
        with db:db.execute('UPDATE wallet_history_jobs SET heartbeat=? WHERE id=?',(time.time(),job['id']))


def fifo(events,opening):
    """Known buys carry cost; transferred/opening inventory carries unknown cost."""
    lots=defaultdict(deque);sales=[]
    for token,quantity in opening.items():
        if quantity:lots[token].append([quantity,None])
    for e in events:
        token,qty=e['token'],e['quantity']
        if qty>0:
            lots[token].append([qty,Decimal(e['cost']) if e.get('cost') is not None else None])
            continue
        remaining=-qty;cost=Decimal(0);unknown=0
        while remaining and lots[token]:
            lot=lots[token][0];take=min(remaining,lot[0])
            if lot[1] is None:unknown+=take
            else:
                allocated=lot[1]*Decimal(take)/lot[0];cost+=allocated;lot[1]-=allocated
            lot[0]-=take;remaining-=take
            if not lot[0]:lots[token].popleft()
        unknown+=remaining
        if e.get('proceeds') is not None:
            sales.append(dict(token=token,tx=e['tx'],quantity=-qty,unknown_quantity=unknown,
                              cost=str(cost) if not unknown else None,
                              pnl=str(Decimal(e['proceeds'])-cost) if not unknown else None))
    return sales,lots


def explorer_crosscheck(db,job,wallet,items):
    """Cross-check explorer external history against independently fetched receipts.

    The explorer also reports failed/native-only transactions outside the token scan.
    Their fees are reported separately, not silently folded into matched-sale P&L.
    """
    indexed={};fees=failed=0
    for item in items:
        tx=item['hash'].lower()
        if tx in indexed:raise ValueError('Explorer history contains a duplicate transaction')
        if not job['start_block']<=int(item['blockNumber'])<=job['end_block']:
            raise ValueError('Explorer history outside audit interval')
        if wallet not in (item['from'].lower(),(item.get('to') or '').lower()):
            raise ValueError('Explorer history contains an unrelated transaction')
        indexed[tx]=item
        if item['from'].lower()==wallet:
            fees+=int(item['gasUsed'])*int(item['gasPrice'])
            failed+=item['isError']=='1'
    rows=db.execute('''SELECT DISTINCT h.tx,r.raw FROM wallet_history_events h
      JOIN chain_receipts r ON r.tx=h.tx WHERE h.job=? AND h.wallet=?''',(job['id'],wallet)).fetchall()
    missing=[];mismatches=[];checked=0;internal=0;token_txs=set()
    for row in rows:
        tx=row['tx'].lower();token_txs.add(tx);receipt=json.loads(row['raw'])
        if wallet not in (receipt['from'].lower(),(receipt.get('to') or '').lower()):
            internal+=1;continue
        item=indexed.get(tx)
        if item is None:missing.append(tx);continue
        agrees=(item['blockHash'].lower()==receipt['blockHash'].lower()
          and int(item['blockNumber'])==int(receipt['blockNumber'],16)
          and int(item['gasUsed'])==int(receipt['gasUsed'],16)
          and int(item['gasPrice'])==int(receipt['effectiveGasPrice'],16)
          and int(item['txreceipt_status'])==int(receipt['status'],16)
          and item['from'].lower()==receipt['from'].lower()
          and (item.get('to') or '').lower()==(receipt.get('to') or '').lower())
        if agrees:checked+=1
        else:mismatches.append(tx)
    return dict(source='blockscout_authenticated_paginated_external_transactions',
      status='matched' if not missing and not mismatches else 'disagreement',
      external_transactions=len(indexed),outgoing_failed_transactions=failed,
      receipt_matches=checked,missing_external_transactions=missing,receipt_mismatches=mismatches,
      internal_token_transactions=internal,external_transactions_without_token_events=len(set(indexed)-token_txs),
      explorer_reported_outgoing_gas_fees_eth=str(Decimal(fees)/10**18),
      scope='Bounded external transaction history. Overlapping token-transaction receipts are independently checked; remaining rows are explorer-reported. This is not a lifetime or full native-balance audit.')


def resolve_predeployment_balances(rpc,opening,height,heartbeat=lambda:None):
    """An absent contract has no opening token balance; a failed call alone proves nothing."""
    absent=getattr(rpc,'_history_absent_contracts',set())
    missing=[t for t,v in opening.items() if v is None and (height,t) not in absent]
    for offset in range(0,len(missing),20):
        tokens=missing[offset:offset+20]
        codes=rpc.batch('eth_getCode',[[t,hex(height)] for t in tokens])
        for token,code in zip(tokens,codes):
            if code=='0x':absent.add((height,token))
        heartbeat()
    rpc._history_absent_contracts=absent
    return {t:0 if v is None and (height,t) in absent else v for t,v in opening.items()}


def audit(db,rpc,job,wallet):
    logs=db.execute('''SELECT * FROM wallet_history_events WHERE job=? AND wallet=?
      ORDER BY block,tx_index,log_index''',(job['id'],wallet)).fetchall()
    tokens=sorted({r['token'] for r in logs if r['token']!=evm.WETH})
    calls=[(t,'0x70a08231'+wallet[2:].zfill(64)) for t in tokens]
    def balances(height):
        values=rpc.reads(calls,height);out={}
        for t,v in zip(tokens,values):
            try:w=evm.words(v)
            except ValueError:w=[]
            out[t]=w[0] if len(w)==1 else None
        return out
    opening=balances(max(0,job['start_block']-1));ending=balances(job['end_block'])
    def heartbeat():
        with db:db.execute('UPDATE wallet_history_jobs SET heartbeat=? WHERE id=?',(time.time(),job['id']))
    opening=resolve_predeployment_balances(rpc,opening,max(0,job['start_block']-1),heartbeat)
    delta=defaultdict(int);groups={}
    for r in logs:
        delta[r['token']]+=int(r['delta']);groups.setdefault(r['tx'],[]).append(r)
    reconciled={t:opening[t] is not None and ending[t] is not None and opening[t]+delta[t]==ending[t] for t in tokens}
    events=[];fees=0;buys=0;sells=0;other=0
    for tx,items in groups.items():
        receipt=json.loads(db.execute('SELECT raw FROM chain_receipts WHERE tx=?',(tx,)).fetchone()[0])
        trace=json.loads(db.execute('SELECT raw FROM chain_traces WHERE tx=?',(tx,)).fetchone()[0])
        if receipt['blockHash']!=json.loads(items[0]['raw'])['blockHash']:raise ValueError('Cached receipt block mismatch')
        gas=int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16) if receipt['from'].lower()==wallet else 0
        fees+=gas;trade=None
        try:
            match=attribution.flow(receipt,trace)
            if match[0]==wallet:trade=match
        except ValueError:pass
        net=defaultdict(int)
        for item in items:net[item['token']]+=int(item['delta'])
        if trade:
            _,token,qty,quote=trade
            if net[token]!=qty:raise ValueError('Wallet log scan and receipt transfer totals disagree')
            buys+=qty>0;sells+=qty<0
        else:other+=1
        for token,quantity in net.items():
            if token==evm.WETH or not quantity:continue
            e=dict(tx=tx,token=token,quantity=quantity)
            if trade and trade[1]==token:
                if quantity>0:e['cost']=-trade[3]+gas
                else:e['proceeds']=trade[3]-gas
            events.append(e)
    with localcontext() as context:
        context.prec=100
        sales,lots=fifo(events,{t:q or 0 for t,q in opening.items()})
    known=[s for s in sales if s['pnl'] is not None and reconciled[s['token']]]
    pnl=sum((Decimal(s['pnl']) for s in known),Decimal(0));cost=sum((Decimal(s['cost']) for s in known),Decimal(0))
    return dict(wallet=wallet,source='goldsky_archive_rpc_receipts_and_call_traces',
      coverage=dict(start_block=job['start_block'],end_block=job['end_block'],start_time=job['start_time'],end_time=job['end_time'],
        scope='All standard ERC20 Transfer events involving this wallet in this interval, plus their complete transaction receipts and call traces. Native-only transactions are outside this audit.'),
      transfer_events=len(logs),transactions=len(groups),buys=buys,sells=sells,other_transactions=other,
      tokens=len(tokens),tokens_reconciled=sum(reconciled.values()),unreconciled_tokens=[t for t,v in reconciled.items() if not v],
      reconciliation_failures={t:dict(opening=opening[t],transfer_delta=str(delta[t]),ending=ending[t])
        for t,v in reconciled.items() if not v},
      matched_sales=len(known),unknown_basis_or_unreconciled_sales=len(sales)-len(known),
      winning_matched_sales=sum(Decimal(s['pnl'])>0 for s in known),realized_pnl_weth=str(pnl/10**18),
      return_on_matched_cost_pct=float(pnl/cost*100) if cost else None,observed_transaction_fees_eth=str(Decimal(fees)/10**18),
      tokens_with_opening_inventory=sum(bool(q) for q in opening.values()),
      open_token_positions=sum(bool(v) for v in lots.values()),accounting='FIFO; transferred-in or opening inventory has unknown cost; unrealized holdings excluded',
      sales=known,mirror_eligible=False,eligibility_reason='Historical audit only; no out-of-sample predictive performance established')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True);parser.add_argument('--wallet',action='append',default=[])
    parser.add_argument('--hours',type=float,default=24);parser.add_argument('--resume',type=int)
    parser.add_argument('--output',default='artifacts/wallet-history.json')
    parser.add_argument('--explorer-history',help='Reuse a saved, bounded Blockscout external-history JSON file')
    args=parser.parse_args()
    if not 0<args.hours<=24*30:parser.error('hours must be in (0,720]')
    wallets=sorted(set(w.lower() for w in args.wallet))
    for w in wallets:
        if len(w)!=42 or evm.address(w)!=w:parser.error('Invalid wallet address')
    if not wallets and not args.resume:parser.error('Provide --wallet or --resume')
    db=sqlite3.connect(args.db,timeout=30);db.row_factory=sqlite3.Row;schema(db);rpc=evm.RPC()
    job_id=args.resume or start(db,rpc,wallets,args.hours)
    try:
        with db:db.execute('UPDATE wallet_history_jobs SET heartbeat=?,error=NULL WHERE id=?',(time.time(),job_id))
        with ThreadPoolExecutor(max_workers=4) as executor:
            job=db.execute('SELECT * FROM wallet_history_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise ValueError('Unknown history job')
            scan(db,rpc,job,executor);transactions(db,job,executor)
        reports=[]
        explorer_cache=json.loads(Path(args.explorer_history).read_text()) if args.explorer_history else None
        for wallet in json.loads(job['wallets']):
            report=audit(db,rpc,job,wallet);reports.append(report)
            if explorer_cache is not None or blockscout.key():
                try:
                    if explorer_cache is not None:
                        saved=next((x for x in explorer_cache if x['wallet']==wallet),None)
                        if saved is None or any(saved[k]!=job[k] for k in ('start_block','end_block')):
                            raise ValueError('Saved explorer history does not match this wallet and interval')
                        items=saved['transactions']
                    else:items=blockscout.account_transactions(wallet,job['start_block'],job['end_block'])
                    report['explorer_crosscheck']=explorer_crosscheck(db,job,wallet,items)
                except (ValueError,KeyError,TypeError) as exc:
                    report['explorer_crosscheck']=dict(status='unavailable',error=str(exc)[:180])
            with db:db.execute('INSERT OR REPLACE INTO wallet_history_audits VALUES(?,?,?,?)',(job_id,wallet,time.time(),json.dumps(report)))
        if rpc.block(job['end_block'])['hash']!=job['end_hash']:raise ValueError('Audit boundary reorganized')
        with db:db.execute("UPDATE wallet_history_jobs SET status='complete',error=NULL WHERE id=?",(job_id,))
        output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(reports,indent=2)+'\n')
        print(json.dumps({'job':job_id,'status':'complete','output':str(output)}),flush=True)
    except Exception as exc:
        message=str(exc)[:180] if isinstance(exc,ValueError) else type(exc).__name__
        with db:db.execute("UPDATE wallet_history_jobs SET status='error',error=? WHERE id=?",(message,job_id))
        raise ValueError(message) from None
    finally:db.close()


if __name__=='__main__':main()
