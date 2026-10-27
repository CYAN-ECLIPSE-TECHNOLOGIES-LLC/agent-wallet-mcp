# BENCH B1-TESTNET — Verity, W-003, independent bench

- **Cell:** USDC × Base Sepolia (`eip155:84532`), ERC-20 path + ETH gas row.
- **Bench plan:** `TESTNET-BENCH-PLAN.md` §4 (21 probes), §2 entry conditions, §5 pass criteria.
- **Build benched:** worktree `agent-wallet-mcp-b0`, branch `build/b0-asset-core-20261027`, HEAD `92861ce`; cell commits verified present: `9d494cc` registry, `4e5a66e` adapter, `b15c90a` fund signer, `05fe112` watcher, `03a5560` oracle.
- **Method:** bencher's own harness (own fixtures, wallets, amounts, RPC stub, desk signing, canonical-JSON/SHA-256 receipt recomputation, tx sender recovery via `Account.recover_transaction`) — builder's numbers never an input. Harnesses: `/tmp/verity_b1/harness.py` (99 checks), `/tmp/verity_b1/sandbox_e2e.py` (14 checks), `/tmp/verity_b1/live_refusals.py` (6 checks, live chain).
- **Date:** 2026-10-27.

## VERDICT: INCOMPLETE (testnet scope, this cell only)

Not PASS, not PASS-CONDITIONAL, not FAIL. Everything executable without on-chain funds **passed** (zero defects at FAIL severity; 3 Low findings + 1 Info, below). But §2.6 faucet funding — a pre-bench entry condition — is unmet, so the cell's core claim (§1: submits / confirms / accounts USDC on a live test chain, balances provable 3-way) is **untested on-chain**. §5 PASS requires every probe executed or NOT_RUN with a reason Verity accepts; FAUCET_UNFUNDED is a true statement of the environment, not an acceptable substitute for the broadcast half of the battery. The bench completes when funding lands and probes 1–8, 12–19 + live-chain halves of 10/16/17 run.

## Faucet outcome (recomputed by Verity, 2026-10-27)

Fund address `0xeE05b3965775e905f24a311BA1374ed96f953660` (envelope read-back: domain=fund, family=evm, chain `eip155:84532`, asset_scope = cell USDC+ETH; vault dir 0700, files 0600):

| Source | ETH (wei) | USDC (base) | nonce | chainId |
|---|---|---|---|---|
| sepolia.base.org | 0 | 0 | 0 | 0x14a34 |
| base-sepolia-rpc.publicnode.com (adapter-side) | 0 | 0 | 0 | 0x14a34 |
| base-sepolia.drpc.org (independent) | 0 | 0 | 0 | 0x14a34 |

3-way zero state: chain (3 providers) **0 == ledger 0 == audit 0** — ledger/audit sides vacuously zero: no testnet store file exists (`testnet-store.json` absent; nothing has ever written testnet state), zero testnet receipts exist. Targets ≥0.1 ETH / ≥200 USDC **not met**; faucet tx hashes: none exist. No mainnet asset spent/bridged/swapped; nothing bought. Live adapter read-only smoke via publicnode: health ok (head 50992994 at check time), both balances `absent/0`, finality policy CONFIRMED@1 / FINAL@6. Live two-source oracle from this VM: USDC/USD 1.00, ETH/USD 2346.41, sources `coinbase+coingecko`.

## Independent contract-address verification: VERIFIED

Registry value `0x036CbD53842c5426634e7929541eC2318f3dCF7e` agrees character-for-character with: (A) BaseScan Sepolia verified token page fetched by Verity — "USDC (USDC)", FiatTokenProxy, "Contract Source Code Verified (Exact Match)", Circle copyright in source; (B) Circle developer-documentation address as quoted/cited in independent search results for Base Sepolia USDC (Verity's search did not surface the Circle page directly — recorded honestly; builder's dual-source record in `assets.json` names the same two sources); (C) Verity's own on-chain `eth_call` via two independent RPCs: `name()=USDC`, `symbol()=USDC`, `decimals()=6` at that address. Registry row lifecycle correct: shipped `spend_enabled=false, listed=true, live=false, used=false`; testnet-config flip enables **exactly** the 2 cell rows in memory; mainnet rows never flipped (probed).

## Per-probe results (§4)

| # | Probe | Result | Evidence (Verity's) |
|---|---|---|---|
| 1 | Faucet fund + 3-way balances | **NOT_RUN — FAUCET_UNFUNDED** | 0/0 recomputed 3-provider (above); no faucet disbursement exists to reconcile |
| 2 | N≥5 payments, conservation | **NOT_RUN — FAUCET_UNFUNDED** | needs funded broadcasts |
| 3 | Store export / restart mid-battery | **NOT_RUN — FAUCET_UNFUNDED** | no testnet store exists; persistence proven only at sandbox scope (suite + probe 20) |
| 4 | Per-tx cap pre-broadcast | **NOT_RUN** (refusal half **PASS live**) | live chain, real fund envelope + adapter: cap+1 → `OVER_PER_TX_CAP` pre-broadcast; amount==cap binds on `INSUFFICIENT_BALANCE`, not cap (distinguishes the check); chain nonce/balance identical before/after on 2 independent endpoints. Execution half needs funds |
| 5 | Daily caps (per-asset + USD) | **NOT_RUN — FAUCET_UNFUNDED** | cumulative-crossing needs executed payments to accumulate spend |
| 6 | Allowlist + tighten mid-flight | **NOT_RUN** (refusal half **PASS live**) | non-allowlisted recipient → `NOT_ALLOWLISTED` pre-broadcast, live chain. Tighten-between-authorize-and-dispatch needs an authorizable (funded) intent |
| 7 | Approval threshold / TTL at testnet | **NOT_RUN — FAUCET_UNFUNDED** | queue→execute/deny/expire needs funded dispatch; TTL logic proven at sandbox scope only |
| 8 | Policy expiry / asset freeze at re-check | **NOT_RUN** (expiry refusal **PASS live**) | expired policy → `POLICY_EXPIRED` pre-broadcast, live chain. Between-authorize-and-dispatch variant + per-asset freeze need funded authorize |
| 9 | Malformed battery, testnet surface | **PASS** | own harness: float/bool/negative/zero/str/list/dict/bytes amounts, over-precision (`AMOUNT_OVER_PRECISION`), unknown asset, 7 bad-recipient forms — typed refusals at parse / evaluate / buildPayment / dispatch layers; **zero broadcasts** (`sent_raw=0` after whole battery). Server-transport malformed also typed (probe 20 script) |
| 10 | Desk-auth v2 (refusal side) | **PASS** | tampered amount / asset / idempotency → `DESK_AUTH_INVALID` pre-broadcast, desk nonce unconsumed, next valid payment executes on that nonce; replayed authorization → `DESK_AUTH_NONCE`, exactly 1 broadcast total (the original); nonce gap (3 first) → `DESK_AUTH_NONCE`, nonce 0 still usable; missing presentation → `DESK_AUTH_INVALID`, 0 broadcasts. Executed against chain-backed adapter + real fund-signer verification with my scripted RPC (broadcast layer stubbed — live-chain half joins the funded re-run) |
| 11 | Oracle kill / stale / diverge | **PASS** (finding F-2) | oracle layer, my fixtures: kill A or B → `ORACLE_UNAVAILABLE`; diverge → `ORACLE_DIVERGENCE`; stale → `PRICE_STALE`. Policy layer: one source killed → pegged degrade exactly per plan (`price_unverified`, forced approval above threshold, allow-below-unverified); both-stale / single old snapshot → C-1 policy-layer staleness fires (never verified, `oracle_ref=None`, forced approval above threshold); future-dated snapshot likewise; stale volatile (ETH) → hard deny `PRICE_STALE` |
| 12 | Freeze while idle | **NOT_RUN** (refusal **PASS live**) | frozen policy → `POLICY_FROZEN` pre-broadcast, live chain. Full revoke→subsequent-pays sequence at server/testnet scope needs funded setup |
| 13 | Freeze mid-flight | **NOT_RUN — FAUCET_UNFUNDED** | needs an in-flight broadcast |
| 14 | Nonce race (concurrent) | **NOT_RUN — FAUCET_UNFUNDED** | single-flight nonce ledger proven at scripted layer (probe 10 replay/gap); concurrent-process race on live chain untested |
| 15 | Freeze during approval-pending | **NOT_RUN — FAUCET_UNFUNDED** | needs approval-queued funded intent at live scope |
| 16 | Confirmation depth / exactly one FINAL | **PASS** (scripted-RPC layer; live-chain depth joins funded re-run) | chain-backed adapter + my RPC stub: dispatch at 1 conf → 1 provisional receipt, 0 FINAL; advance to 6 conf → watch → FINAL, **exactly 1 FINAL receipt**, spend recorded exactly once, re-watch emits no second FINAL; receipt hashes recomputed independently (my canonical-JSON SHA-256) ✓. Watcher: timeout → UNKNOWN (never FINAL/FAILED, 0 receipts); UNKNOWN→FINAL = 1 FINAL, 0 provisional; SUBMITTED→FINAL direct = 1 FINAL |
| 17 | Submission failure / RPC killed | **NOT_RUN** (partial: watcher timeout→UNKNOWN proven, probe 16) | mid-battery live submission failure + single-flight retry proof need broadcasts |
| 18 | Fee truth / fee cap | **NOT_RUN — FAUCET_UNFUNDED** (partial: signer `FEE_OVER_CAP` proven, S2) | fee-vs-real-receipt to the wei needs a broadcast tx |
| 19 | Gas insufficiency | **NOT_RUN** | needs USDC-funded / gas-drained wallet; current 0/0 state refuses `INSUFFICIENT_BALANCE` before the gas check, so `INSUFFICIENT_GAS_BALANCE` is unreachable in this state |
| 20 | Full suite + sandbox regression | **PASS** | suite under Anchor venv: **158 passed**; system python3: 158 ran, OK (11 signer-dep skips) — builder's claim verified exactly. My own stdio sandbox e2e (own client/amounts/ledger): 10-tool surface, fund 137.25 → pay 23.47 executed, balance 113.78, store schema v2 with exactly 1 receipt, receipt hash recomputed independently ✓, persisted balance exact in base units; transport malformed (float/negative/over-precision/junk) all typed errors |
| 21 | BTC reserve-only in testnet config | **PASS** (note F-3) | grant naming BTC → `ASSET_RESERVE_ONLY`; pay naming BTC → `ASSET_RESERVE_ONLY`, both in `load_testnet_registry()` configuration |

## Additional verifications (parent brief)

- **Mainnet refusal at construction:** chain-backed adapter refuses `eip155:8453`, `eip155:1`, `eip155:11155111` (not claimed), plus 137 / 42161 / 10 — all `MAINNET_REFUSED`; 84532 without RPC config → `RPC_NOT_CONFIGURED`; `FundSigner(expected_chain_id=8453)` → `MAINNET_REFUSED`; `create_fund_key(chain=1)` → `MAINNET_REFUSED`.
- **Fund-signer invariants (sign-probe level, my keys/artifacts):** valid ERC-20 signs, sender recovered independently == fund-key address; missing auth → `DESK_AUTH_MISSING`; missing signature field → `DESK_AUTH_MISSING`; tampered signature → `DESK_AUTH_INVALID`; ERC-20 `value != 0` → `ERC20_VALUE_REFUSED`; artifact amount != authorized → `AMOUNT_MISMATCH`; **calldata** amount != authorized → `AMOUNT_MISMATCH`; **calldata** recipient != authorized → `AMOUNT_MISMATCH`; fee over cap → `FEE_OVER_CAP`; asset outside envelope scope → `SCOPE_REFUSED`; native: value==amount signs, value!=amount → `AMOUNT_MISMATCH`, calldata on native → `NATIVE_DATA_REFUSED`.
- **Mandate status at dispatch: NOT WIRED at testnet scope → finding F-1.**

## Findings

| ID | Severity | Finding | Caught by |
|---|---|---|---|
| F-1 | Low (scope-explicit) | **No mandate-status check exists in this cell's dispatch path.** `server.py` / `state_machine.py` / `policy.py` / `fund_signer.py` / `families/*` contain no mandate input or status-list check; `Dispatcher.dispatch` and `FundSigner.sign_pay` accept no mandate parameter, so a revoked mandate can neither be presented nor refused here; receipts reserve `mandate_id` as null (`receipts.py` RESERVED_IDENTITY_FIELDS). Out of this cell's §1 claim and §4 battery (hence Low), but the Herald-side revocation enforcement lives in a different artifact and is **not** wired into this path — must be closed before any identity-bound claim for this cell or any live scope | parent-brief mandate probe (code audit + grep, dispatch-surface inspection) |
| F-2 | Low | **Policy layer collapses oracle failure codes for pegged assets.** Divergence / staleness / unavailability all degrade to identical `price_unverified` with verdict `code=None`, `oracle_ref=None` — probe 11's `ORACLE_DIVERGENCE` is observable only at the oracle layer, and a receipt cannot distinguish *why* the price was unverified. A below-threshold payment also proceeds unverified on actively-divergent feeds (divergence ≠ unavailability). Recommend: carry the oracle failure code into verdict/receipt; consider hard-deny on `ORACLE_DIVERGENCE` even for pegged assets | probe 11, policy-layer divergence case |
| F-3 | Low | **Recipient-side R/P-1 is vacuous in every shipped config:** `registry.reserve_addresses` is empty (all rows `reserve_addresses: []`), so the pay-to-reserve-address and grant-allowlist-reserve checks have no data to bite on. Asset-side R/P-1 fully enforced (probe 21). No registered reserve address existed for the recipient spot-check | probe 21 |
| F-4 | Info | Replay ordering: on a replayed authorization the dispatcher obtains a fresh fund-signer signature **before** the nonce-consume refusal (`DESK_AUTH_NONCE`). No broadcast occurs (broadcast is strictly after consume) and the nonce stays correct — outcome sound, ordering noted for the record | probe 10 replay case |
| — | Info (harness) | Verity's first harness run flagged `parse_amount("1.5")` as malformed — bencher error, not a defect: a decimal **string** is a valid boundary type (=1,500,000 base); the float **type** case refuses correctly. Corrected; harness 99/99 | — |
| — | Info (env) | Public Base Sepolia RPCs (publicnode, dRPC) intermittently drop connections from this VM; adapter's client retries (2) and read paths completed. Worth a retry-tolerant endpoint choice for the funded re-run window | live checks |

## Integrity

- Sandbox ledger sha256 **unchanged before/after**: `7b06c9b1ae2f34096f9d1b3f02db1cbcd5c10325cbd279fda5ac3ea23a1fdeab` — matches the B1 record value exactly. Team ledger unchanged (`446b2b21…c2`). No testnet store created. Worktree otherwise clean.
- Harness totals: 99/99 (core) + 14/14 (sandbox e2e) + 6/6 (live refusals). Zero broadcasts in every refusal battery; the only broadcasts in the entire bench went to Verity's scripted RPC stub.

## Remaining gates

**(a) Before the broadcast half of this bench can run:**
1. Fund `0xeE05b3965775e905f24a311BA1374ed96f953660` to ≥0.1 Base-Sepolia ETH + ≥200 Base-Sepolia USDC. Only routes: owner/browser claim at CDP Portal or Circle faucet signed in to a CDP/Circle account, or CDP API credentials for the SDK route (builder's 8-route record stands — Verity did not re-attempt browser faucets; subagent has no live browser). Record every faucet tx hash in the evidence pack; repeat daily claims may be needed for the USDC target.
2. Then re-run, in one funded window: probes 1–8, 12–15, 17–19 in full + live-chain halves of 10, 16 — with 3-way recomputation (chain via a second RPC ≠ adapter's, exported anchor-verified store, audit/receipt chain) per §3.
3. No code gate blocks the re-run: adapter / signer / watcher / oracle all proven at the scripted layer; F-1/F-2/F-3 are conditions to disposition, not blockers for testnet completion.

**(b) Before any second cell (Solana devnet / Ethereum Sepolia) may be attempted:**
1. This cell reaches PASS (or PASS-CONDITIONAL with conditions Verity records) at testnet scope — per §5, conditions attach to the *next* cell's entry; an INCOMPLETE first cell exports nothing.
2. Per-cell build first: dual-source registry population for that cell; adapter guard extension (this build refuses `eip155:11155111` at construction by design; no Solana family adapter exists); that cell's fund-signer domain + faucet funding; its own bench plan/battery.
3. F-1 (mandate wiring) and F-2 (oracle-code surfacing) dispositioned before the pattern is replicated to a second cell.
4. Unchanged by any testnet result (§6): live activation still separately requires live-scope Verity PASS, stablecoin-settlement counsel gate open, KMS/HSM condition for float size, recorded owner live-activation decision.

— Verity, W-003 · Director of Quality & Certification · 2026-10-27
