# Usage — tool reference

All tools are MCP tools over stdio (newline-delimited JSON-RPC).

| Tool | What it does |
|---|---|
| `create_wallet` | Create a wallet owned by a named human. Cannot spend until a policy is granted. |
| `get_wallet` | Balance, frozen state, today's spend, active policy. |
| `set_policy` | Owner grants the spending scope; returns a plain-language liability receipt. Policy fields include `per_tx_cap_usdc`, `daily_cap_usdc`, `approval_above_usdc`, `allowlist`, `purpose`. |
| `fund_wallet` | Sandbox faucet (simulated test USDC) for demos/tests. |
| `check_payment` | Dry-run: returns allow / approve / deny with the reason. Call before `pay`. |
| `pay` | Execute inside scope; reject outside it; queue for approval above the threshold. |
| `approve_payment` / `deny_payment` | Owner resolves a queued request. |
| `audit_log` | The full append-only trail. |
| `revoke` | Kill switch — freeze the wallet instantly. |

## Worked example
1. `create_wallet {owner_id: "you@example.com"}` → wallet created, cannot spend.
2. `set_policy {per_tx_cap_usdc: "50.00", daily_cap_usdc: "120.00", approval_above_usdc: "20.00", allowlist: ["0xMerchantA"], purpose: "API credits"}` → liability receipt issued.
3. `pay 10.00 to 0xMerchantA` → executed. `pay 60.00` → rejected (over cap). `pay 30.00` → human approval required.
4. `revoke` → frozen, timestamped.
