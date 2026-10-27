# Eclipse Agent Wallet MCP

**Powered by Cyan Eclipse Technologies** — https://cetech.ai · Listed on Eclipse Bazaar (eclipsebazaar.com).

An MCP server that gives an SI agent a wallet it can only spend inside a
human-granted written policy: per-transaction cap, daily cap, approval
threshold, recipient allowlist, expiry, and purpose. Payments outside the
scope are rejected, or routed to a human approval queue when they exceed
only the approval threshold. Every grant, payment, rejection, approval and
revocation is written to an append-only audit log, and a kill switch
(`revoke`) freezes the wallet instantly with a timestamped record.

## Who it is for
Owners who let SI agents spend on their behalf and need provable,
scope-limited authority instead of blanket account access.

## Status
v0.1.0 reference implementation. **Sandbox only** — balances are simulated
test USDC in a local JSON ledger. No real funds, no private keys, no network
calls. Hosted HTTP transport, policy templates, and real payment rails
(x402 / partner processors) are on the roadmap, behind the same tool surface.

## Quick start
```bash
python3 server.py   # MCP over stdio, stdlib only, no dependencies
```
Then, from any MCP client: `create_wallet` → `set_policy` → `fund_wallet`
(sandbox faucet) → `pay` / `check_payment`. See Usage.
