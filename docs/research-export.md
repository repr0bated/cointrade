# Offline algorithm research exports

Run from the `cointrade/` project directory with Python 3.11 or later:

```sh
PYTHONPATH=. python scripts/export-research.py --output artifacts/cointrade-research-YYYY-MM-DD
PYTHONPATH=. python scripts/finalize-research-export.py artifacts/cointrade-research-YYYY-MM-DD
```

Use a new output directory for each capture. The source defaults to
`data/gui-live.sqlite`; override it with `--db PATH` on the first command.
Keep `research` in the output directory name: the second command replaces it
with `algorithm-data` for the smaller companion bundle.

The first command reads a consistent SQLite WAL snapshot in one read-only
transaction while the scanner continues running. It exports all rows of the
included tables, preserves unresolved outcomes, records omissions, removes
credential-shaped values and provider URLs, and generates a schema, configuration,
data-quality report, documentation and checksummed ZIP. Public wallet addresses
and transaction hashes remain as join keys. This is sanitization, not anonymization.

The second command creates CSV and JSONL projections for algorithm research,
retaining all swaps, assessments, wallet outcomes and follower samples in scope.
It includes the skill inventory from [research-skills.md](research-skills.md).
The larger ZIP also includes SQLite, raw event logs and normalized chain tables.
Raw receipt/trace caches are omitted from both bundles to control size.

Upload the smaller `cointrade-algorithm-data-YYYY-MM-DD.zip` with the algorithm
generation prompt. Start with its README, manifest and quality report. No
provider connection or application deployment is needed to inspect these files.

The scripts verify included row counts, SQLite integrity and foreign keys,
archive CRCs, and file hashes. They do not validate profitability. Historical
wallet FIFO returns, delayed follower simulations and paper account fills are
different datasets with different units and availability times; their field
definitions and limitations are included in the export.

Live databases, generated exports, logs, dependency installations and environment
files are excluded from version control. Local ZIPs remain available after a
commit; they are not published by a Git push.
