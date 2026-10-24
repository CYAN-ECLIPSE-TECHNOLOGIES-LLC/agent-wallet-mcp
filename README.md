# Eclipse Agent Wallet MCP

**Powered by Cyan Eclipse Technologies** — https://cetech.ai
Listed on Eclipse Bazaar (eclipsebazaar.com).

## Why this exists

On **November 24, 2026**, Wells Fargo's Online Access Agreement Section 15 takes effect: anything your AI agent does with your bank account is legally *you* — "at your own risk," including transactions you did not want or intend, with no controls attached and no duty on the bank to watch.

This MCP server is the opposite design: **an agent wallet where responsibility follows scope.**

- The human owner grants a written **policy**: per-transaction cap, daily cap, approval threshold, recipient allowlist, expiry, purpose.
- The agent can spend **only inside that scope**. Outside it, payments are rejected — or routed to a **human approval queue** when they exceed only the approval threshold.
- Every grant, payment, rejection, approval and revocation is written to an **append-only audit log** — the evidence of what was actually authorized.
- A **kill switch** freezes the wallet instantly, with the revocation timestamped in the log. Revoking access to your bank means "notify us and wait"; here it is immediate and provable.

## Status

v0.1.0 reference implementation. **Sandbox only** — balances are simulated test USDC in a local JSON ledger. No real funds, no private keys, no network calls. The policy engine is the product; production payment rails (x402, partner processors) plug in behind it without changing the tool surface.

## Tools (10)

| Tool | What it does |
|---|---|
| `create_wallet` | Create a wallet owned by a named human. Cannot spend until a policy is granted. |
| `get_wallet` | Balance, frozen state, today's spend, active policy. |
| `set_policy` | Owner grants the spending scope; returns a plain-language **liability receipt**. |
| `fund_wallet` | Sandbox faucet (simulated test USDC) for demos/tests. |
| `check_payment` | Dry-run: returns allow / approve / deny with the reason. Agents call this before `pay`. |
| `pay` | Execute inside scope; reject outside it; queue for approval above the threshold. |
| `approve_payment` / `deny_payment` | Owner resolves a queued request. |
| `audit_log` | The full append-only trail. |
| `revoke` | Kill switch — freeze instantly. |

## Run it

```bash
python3 server.py            # MCP over stdio, stdlib only, no dependencies
```

State persists in `ledger.json` beside the server (override with `AGENT_WALLET_LEDGER`).

### Use it with your Muse (custom connector / local MCP)

Point your agent's MCP configuration at the command `python3 /path/to/server.py` — any MCP client (Muse, Muse, Cursor) can drive it. A worked example session:

1. `create_wallet {owner_id: "you@example.com"}` → wallet created, cannot spend.
2. `set_policy {per_tx_cap_usdc: "50.00", daily_cap_usdc: "120.00", approval_above_usdc: "20.00", allowlist: ["0xMerchantA"], purpose: "API credits"}` → liability receipt issued.
3. `pay 10.00 to 0xMerchantA` → executed. `pay 60.00` → rejected (over cap). `pay 30.00` → human approval required.
4. `revoke` → frozen, timestamped. Agent's authority ends the second the owner says so.

## Roadmap

- v0.2: hosted HTTP transport + per-user API keys; policy templates (shopping, SaaS, research budgets).
- v0.3: real rails behind the same policy engine — x402/USDC settlement, partner-processor fiat — only under the owner's granted scope.
- v1.0: Eclipse Bazaar listing with live test-bench scorecard (policy-engine conformance suite runs against every release).

## License / attribution

A Cyan Eclipse Technologies product. The "Powered by Cyan Eclipse Technologies" attribution travels with it.
