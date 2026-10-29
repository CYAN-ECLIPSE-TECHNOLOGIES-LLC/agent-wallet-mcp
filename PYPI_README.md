# cyan-eclipse-agent-wallet

mcp-name: io.github.CYAN-ECLIPSE-TECHNOLOGIES-LLC/agent-wallet

**What this is:** the MCP server for **Eclipse Agent Wallet** — an
MCP server that gives SI agents a wallet they can only spend inside
a human-granted written policy: per-transaction and daily caps,
recipient allowlists, approval thresholds, expiry, and an instant,
timestamped kill switch — and proves it with an append-only audit
log.

## Honest scope

- **SANDBOX ONLY:** balances are simulated test USDC in a local JSON
  ledger. No real funds move. Production rails (x402 / partner
  processors) plug in behind the same policy engine.
- State file: `$AGENT_WALLET_LEDGER` or `./ledger.json` next to the
  server.
- Certified (sandbox) by the Eclipse Test Bench at v0.1.0 scope:
  policy enforcement (no-policy/over-cap/off-allowlist denied),
  approval queue, kill switch, append-only audit — production
  payment rails were not tested (simulated test USDC only).

## Run it

```console
$ pip install cyan-eclipse-agent-wallet
$ eclipse-agent-wallet           # MCP over stdio, Python stdlib only
```

MCP client config:

```json
{ "mcpServers": { "cyan-eclipse-agent-wallet": { "command": "uvx", "args": ["--from", "cyan-eclipse-agent-wallet", "eclipse-agent-wallet"] } } }
```

## Tools (10)

`create_wallet` · `get_wallet` · `set_policy` · `fund_wallet` · `check_payment` · `pay` · `approve_payment` · `deny_payment` · `audit_log` · `revoke`

Powered by Cyan Eclipse Technologies LLC (cetech.ai) — a Veteran-Owned
Small Business. MIT licensed.
