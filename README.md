# Cointrade

A runnable **paper-trading** experiment with a $40 simulated bankroll. Python 3.11+; no runtime dependencies. There is no wallet signer, exchange account integration, or live order endpoint.

For offline algorithm research, see [sanitized data exports](docs/research-export.md)
and the [provider and crypto skill inventory](docs/research-skills.md).

## Live GUI

```sh
python -m cointrade --db data/gui-live.sqlite gui --scan
```

Open **http://127.0.0.1:8765**. The GUI shows the Robinhood Chain event feed, new pools, risk decisions, wallet evidence, simulated positions/fills, and AI routing/spend. It refreshes from SQLite every two seconds. Scanner progress, backlog, provider errors and browser connection state are displayed separately. Pause view freezes the display only; the scanner continues. Rows open an inspector with evidence and explorer links. The histogram uses actual indexed event counts, not simulated chart data.

`--scan` runs chain polling alongside the GUI, resuming the selected database. Omit it for a read-only viewer of an existing database, e.g. `--db data/robinhood.sqlite gui`. The default lookback for a new GUI database is 100 blocks; `--lookback` changes that initial window. The GUI session uses its own account when given a new database path; it does not merge previous experiment histories. `--port` changes the listening port. The default address is local-only; the server has no authentication, so keep it local or use an SSH tunnel. Ctrl-C stops the server and its scanner worker. Temporary scanner failures retry with backoff; reorganization errors stop the worker for investigation. No AI calls are triggered by viewing the dashboard or running its scanner.

## Primary experiment: newly launched on-chain tokens

The target is **Robinhood Chain → deterministic detection and scoring → optional AI analysis → risk rules → paper execution**. The Coinbase momentum demo below is an auxiliary accounting test, not a reproduction of the reference post.

```sh
python -m cointrade --db data/robinhood.sqlite onchain --cycles 100 --interval 2
python -m cointrade --db data/robinhood.sqlite onchain-status
python -m cointrade --db data/robinhood.sqlite launches
python -m cointrade --db data/robinhood.sqlite wallets
```

The indexer verifies chain ID 4663 and the configured Uniswap deployments, then follows ERC20 mint events and canonical Uniswap V2/V3/V4 pool creation, liquidity-change and swap logs. It stores raw logs, observed token bytecode/owner/decimals, deployment evidence, pool launch timestamps, and transaction senders. A new pool is not automatically a new token: the previous block must have no token code to confirm fresh deployment. Mint events can also come from existing tokens. The creator of an internal deployment is not assumed to be the transaction sender.

This version uses **confirmed RPC log polling**, with two descendant blocks by default. It is not yet a WebSocket/Nitro sequencer-feed consumer and cannot promise sub-second sniping or detection of every launchpad. `ROBINHOOD_RPC_URL` can point to an HTTPS provider; the default is the public rate-limited endpoint. Indexing makes real network requests and may require paid/archive infrastructure at larger scale. `--lookback 1000`, `--batch-blocks 200`, and `--confirmations 2` control the initial window and fetch size. The cursor resumes without duplicating events. RPC failures preserve the cursor and stop the command; a detected reorganization halts the paper account, requiring a new database to reindex. Two-block confirmation is not an Ethereum-finality guarantee.

Pool activity is followed for the first day and longer for held positions. The GUI runs independent indexing, enrichment, and wallet-reconstruction workers. Indexing stores raw evidence first; deferred metadata and transaction traces cannot stall the block cursor. Goldsky requests share a rate limiter. Oversized log requests split by block range without advancing past failed fetches.

Wallet P&L uses FIFO lots with gas costs, measured in ETH/WETH. The trace worker reconstructs net token and native/wrapped ETH cash flows for the transaction origin or its directly called trading contract. Reverted calls and inherited DELEGATECALL values do not count as transfers; canonical pools and routers are excluded as wallet candidates. Transactions with ambiguous ownership/assets and unmatched sell basis remain ineligible for mirroring. Historical reconstruction resumes from persisted transaction reviews. It is observed-window history, not a lifetime wallet profile. Deployment traces distinguish CREATE/CREATE2 deployers from transaction initiators; creator associations cover the indexed window only.

Security evidence combines GoPlus, authenticated Blockscout and Sourcify source fallbacks, and on-chain checks. Explorer source verification requires the returned deployed bytecode to match the archive RPC bytecode at the assessment block. Source states distinguish verified, provider-reported unverified, and unavailable; source verification alone does not establish safe permissions. Holder balances reconstructed from deployment Transfer logs must reconcile to both totalSupply and balanceOf. A runtime-checked Multicall3 batches balance reads without treating failed calls as zero. V2 liquidity and burned LP are read directly. V3 liquidity uses pool balances; V4 core liquidity uses the canonical ReservesLens, not the shared PoolManager's aggregate balance.

The GUI scanner, metadata worker, evidence worker, and wallet backfill share a cross-process RPC rate limiter. Metadata enrichment progresses independently of slower security and wallet checks. `BLOCKSCOUT_API_KEY` or the protected `~/.config/cointrade/blockscout-api-key` file enables read-only explorer requests; the key is sent in an authorization header, never a URL.

For a bounded wallet-history audit using archive logs, full receipts and call traces:

```sh
COINTRADE_RPC_PROVIDER=goldsky python -m cointrade.history \
  --db data/gui-live.sqlite --hours 24 --wallet 0xYOUR_WALLET_ADDRESS \
  --output artifacts/wallet-history.json
# Resume an interrupted job without discarding its cached progress:
COINTRADE_RPC_PROVIDER=goldsky python -m cointrade.history \
  --db data/gui-live.sqlite --resume 1 --output artifacts/wallet-history.json
```

Repeat `--wallet` to audit multiple addresses. Timestamp-based block boundaries and paginated log ranges define the exact interval. Opening balances plus every observed token transfer must reconcile to ending archive balances. Opening and transferred-in inventory have unknown cost basis; those sales cannot become fabricated profits. Only reconciled sales with known FIFO cost contribute to the reported realized return. This excludes unrealized holdings and is not whole-wallet or lifetime performance.

When a Blockscout key is configured, the audit also fetches paginated external transaction history, including failures and transactions without token events, and checks overlapping receipts for matching block hash, status, sender, recipient and gas. Those additional rows remain explorer-reported; they are not a full native-balance reconciliation. `--explorer-history FILE` can reuse a saved JSON list containing each wallet, start/end blocks and its transactions. Targeted audits temporarily receive priority over the general wallet backfill. Job state and completed reports are available under `wallet_history` in `/api/state`; a completed audit does not automatically qualify a wallet for mirroring.

Canonical V2 reserve math and V3/V4 quoters produce size-specific pool quotes. Read-only eth_simulateV1 performs buy, approvals, and full acquired-balance sell through canonical routers from a funded synthetic account. It measures transfer shortfalls separately from pool fees/price impact. A simulation applies only to that account and block; it cannot prove future permissions or safety. Paper entry quantities use the observed simulated buy output; exit quotes include pool fees without charging them twice. Missing source/privilege evidence, unreviewed hooks, failed simulation, excessive impact, or unproven liquidity withdrawal protection still reject entries. Time-lock and V3/V4 position-lock proofs are not inferred from the presence of liquidity.

When all implemented gates pass, **SNIPE** requires a confirmed token deployment within five minutes and at least three attributable recent buyers; **MIRROR** requires an eligible wallet's attributable recent buy. Otherwise the decision is **SKIP**. V2 reserve quotes are converted from WETH to USD using a public ETH/USD reference. That off-chain conversion is an approximation. Paper fills are prohibited while backfilling or when indexed blocks are stale. The existing cash, exposure, cooldown and equity controls are applied after launch scoring. Automatic AI escalation is not yet attached to this scanner; explicit analysis commands below provide the routing interface without sending every event to a model.

Candidate evidence scores start at zero. Version 2 awards up to 10 points for published source, 20 for known contract-control checks, 15 for executable quotes and simulated trading costs, 15 for liquidity, 10 for holder distribution, 10 for liquidity withdrawal protection, 15 for verified recent buyers, and 5 for qualified-wallet activity. Every component and its unresolved evidence is visible in the decision inspector. Weights are an uncalibrated heuristic, not probability of profit or a safety verdict; a risk failure blocks trading regardless of score. Holder distribution still includes pool/custody addresses and is explicitly labeled that way. Historical dashboard rows show the current formula applied to their saved evidence while preserving the recorded score and action.

Recent buyer attribution checks up to four recent transactions per token assessment using complete receipts and call traces, independently of the FIFO P&L backlog. Unprocessed and ambiguous transactions remain explicit; verified buyer counts are lower bounds. Recent launches with new swap activity receive reassessment priority. Pool selection is bounded to the assessment block, and launch age uses that block's timestamp so concurrent indexing cannot create negative ages.

The first mainnet smoke run discovered 10 pools, indexed 91 swaps and observed 81 transaction-origin addresses. It had zero qualified wallet histories and zero paper fills. These are indexing checks, not performance results. The test suite covers ABI directions, spoofed factory addresses, cursor failure/reorganization behavior, FIFO accounting, missing-risk rejection and SNIPE/MIRROR/SKIP precedence.

## Live prices, simulated trades

From this directory, run an hour of public Coinbase BTC, ETH and SOL data:

```sh
python -m cointrade --db data/coinbase.sqlite coinbase --cycles 60 --interval 60
```

No account or API key is needed for these public market-data requests. Each cycle prints portfolio JSON. Omit `--cycles` for one cycle, or use `--product BTC-USD` (repeatable) to narrow the watchlist. The process runs in the foreground; Ctrl-C stops it, and the same command resumes its saved account. A cycle's fetch time is additional to the interval. This is polling, not a tick-by-tick exchange simulator.

```sh
python -m cointrade --db data/coinbase.sqlite status
python -m cointrade --db data/coinbase.sqlite events --limit 10
python -m cointrade --db data/coinbase.sqlite trades
python -m cointrade --db data/coinbase.sqlite halt
```

`halt` persists across restarts. Subsequent scans close positions when fresh quotes arrive; it cannot guarantee exits during an outage. Use a new database to start a new experiment. There is deliberately no automatic reset of losses or halt state.

The Coinbase baseline compares five-minute and twenty-minute simple moving averages using 20 consecutive, completed one-minute candles. It enters when the short average is at least 0.05% above the long average and at least half of consecutive closes rise. Spread must be at most 20 basis points. Quotes older than 120 seconds are rejected; up to 15 seconds of provider clock skew is tolerated. Gaps and stale candles block entries. Existing holdings are always polled, even when omitted from a subsequent command's watchlist.

These are unvalidated example rules. They implement a repeatable experiment, not a demonstrated trading edge. The latest trade timestamp is used conservatively as a freshness proxy for the ticker's bid/ask.

## Offline demo

```sh
python -m cointrade --db data/demo.sqlite replay examples/demo.jsonl
python -m cointrade --db data/demo.sqlite trades
python -m unittest discover -s tests -v
```

The synthetic demo buys a qualifying token, rejects a token with active mint authority, and exits the first at its take-profit threshold. Its profit is scripted fixture output, not measured strategy performance. Replaying it again skips timestamps already processed, so it cannot duplicate fills. Replay input must be chronological; timestamps at or below the saved watermark are ignored. One JSON object per line: `{"ts": <epoch seconds>, "snapshots": [...]}`. See the fixture for the complete snapshot schema.

## Solana token experiment

```sh
python -m cointrade --db data/solana.sqlite scan --token MINT_ADDRESS
python -m cointrade --db data/solana.sqlite scan --discover 5 --cycles 60 --interval 60
```

DEX Screener supplies token pools, price, liquidity, volume, and activity. Discovery samples latest **token profiles**, not all new mints. The adapter selects the most liquid pool where the watched token is the base token. `SOLANA_RPC_URL` optionally supplies an HTTPS RPC endpoint; otherwise it uses the public mainnet endpoint. Rate limits or missing RPC safety information reject entries but preserve available prices for existing-position exits.

Only standard SPL mints with revoked mint and freeze authorities qualify. Token-2022 and unknown programs are excluded. The top ten token accounts must hold at most 50% of supply. This is an account concentration proxy, not wallet-owner analysis: pool vaults are included, while wallets split across accounts may be missed. Defaults also require $25k liquidity, $10k daily volume and an hour-old pool, plus positive five-minute activity. The liquidity and authority checks do not prove sellability or exclude every malicious token. No pool-lock verification, wallet copy trading, or wallet profitability ranking is implemented yet.

## Accounting and risk limits

Defaults: $40 cash; $10 cash reserve; $2 all-in entry budget; at most five simultaneous positions and $30 entry-cost exposure. The $20 equity circuit breaker permanently blocks new entries. Exits trigger at a 15% price decline, 25% rise, or one-hour holding period. Sold assets have a one-hour re-entry cooldown. Those thresholds are sample experiment settings.

Each fill applies 30 bps simulated trading fees, 100 bps adverse slippage and a $0.005 fixed cost. Coinbase buys start at ask and sells at bid before slippage; Solana fills use the reported pool price. The fixed cost is a generic stress assumption, not a claim that Coinbase charges a blockchain fee per exchange trade. The $2 buy budget includes fees. Recorded realized P&L includes both entry and exit costs. Portfolio equity uses the last observed mark and excludes future exit costs; stale marks are flagged. Missing portfolio quotes block additional entries. Floating-point arithmetic is adequate for this simulator, but is not a production settlement ledger.

These costs, position sizes, and fill assumptions do not enforce actual exchange minimums, increments, order-book depth, or venue-specific fees. Polling can miss brief triggers, and stops fill at the next available quote, not at a guaranteed stop price.

Print or customize settings:

```sh
python -m cointrade config
python -m cointrade --config examples/config.json --db data/custom.sqlite coinbase
```

Settings and source are pinned in each SQLite account. Changing either requires a new database. This keeps synthetic replay, Coinbase, and on-chain experiments separate. SQLite transactions make a tick's decisions, fills and restart watermark atomic. `events` stores input snapshots and every decision's reasons; `trades` stores cash flows and realized P&L.

## AI routing and explicit Astra work

The deterministic scan loop never invokes AI. Separate, explicitly enabled review workers can run alongside it.

The live dashboard now uses `--astra-subscription`: an initial ten-review comparison through the locally signed-in Codex CLI, selecting `gpt-6-astra` with high reasoning. It reviews fresh assessed tokens one at a time and stores rationale, missing evidence, measured failures, and data-quality findings. The AI tab compares each recommendation with the original scanner decision. This is advisory: it cannot change risk rules or execute trades. Subscription usage limits apply; there is no API-key fallback. Restarts preserve the ten-review limit. A failed or interrupted review stops the experiment instead of silently retrying. The previous OpenRouter experiment remains cancelled with its allocation/history intact.

```sh
codex login status
python -m cointrade --db data/gui-live.sqlite gui --scan --astra-subscription
```

This uses the official client's saved ChatGPT authentication and isolated, read-only review sessions. API-key environment variables and user provider overrides are excluded from those sessions. The CLI selects the model explicitly; the dashboard labels that selection rather than claiming API-reported model metadata. Review sessions receive the supplied evidence and have no shell, browser, plugin, or agent-delegation tools enabled.

To request a separate API-backed report, set `OPENROUTER_API_KEY` in your shell and run:

```sh
python -m cointrade --db data/coinbase.sqlite analyze
```

In OpenRouter, use **`openrouter/auto`** for model selection and leave Allowed Models unrestricted initially. The application requests a cost tier per task. Use **`openai/gpt-6-astra`** explicitly for the separate development route. Choosing the `high` Auto Router tier does not guarantee Astra.

| Work | Route |
|---|---|
| Prices, liquidity, concentration, authorities, transactions, wallet P&L, sizing, risk | Deterministic code; no model |
| Unusual activity, wallet summaries, conflicting signals | Auto Router `low` |
| Complex pattern analysis | Auto Router `medium`, explicitly escalatable to `high` |
| Debugging, algorithm design, failure analysis, code improvements | Explicit `openai/gpt-6-astra`; no model fallback |

```sh
# Inspect the policy without an API key or a paid request:
python -m cointrade ai-route wallet_summary
python -m cointrade ai-route pattern_analysis --tier high
python -m cointrade ai-route debugging

# Explicit paid requests, using JSON evidence you choose to send:
python -m cointrade --db data/robinhood.sqlite analyze --task pattern_analysis --tier medium --input evidence.json
python -m cointrade --db data/robinhood.sqlite analyze --task debugging --input failure.json
```

`OPENROUTER_ALLOWED_MODELS` may optionally contain a JSON array of allowed model IDs/patterns. It applies only to Auto Router; the application has no default cheap-only allowlist. Evidence files are sent as provided: do not include secrets. Without an input file, only numeric portfolio summaries are sent. Responses are advisory and cannot execute trades, apply code, or change risk settings. Both the requested route and actual selected model are logged.

The persistent allowance remains ten cents/day and $3/calendar month (UTC), **per database**. Low requests reserve 2 cents, medium 4 cents, and high/Astra 10 cents. Thus one high/Astra request consumes the default day's entire allowance and is rejected if earlier requests used any of it. This small experimental budget supports short analyses, not a sustained autonomous development loop. Reservations persist even on failed or interrupted requests; reported costs above the reservation are charged to the ledger.

Low input/output limits are 6,000 evidence bytes/512 output tokens; medium 4,000/1,024; high and Astra 2,500/1,024. Provider input/output price caps per million tokens are respectively $1/$2, $3/$12 and $10/$50, with zero per-request fees. `xhigh` and `max` exist upstream but are not enabled in this application's budget policy. These controls bound normal usage; external billing and tokenization remain provider-controlled. A dedicated OpenRouter key with a provider-side credit limit supplies an independent spending boundary across databases and other clients. Credit-purchase fees are outside the local ledger. No API requests or model escalations are retried automatically.

API expenses are separate from simulated trading cash. No paid calls are needed to run market simulations, and this prototype does not validate the earlier $5/month operating-cost estimate.

## API references

- [Coinbase public ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker) and [candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
- [DEX Screener API](https://docs.dexscreener.com/api/reference)
- Solana [getAccountInfo](https://solana.com/docs/rpc/http/getaccountinfo) and [getTokenLargestAccounts](https://solana.com/docs/rpc/http/gettokenlargestaccounts)
- OpenRouter [Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router) and [provider price caps](https://openrouter.ai/docs/guides/routing/provider-selection)
- Robinhood Chain [network endpoints](https://docs.robinhood.com/chain/connecting/) and [canonical WETH](https://docs.robinhood.com/chain/contracts/)
- Uniswap [Robinhood deployment addresses in governance](https://gov.uniswap.org/t/temp-check-protocol-fee-expansion-robinhood-chain/26168)
- GoPlus [token security API](https://github.com/GoPlusLabs/OpenAPI/blob/main/SecurityAPI.md)
- OpenAI [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)

Robinhood Chain is integrated as described above. The Robinhood brokerage API is not integrated.

Goldsky endpoint setup (2026-09-24): `cointrade-robinhood` was created through
Goldsky's project API. Its separate RPC key is stored outside this repository at
`~/.config/cointrade/goldsky-rpc-key` with mode 0600. The project API key is not an
RPC endpoint key. Use `scripts/start-dashboard.sh` to run the live dashboard with
Goldsky. The script does not enable the cancelled Astra experiment. No subscription
upgrade or Turbo pipeline was created. RPC access, historical receipts, and
`debug_traceTransaction` were verified against Robinhood Chain (4663).

Data-repair validation: the raw scanner caught up to its two-block confirmation target;
three reconstructed native cash flows matched independent prestateTracer state differences
after gas reconciliation. A live V4 synthetic round trip completed with no token-transfer
shortfall but approximately 98.6% loss from pool economics, correctly triggering rejection.
The suite covers ABI encoding, reverted-trace exclusion, native cash
flows, holder reconciliation, quote accounting, and distinguishing missing source evidence.
Historical wallet reconstruction and per-token enrichment continue in the background.
Unknown source publication, permissions, or lock proofs remain explicit evidence gaps;
they are not converted into safety passes.

### Astra terminal with live data

The imported React terminal at `imports/robinhood-terminal` now reads this scanner's real records and paper ledger. Start `scripts/start-dashboard.sh` and `scripts/start-terminal.sh`; open **http://127.0.0.1:3000**. See the terminal README for build steps. Its prior demo feed, wallet returns, random P&L, fake trade hashes, and Gemini analyzer have been removed.

`gui --astra-live` continuously reviews detected tokens through the signed-in ChatGPT subscription using `gpt-6-astra` / high reasoning. The persistent serial queue, responses, errors, and pause/resume controls are visible in the terminal. Each token is automatically reviewed once; a recheck requires a new saved assessment. This mode is separate from the original ten-review comparison, and never resets its history or reactivates the $3 API experiment. Subscription failure stops requests without a paid fallback.

Astra adds a paper-entry gate: both its recommendation and current deterministic risk rules must pass, with matching pool/action and evidence at most five minutes old. Pausing prevents new paper entries while keeping position exits active. Existing bankroll, exposure, fees, slippage, and exit rules remain enforced. No real-funds execution is implemented.

### Relational evidence and data quality

The live collector now links blocks, transactions, accounts, observed contract code, verified ERC20 balances, and decision measurements through an additive SQLite schema. The Safety evidence tab shows observation times, block references, typed gaps, and bounded refresh attempts. Astra receives the same validated measurement contract and can request read-only refreshes; unresolved evidence continues to block paper entries. See [the schema and coverage guide](docs/blockchain-schema.md) and [the versioned JSON contract](docs/evidence-v1.schema.json).
