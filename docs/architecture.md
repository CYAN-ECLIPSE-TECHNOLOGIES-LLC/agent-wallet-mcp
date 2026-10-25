# Architecture

Single-file stdlib Python MCP server (`server.py`) over stdio.

- **Policy engine** — evaluates every payment against the active policy:
  per-transaction cap, daily cap, allowlist, expiry, frozen state. Outcomes:
  execute, reject with reason, or queue for human approval.
- **Ledger** — local JSON file (`ledger.json` / `AGENT_WALLET_LEDGER`):
  wallets, policies, balances, approval queue, append-only audit entries.
- **Data flow** — MCP client → stdio JSON-RPC → tool handler → policy
  check → ledger write → response. No network calls, no external services.
- **Roadmap seams** — production payment rails (x402, partner processors)
  and hosted HTTP transport plug in behind the same tool surface.
