# Followable wallet momentum — paper experiment

Open the **Momentum experiment** tab at http://127.0.0.1:3000/?view=momentum. `scripts/start-dashboard.sh` starts the live scanner, Astra subscription reviews, three isolated paper accounts, and the archive replay workers. No component can sign or broadcast an order.

Both servers now run under [runit supervision](../services/README.md), including
startup after a machine restart. A disconnected GUI retains old timestamps and
does not imply the scanner or paper execution is still running.

Trading rules remain frozen: each account starts with $40, uses $2 entries, at most five positions, a $10 reserve, an $20 equity entry halt, an 8% stop, a 20% target, and a 15-minute timeout. The original strategy and account remain separate. The arms are wallet momentum, wallet momentum plus the existing fresh Astra MIRROR approval, and a simple momentum control without wallet qualification. The Astra arm includes actual review/queue delay and uses the existing subscription requests.

## Archive measurement v2

The earlier replay depended on later scanner assessments. Many tokens had none, so missing prices were incorrectly useful only as blanket stress assumptions. Measurement v2 archives those diagnostics in `momentum_sample_archive` and requeues their underlying buys. It preserves accounts, fills, trading rules and original chain data. A separate versioned measurement policy records this change.

The cohort contains the first observed buy per wallet/token from attributed historical flows and captured live flows, without choosing winners. Each sample resolves its pool from the leader's actual transaction and pool-creation evidence. Ambiguous or missing routes remain unresolved. Entry is at least 30 seconds after the leader, or later if recorded first-seen latency requires it. Signals received after the 120-second entry deadline are rejected. Historical detection latency is an explicit 30-second assumption where no first-seen record exists.

Archive block lookup finds the first block at or after each scheduled timestamp. Entry and every subsequent 30-second observation use canonical exact-input quotes and a synthetic-account buy/sell simulation. Atomic amounts avoid an unnecessary dependency on optional token metadata or TVL readers. The separately quoted position exit checks actual position size and prevents the synthetic buy's own liquidity from masquerading as external exit depth. Historical USD conversion uses the last fully closed one-minute ETH/USD candle, never a future candle or today's spot price. Candle ranges are cached. The format and interval limits follow the [Coinbase Exchange candle documentation](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).

Stops and targets use the available modeled net exit quote, not the threshold price. The timeout is explicitly sampled at entry plus 900 seconds. Pool fees are included in quotes; each side adds 1% assumed slippage and $0.005 assumed network cost. Each sample retains its scheduled times, block hashes, FX provenance and quotes. Identical pool/time cases share reads; each worker step advances only one observation and persists progress before continuing.

Provider/read failures retry the same observation with bounded backoff. A known entry execution failure is recorded as **entry rejected**, rather than missing history or a loss. A path with missing observations remains **incomplete**, even if a later price can be recovered. An unpriced final exit has no measured return; a separate -100% stress value is available for conservative ranking. These values never become real or paper fills. Earlier missed stops, account-specific restrictions, MEV and between-sample moves remain limitations of the model.

Replay scheduling keeps four cases from distinct pools in progress and rotates
one observation at a time, with at most four observations before returning a
pool to the scheduling queue. An early pool with many buyers, a long hold, or
missing exits cannot monopolize backfill. Unfinished paths resume at their exact
saved target; no observation is omitted. New pools join the back of the queue;
order never uses measured returns. Slots and retry targets survive restarts. The
worker continues immediately when work is ready, within the same shared RPC
rate limit. This changes processing order, not the measurement or trading rules.

Three of the four slots now prioritize the earliest observed repeat wallet with
at least ten known entries across five tokens. Its entire pending history stays
in scope, including losses and unresolved exits. Once that history is processed,
the next repeat wallet is selected. The fourth slot continues broader coverage.
Selection never uses profitability. The GUI displays this wallet's measured,
pending and total entries separately from the global outcome count.

## Timely live signals

Recent-buyer decoding starts as soon as a new pool is indexed. It does not wait
for a contract safety assessment. Its bounded queue prioritizes buys and spreads
work across tokens still lacking two verified recent buyers. The older FIFO
history reader uses two concurrent RPC readers, leaving capacity for live work.

## Astra review policy

Review packets version 2 describe the actual on-chain strategy rules. The
generic screener's one-hour minimum token age and 24-hour volume requirement do
not apply to the launch engine and are no longer supplied as launch limits.
Baseline launch and momentum policies are named separately, including their
different score and age thresholds. Momentum packets include its own current
signal and prequalified-wallet evidence. These are corrections to the review
context; deterministic trading rules and frozen account settings are unchanged.

Three review turns favor freshly assessed launches within the 15-minute
experiment window; every fourth turn serves the older backlog. Historical
reviews are preserved. Queue priority cannot guarantee every launch is reviewed
within its entry window when arrivals exceed the subscription worker's capacity.

A dedicated recent-trade worker prioritizes the last 90 seconds of activity for recently assessed tokens. It uses receipt/trace caches and block batches, verifies receipt identity and block hashes, and keeps ambiguous transactions explicit. Historical FIFO accounting continues independently, so its backlog cannot monopolize recent buyer identification.

Net buying is measured from canonical pool swap logs in the 60-second signal window. Each log's pool, block hash and decoded amount are checked; the indexed feed must be fresh. This measurement does not require identifying every trader. Buyer identities still require independent receipt-and-trace attribution: two distinct holders and transaction senders, excluding known creator/deployer/initiator addresses. Common funding and hidden coordination remain incompletely covered. The wallet arms additionally require both wallets to have qualified before the signal window.

Contract, source, holder concentration, liquidity and withdrawal-protection checks remain enforced. The original heuristic score and launch trigger are replaced by the new momentum signal. Launches must be no older than 15 minutes. Entries are requoted after actual decision latency, remain within a 2% all-in premium over the trigger price, and recheck liquidity and LP protection. Unknown safety values never become passes.

Wallet qualification requires ten fully measured follower outcomes across five tokens, 80% measured coverage, at least 55% wins, stressed mean return at least 2%, and a positive stressed median. Measured returns and stress statistics are displayed separately. Outcomes and their completion/availability timestamps must precede a triggering buy. These thresholds are an experimental hypothesis; training returns do not establish a profitable live strategy.

The dashboard separates archive progress, rejected entries, unresolved paths, measured follower returns, live paper P&L, attribution health and skip reasons. Open live positions with stale or unavailable sell quotes retain zero stressed value; exits continue retrying. The global scanner/account halt blocks new entries. Removing `--momentum-experiment` stops experiment workers without deleting saved state. Astra pause controls affect the Astra-gated arm; mechanical arms continue.
