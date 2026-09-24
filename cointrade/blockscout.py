"""Authenticated, read-only explorer access; credentials never enter URLs or reports."""
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlencode
from .providers import request_json

_LOCK=threading.Lock()
_NEXT=0.0


def key():
    value=os.environ.get('BLOCKSCOUT_API_KEY')
    if value:return value
    path=Path('/home/jeremy/.config/cointrade/blockscout-api-key')
    return path.read_text().strip() if path.exists() else None


def get(path,params=None):
    global _NEXT
    secret=key()
    if not secret:raise ValueError('Blockscout API key not configured')
    if not path.startswith('/') or '://' in path:raise ValueError('Invalid explorer path')
    with _LOCK:
        delay=max(0,_NEXT-time.monotonic());_NEXT=max(_NEXT,time.monotonic())+.3
    time.sleep(delay)
    url='https://api.blockscout.com/4663'+path
    if params:url+='?'+urlencode(params)
    return request_json(url,headers={'Authorization':'Bearer '+secret})


def account_transactions(wallet,start,end):
    """Page the complete bounded external transaction list, including failures."""
    result=[];seen=set();page=1
    while True:
        data=get('/api',dict(module='account',action='txlist',address=wallet,
                            startblock=start,endblock=end,sort='asc',page=page,offset=100))
        items=data.get('result')
        if not isinstance(items,list):raise ValueError('Blockscout transaction history unavailable')
        if not items:
            if data.get('message') not in ('No transactions found','OK'):raise ValueError('Unexpected explorer history response')
            break
        if str(data.get('status'))!='1':raise ValueError('Explorer history returned an error')
        for item in items:
            if item['hash'] in seen:raise ValueError('Explorer pagination repeated a transaction')
            if not start<=int(item['blockNumber'])<=end:raise ValueError('Explorer history outside requested interval')
            seen.add(item['hash']);result.append(item)
        if len(items)<100:break
        page+=1
    return result
