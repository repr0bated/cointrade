# Astra paper terminal

Local interface: **http://127.0.0.1:3000**. Scanner: **http://127.0.0.1:8765**.

This connects to the existing Cointrade scanner, SQLite ledger, Goldsky RPC, and evidence services. Astra uses `gpt-6-astra`, high reasoning, through the signed-in Codex ChatGPT subscription. No Gemini or API-key fallback. No wallet signing or real-funds trading.

The original imported archive remains at the workspace root. Its seeded feed, random prices/P&L, fabricated wallet performance, synthetic transaction hashes, and Gemini analyst have been removed from this runnable project.

## Run locally

From `/srv/git/cointrade`:

```bash
bash scripts/start-dashboard.sh
# In another terminal, after installing/building this frontend:
bash scripts/start-terminal.sh
```

Build this frontend with `bun install`, `bun run lint`, and `bun run build` from this directory. The start script uses the production build and listens only on loopback. The proxy permits only the scanner snapshot, health check, and Astra queue controls.

## Behavior

- Blocks, detected pools/tokens, risk evidence, and activity come from the live scanner. The header shows cursor lag and freshness, not just a successful web connection.
- Every newly observed token (or newly assessed token) enters the serial Astra queue once. The first activation includes the previous two minutes. Rechecks can be requested when a new assessment exists. Queue backlog is visible.
- Reviews are persisted separately from the earlier ten-review experiment. Pauses and failures survive restart. Subscription errors stop new requests; failed requests are not silently retried.
- New paper entries require both fresh deterministic risk approval and a matching Astra SNIPE/MIRROR recommendation on the same pool, based on evidence no older than five minutes. Astra cannot bypass risk or position limits.
- Pause stops Astra requests and new paper entries. An in-progress request can finish; existing paper positions retain their exit rules.
- Quotes, slippage, and fees determine paper fills. The $40 starting account, $2 position size, 15% stop loss, and 25% take profit are the existing backend settings. Unknown/stale values and zero fills remain visible.
- Wallet history is bounded and excludes unknown-basis/unreconciled sales. No lifetime profit or predictive performance is fabricated.

The service depends on local subscription authentication and the local scanner; it is intentionally run on this computer rather than published as a disconnected hosted frontend.
