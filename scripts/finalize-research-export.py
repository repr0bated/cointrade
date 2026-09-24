#!/usr/bin/env python3
"""Finalize export documentation and produce a smaller algorithm-research bundle."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

folder = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('research_export', Path(__file__).with_name('export-research.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# Refresh documentation from the corrected generator, without touching data.
(folder/'README.md').write_text(module.README)
manifest = json.loads((folder/'manifest.json').read_text())
readme = folder/'README.md'
manifest['files']['README.md'] = {'bytes':readme.stat().st_size,'sha256':hashlib.sha256(readme.read_bytes()).hexdigest()}
module.write_json(folder/'manifest.json',manifest)
archive = folder.with_suffix('.zip')
if shutil.which('zip'):
    subprocess.run(['zip','-q',str(archive),'README.md','manifest.json'],cwd=folder,check=True)
else:
    temp = archive.with_suffix('.tmp.zip')
    with zipfile.ZipFile(temp,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(folder.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(folder))
    temp.replace(archive)
with zipfile.ZipFile(archive) as z:
    assert z.read('README.md') == readme.read_bytes()
    assert len(z.namelist()) == len(set(z.namelist()))

small = folder.with_name(folder.name.replace('research','algorithm-data'))
small.mkdir(exist_ok=False)
shutil.copytree(folder/'csv',small/'csv')
for name in ('data_quality.json','strategy_settings.json','wallet_rankings_snapshot.json','SKILLS.md'):
    shutil.copyfile(folder/name,small/name)
db=sqlite3.connect((folder/'research.sqlite').as_uri()+'?mode=ro',uri=True)
db.row_factory=sqlite3.Row
exports={}
csvs={
    'swaps.csv':'''SELECT s.*,e.block,e.block_hash FROM chain_swaps s LEFT JOIN chain_events e
        ON e.tx=s.tx AND e.log_index=s.log_index ORDER BY s.ts,s.tx,s.log_index''',
    'tokens.csv':'SELECT token,first_block,first_ts,deployment_confirmed,decimals,code_hash,owner FROM chain_tokens',
    'wallet_flows.csv':'SELECT * FROM wallet_flows ORDER BY ts,tx',
    'wallet_unknown.csv':'SELECT * FROM wallet_unknown',
    'wallet_review_status.csv':'SELECT tx,ts,status,reason FROM wallet_reviews',
    'live_trade_observations.csv':'SELECT * FROM live_trade_observations',
    'live_flows.csv':'SELECT * FROM momentum_flows ORDER BY ts,tx',
    'replay_members.csv':'SELECT * FROM momentum_replay_members',
    'historical_fx.csv':'SELECT * FROM momentum_fx_candles ORDER BY start',
    'paper_trades.csv':'SELECT * FROM momentum_trades',
    'original_paper_trades.csv':'SELECT * FROM trades',
}
for name,sql in csvs.items():
    module.csv_query(db,small/'csv'/name,sql)
    count=db.execute('SELECT COUNT(*) FROM ('+sql+')').fetchone()[0]
    exports['csv/'+name]={'rows':count,'query':sql}
    print(name,count,flush=True)
(small/'jsonl').mkdir()
for table in ('launch_decisions','astra_live_reviews','momentum_decisions','momentum_replay_cases','momentum_samples','evidence_measurements','evidence_findings','chain_holder_checks','chain_source_checks','chain_risks'):
    count=0
    with (small/'jsonl'/f'{table}.jsonl').open('w') as f:
        for row in db.execute(f'SELECT * FROM "{table}"'):
            record=dict(row)
            for key,value in record.items():
                if isinstance(value,str) and value.lstrip().startswith(('{','[')):
                    try:record[key]=json.loads(value)
                    except ValueError:pass
            f.write(module.encoded(record)+'\n');count+=1
    assert count==manifest['included_tables'][table]['rows']
    exports['jsonl/'+table+'.jsonl']={'rows':count,'source_table':table,'scope':'All rows from the sanitized snapshot. JSON object/array text columns decoded into nested values.'}
db.close()
intro='''# Algorithm research input

Upload this package with the algorithm-generation prompt. Ask for algorithm
specifications to bring back to the existing implementation agent; no new app,
provider setup, trading credentials or transaction execution is needed.

Start with data_quality.json, strategy_settings.json and SKILLS.md, then inspect
csv/ and jsonl/. These are real recorded observations and simulated historical
outcomes from a static snapshot, not a live feed or validated profitable strategy.
Use Python csv/json (standard library); no special plugin is required.

## Package contents
- All recorded swap rows, with their original block number/hash joined from logs.
- Pool and token identifiers/decimals; token metadata and runtime bytecode omitted.
- All launch assessments and their detailed evidence, reasons and Astra reviews.
- Wallet flows, closed FIFO outcomes, attribution status and unresolved basis.
- Delayed follower samples, shared replay paths, membership and historical FX.
- Original and isolated paper fills (currently empty, with CSV headers preserved).
- Strategy configuration, source snapshot quality report, skill capability list.

manifest.json lists exported files, row counts, hashes and source snapshot time.
The quality report describes the source snapshot, including tables not bundled
here. The fuller companion research ZIP has SQLite, raw logs and normalized chain
tables; neither bundle includes the bulky raw receipt and trace caches.

CSV blank fields mean missing values. JSONL means one JSON object per line;
JSON object/array columns are decoded, while exact atomic integer strings are
preserved. Load address/hash/atomic-amount CSV columns as text (pandas dtype=str)
to avoid float rounding, leading-zero loss, or spreadsheet interpretation.

Do not interpret zero paper fills as zero source activity. Do not omit rejected,
pending or unpriceable outcomes to make profitability appear stronger.
The included rankings are valid only at the export time, not earlier decisions.
The short history cannot establish an enduring edge; prioritize forward tests.

'''
# Keep the reviewed field/unit guide, without references to SQLite-only usage.
guide=module.README.split('## Semantics and joins\n',1)[1]
(small/'README.md').write_text(intro+'## Source field dictionary and limitations\n'+guide)
(small/'EMERGENT_HANDOFF.txt').write_text(module.HANDOFF.replace('2. Inspect research.sqlite using Python sqlite3 or another local SQLite reader.','2. Inspect csv/ with Python csv and jsonl/ with Python json. All JSONL records are one object per line.'))
sm={'format_version':1,'snapshot_utc':manifest['snapshot_utc'],'chain_id':4663,
    'purpose':'Algorithm specifications only; static sanitized source records and modeled outcomes.',
    'source_full_export':archive.name,'full_source_table_counts':{k:v['rows'] for k,v in manifest['included_tables'].items()},
    'scope':'No date, outcome or winner selection in the exported table projections. Exact included projections are recorded below. Not every source table is present.',
    'limitations':'No raw receipt/trace cache, runtime bytecode, full raw logs, database, credentials, connectivity or execution capability. data_quality.json profiles the full source snapshot; not all its tables are bundled here.',
    'sanitization':manifest['sanitization_policy'],'exports':exports,'files':{}}
for p in sorted(small.rglob('*')):
    if p.is_file():sm['files'][str(p.relative_to(small))]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
module.write_json(small/'manifest.json',sm)
sz=small.with_suffix('.zip')
with zipfile.ZipFile(sz,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in sorted(small.rglob('*')):
        if p.is_file():z.write(p,p.relative_to(small))
with zipfile.ZipFile(sz) as z:
    assert z.testzip() is None
    for name,info in sm['files'].items():assert hashlib.sha256(z.read(name)).hexdigest()==info['sha256'],name
print(json.dumps({'compact_archive':str(sz),'compact_bytes':sz.stat().st_size,'full_archive':str(archive),'full_bytes':archive.stat().st_size,'snapshot':manifest['snapshot_utc'],'validated':'ZIP CRC, per-file hashes, exact source row counts, SQLite integrity/FKs'}),flush=True)
