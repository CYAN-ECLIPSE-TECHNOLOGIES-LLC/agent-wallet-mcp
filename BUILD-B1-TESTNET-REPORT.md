# BUILD B1-TESTNET — Agent Wallet first cell: USDC on Base Sepolia

- **Scope: TESTNET ONLY (eip155:84532).** No mainnet keys, no real funds,
  nothing bought, no mainnet asset spent/bridged/swapped. Mainnet chain
  ids are refused at adapter construction (tested).
- **Branch:** `build/b0-asset-core-20261027` · **Worktree:**
  `~/workspace/cyan-eclipse/agent-wallet-mcp-b0`
- **Plan:** `TESTNET-BENCH-PLAN.md` §2, tasks 1–6.

## Per-task results

| # | Task | Commit | Result |
|---|---|---|---|
| 1 | Registry population (dual-source) | `9d494cc` | DONE — see below |
| 2 | EVM submission path | `4e5a66e` | DONE |
| 3 | Fund signer (new key domain) | `b15c90a` | DONE — real testnet fund envelope created (task 6 step) |
| 4 | Watcher / finality | `05fe112` | DONE |
| 5 | Real two-source oracle | `03a5560` | DONE |
| 6 | Faucet funding | this report | **BLOCKED — 0 obtained; every route recorded below** |

## Task 1 — registry rows + dual-source record

- USDC row: `eip155:84532/erc20:0x036CbD53842c5426634e7929541eC2318f3dCF7e`,
  contract **0x036CbD53842c5426634e7929541eC2318f3dCF7e**.
  Sources, agreeing exactly:
  (A) Circle's official developer documentation (developers.circle.com,
  USDC contract addresses — Base Sepolia USDC at that address);
  (B) BaseScan Sepolia verified token page
  (sepolia.basescan.org/token/0x036cbd53842c5426634e7929541ec2318f3dcf7e —
  "USDC (USDC)", FiatTokenProxy, "Contract Source Code Verified
  (Exact Match)", Circle copyright in source).
  Corroborated on-chain via Base Sepolia RPC `eth_call`:
  name = USDC, symbol = USDC, decimals = 6.
  Recorded verbatim in the row's `address_source`.
- ETH row: `eip155:84532/slip44:60`, gas role, native, 18 decimals.
- Lifecycle on both rows: `listed=true, live=false, used=false`,
  `spend_enabled=false` in shipped `assets.json`.
  `spend_enabled` flips ONLY in `testnet_config.load_testnet_registry()`
  (in-memory, testnet chains opt-in in the registry validator; mainnet
  rows can never be enabled through it — tested).
- Registry row count 14 → 16 (one stale count assertion updated).

## Task 2 — EVM submission path

- `EvmAdapter(chain_backed=True)`: ONLY `eip155:84532`; any other chain
  id (8453, 1, 11155111 tested) raises `AdapterError(MAINNET_REFUSED)`
  at construction. RPC endpoint from constructor arg or
  `AGENT_WALLET_BASE_SEPOLIA_RPC` env — never hard-coded; missing
  endpoint raises `RPC_NOT_CONFIGURED`.
- `JsonRpcClient` over stdlib urllib (injectable transport for tests);
  `submit()` broadcasts only fund-signer-signed artifacts and checks the
  returned hash equals the signed artifact's hash; `getStatus()` reads
  receipts/block height into SUBMITTED / CONFIRMED / FINAL / UNKNOWN /
  FAILED_TERMINAL; chain `readBalance` (ERC-20 `balanceOf` / native),
  live `quoteFee`, `health()` (chain-id checked).
- Live read-only smoke against `https://base-sepolia-rpc.publicnode.com`:
  health ok (head 50990773), balances read, fee quote returned.

## Task 3 — fund signer

- `fund_signer.py`: sign-only EIP-1559 signer, envelope/1 custody
  (Anchor/desk pattern), domain `fund`, family `evm`, one chain.
  Invariants enforced signer-side (all tested): desk-auth v2 must be
  presented by the dispatcher and verify against the desk public key
  (`DESK_AUTH_MISSING` / `DESK_AUTH_INVALID`); envelope chain/family
  match artifact; ERC-20 pays refuse `value != 0`; calldata amount and
  recipient == authorized amount/recipient; artifact amount ==
  v2-authorized amount; `gas * maxFeePerGas <= fee quote cap`
  (`FEE_OVER_CAP`); asset must be in envelope `asset_scope`.
- Dispatcher presents the v2 authorization to the signer BEFORE the
  desk nonce is consumed; signer refusal = check error → intent back
  to QUEUED, nonce unburned, zero broadcasts (tested end-to-end).
- **Fund vault (real, testnet):** `~/.secrets/eclipse-agent-wallet-funds/`
  (dir 0700, own `master.key` 0600, envelope 0600) — separate from the
  desk vault, which was not touched.
  Fund address (Base Sepolia): **0xeE05b3965775e905f24a311BA1374ed96f953660**
  Envelope: `fund-key-evm-84532.json`, asset_scope = the USDC + ETH
  testnet asset ids.

## Task 4 — watcher / finality

- Finality policy (chain-backed Base Sepolia): CONFIRMED at
  **1 confirmation**, FINAL at **6 confirmations**.
- `watcher.FinalityWatcher` + `Dispatcher.watch_finality`: provisional
  receipt at CONFIRMED; exactly ONE FINAL receipt, emitted by the
  transition reaching FINAL; spend recorded in the tracker exactly
  once, at FINAL; timeout → intent to UNKNOWN (never silently FINAL,
  never FAILED); UNKNOWN can re-resolve to CONFIRMED/FINAL.
  All proven against fake-RPC + scripted adapters, incl. the
  dispatcher end-to-end (CONFIRMED in dispatch → FINAL in watcher,
  receipts = 1 provisional + 1 final).

## Task 5 — oracle

- `oracle_sources.py`: Coinbase spot API (source A) + CoinGecko
  simple/price (source B) — independent, credential-free, Decimal
  end-to-end — feeding `TwoSourceOracle` for USDC/USD + ETH/USD on the
  testnet asset ids. Divergence and source failure refuse typed
  (`ORACLE_DIVERGENCE` / `ORACLE_UNAVAILABLE`, tested with canned
  transports; both live endpoints also probed reachable from this VM).
- C-1 policy-layer staleness stays ON: TwoSource max staleness 300 s
  and `policy._fresh_or_raise` default 300 s unchanged.

## Task 6 — faucet funding: BLOCKED (0 obtained)

Fund address balances, verified on two independent RPC providers
(publicnode + dRPC), before and after all attempts:
**ETH = 0 wei; USDC = 0 base units.** Targets (≥0.1 ETH, ≥200 USDC)
NOT met. **Faucet tx hashes: none — no faucet disbursed anything.**
No mainnet asset was spent, bridged, or swapped; nothing was bought.

Routes attempted / evaluated, with verbatim outcomes:

1. **Circle USDC faucet** (faucet.circle.com — the plan's candidate for
   USDC): page fetch via the available text-fetch tool failed at the
   runtime layer ("This caller stopped waiting for approval and was
   not released") — outcome unknown, not retried per instruction. The
   faucet is an interactive web form (network/address/token + claim);
   no credential-free API route is published.
2. **Coinbase Developer Platform faucet** (Base docs' primary testnet
   route; "up to 0.1 ETH per 24 hours, plus USDC, EURC, and cbBTC.
   Also available programmatically through the CDP SDK"):
   unauthenticated claim POST to
   `api.developer.coinbase.com/rpc/v1/base-sepolia/faucet/claim`
   returned, verbatim, `404 page not found` (both `eth` and `usdc`).
   The documented programmatic route requires, verbatim from CDP docs
   prerequisites: "Created a CDP API key and generated a Wallet
   Secret" on a signed-in CDP account. No CDP account/credentials
   exist in this environment; creating one is an owner/browser action.
3. **Alchemy Base Sepolia faucet** (listed in Base docs; 0.1 ETH/24 h):
   its own FAQ eligibility, verbatim: "You need at least 0.001 ETH on
   Ethereum Mainnet for EVM-based testnets" and "You need sufficient
   activity on Ethereum Mainnet ... to qualify." The fresh testnet
   fund address has neither, and acquiring mainnet ETH for it would
   violate this task's no-mainnet rule. BLOCKED by eligibility rule.
4. **QuickNode Base Sepolia faucet** (listed in Base docs): FAQ,
   verbatim: "We do require a small mainnet balance on the wallet you
   are using to claim." Same blocker as (3).
5. **Chainstack faucet** (listed in Base docs): requires a Chainstack
   API key (account) — none exists here.
6. **thirdweb faucet** (listed in Base docs): interactive wallet-
   connect claim; no credential-free API route available to this
   agent (no live browser in this subagent environment).
7. **ethfaucet.com** (listed in Base docs): no Base Sepolia route
   surfaced at verification time.
8. Alternates beyond the Base docs list, checked and rejected:
   Bware Labs Base Sepolia faucet — page states, verbatim: "Base
   Sepolia Faucet is undergoing maintenance, please try again later."
   and "All tokens allocated for today have been claimed. Please come
   back tomorrow!"; L2Faucet — "Faucet under maintenance … Token
   requests are currently paused."

What unblocks funding (owner/browser actions, minutes each):
claim from the CDP Portal faucet or Circle faucet in a real browser
signed in to a CDP/Circle account, to the fund address above; or
provide CDP API credentials for the SDK route. Funding may need
multiple daily claims (CDP 0.1 ETH/day covers the ETH target in one
claim if USDC is also claimed there; Circle faucet drips are smaller,
so the 200 USDC target may take repeat claims).

## Suites

- Full local suite: **158 tests, all green** under the Anchor venv
  python (130 pre-existing + 28 new: 5 registry, 10 fund-signer/guard,
  9 testnet adapter/oracle/dispatcher, 4 watcher). Under system
  python3: 158 ran, OK, signer-dependency tests skip (same convention
  as the pre-existing DeskSigner test — eth_account/cryptography live
  in the venv).
- Sandbox ledger untouched (sha256 still
  `7b06c9b1ae2f34096f9d1b3f02db1cbcd5c10325cbd279fda5ac3ea23a1fdeab`,
  the B1-integration post-migration hash); team-ledger untouched; no
  testnet store file was created — testnet state has its own
  configured store path (`testnet_config.testnet_store_path()`) and
  nothing has written to it yet.

## Bench readiness

The built adapter + fund signer + watcher + oracle are READY for
Verity's §4 battery in every respect that does not require a funded
wallet. The battery itself (notably §4.1 3-way balances and all
broadcast probes) CANNOT start until the fund address holds
≥0.1 Base-Sepolia ETH and ≥200 Base-Sepolia USDC — faucet funding is
the single open entry condition, blocked as recorded above.

— Silas Forge, W-002 · Director of Product Engineering · 2026-10-27
