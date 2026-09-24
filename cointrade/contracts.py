"""Independent source verification fallback, bound to the fetched runtime bytecode."""
from .providers import request_json
from . import blockscout
import hashlib


def verify_source(rpc,token,height,db=None):
    explorer=None
    if blockscout.key():
        try:
            result=blockscout.get('/api/v2/smart-contracts/'+token)
            code=result.get('deployed_bytecode')
            if result.get('is_verified') is True and result.get('source_code') and code:
                live=rpc.call('eth_getCode',[token,hex(height)])
                if code.lower()!=live.lower():raise ValueError('Explorer runtime differs from chain bytecode')
                if db is not None:
                    import json,time
                    with db:db.execute('INSERT OR REPLACE INTO chain_source_documents VALUES(?,?,?,?)',
                      (token,'blockscout',time.time(),json.dumps(result)))
                return dict(status='verified',provider='blockscout',runtime_match='exact',block=height,
                  code_sha256=hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
                  contract_name=result.get('name'),compiler=result.get('compiler_version'),
                  source_files=1+len(result.get('additional_sources') or []),
                  abi_functions=sum(x.get('type')=='function' for x in result.get('abi') or []),
                  proxy_type=result.get('proxy_type'),implementations=result.get('implementations') or [])
            explorer=dict(status='unverified' if result.get('is_verified') is False else 'unavailable',provider='blockscout')
        except ValueError as exc:
            explorer=dict(status='unavailable',provider='blockscout',error=str(exc))
    try:
        result=request_json(f'https://sourcify.dev/server/v2/contract/4663/{token}?fields=runtimeBytecode.onchainBytecode,abi,deployment')
        matched=result.get('runtimeMatch') in ('match','exact_match')
        code=result.get('runtimeBytecode',{}).get('onchainBytecode')
        if matched and code and code.lower()==rpc.call('eth_getCode',[token,hex(height)]).lower():
            return dict(status='verified',provider='sourcify',runtime_match=result['runtimeMatch'],
                        deployment=result.get('deployment'),abi=result.get('abi'))
        fallback=dict(status='unavailable',provider='sourcify',error='No matching runtime bytecode')
    except ValueError as exc:
        fallback=dict(status='unavailable',provider='sourcify',error=str(exc))
    if explorer:
        explorer['fallback']=fallback
        return explorer
    return fallback


def deployment_origin(trace,token):
    """Return actual CREATE/CREATE2 deployer separately from transaction initiator."""
    if trace.get('error'):return None
    stack=[trace]
    while stack:
        frame=stack.pop()
        if frame.get('error'):continue
        if frame.get('type','').upper() in ('CREATE','CREATE2') and frame.get('to','').lower()==token:
            return dict(deployer=frame['from'].lower(),initiator=trace['from'].lower(),source='creation_call_trace')
        stack.extend(frame.get('calls',[]))
    return None
