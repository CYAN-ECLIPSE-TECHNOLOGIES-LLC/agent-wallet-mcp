# BENCH B1-TESTNET — Verity, W-003, independent bench

- **Cell:** USDC × Base Sepolia (`eip155:84532`), ERC-20 path + ETH gas row.
- **Bench plan:** `TESTNET-BENCH-PLAN.md` §4 (21 probes), §2 entry conditions, §5 pass criteria.
- **Build benched:** worktree `agent-wallet-mcp-b0`, branch `build/b0-asset-core-20261027`, HEAD `92861ce`; cell commits verified present: `9d494cc` registry, `4e5a66e` adapter, `b15c90a` fund signer, `05fe112` watcher, `03a5560` oracle.
- **Method:** bencher's own harness (own fixtures, wallets, amounts, RPC stub, desk signing, canonical-JSON/SHA-256 receipt recomputation, tx sender recovery via `Account.recover_transaction`) — builder's numbers never an input. Harnesses: `/tmp/verity_b1/harness.py` (99 checks), `/tmp/verity_b1/sandbox_e2e.py` (14 checks), `/tmp/verity_b1/live_refusals.py` (6 checks, live chain).
- **Date:** 2026-10-27.

## VERDICT: INCOMPLETE (testnet scope, this cell only) — SUPERSEDED 2026-10-27 by the broadcast re-run below: **PASS-CONDITIONAL (testnet)**

Not PASS, not PASS-CONDITIONAL, not FAIL. Everything executable without on-chain funds **passed** (zero defects at FAIL severity; 3 Low findings + 1 Info, below). But §2.6 faucet funding — a pre-bench entry condition — is unmet, so the cell's core claim (§1: submits / confirms / accounts USDC on a live test chain, balances provable 3-way) is **untested on-chain**. §5 PASS requires every probe executed or NOT_RUN with a reason Verity accepts; FAUCET_UNFUNDED is a true statement of the environment, not an acceptable substitute for the broadcast half of the battery. The bench completes when funding lands and probes 1–8, 12–19 + live-chain halves of 10/16/17 run.

---

# BROADCAST BATTERY RE-RUN — 2026-10-27 (funded; supersedes the verdict above)

- **Build benched:** same worktree/branch, HEAD `c256fe4` (includes the F-1/F-2 closures `e4fdcef`/`d855057` + delta bench; suite **179 passed + 11 subtests**, re-verified by Verity pre- AND post-battery, identical).
- **Funding (verified by Verity, dual-source + block-level, never taken on claim):** fund address held exactly **30.0 USDC** (30,000,000 base; inbound scan = 11 transfers: 1× 20 USDC Circle faucet, block 50994895, tx `0x3c6769be…6d3`; 10× 1 USDC CDP faucet) and **0.0001 ETH** (100,000,000,000,000 wei; chain nonce 0) — the parent's claim list confirmed exactly on three independent RPCs. The plan's ≥0.1 ETH / ≥200 USDC targets were **not** met; the battery ran on the real float with adaptations logged below. Gas honesty: the real float sufficed for the entire battery (see gas accounting) — no gas stop occurred, nothing was bought/bridged, no mainnet anything was touched.
- **Method:** bencher's own driver (`/tmp/verity_b1t/driver.py` + phase scripts), own bench desk key + recipient, independent check RPC (sepolia.base.org) ≠ adapter RPC (publicnode), true-fee recomputation from raw receipts on the second RPC (OP-Stack total = L2 `gasUsed × effectiveGasPrice` **+ `l1Fee`**), receipt hashes recomputed independently. **155 checks logged** (`battery_log.jsonl`): 137 pass / 18 fail — every fail adjudicated below; 9 of the 18 are harness-model knock-ons of finding F-T2 and reconcile to the wei against chain truth.

## Re-run per-probe results (broadcast half; bench-1 results stand where noted)

| # | Probe | Result | Evidence |
|---|---|---|---|
| 1 | Faucet fund + 3-way balances | **PASS** | Opening verified 3-RPC unanimous (above); pre-tx block 51008809 balance re-read == 100,000,000,000,000 exactly |
| 2 | N≥5 payments, conservation | **PASS** (F-T2 cross-ref) | 18 dispatcher payments, all FINAL on chain; USDC conservation: 30,000,000 out in payments, 30,000,000 returned by recipient, fund ends whole; **ETH conservation closes to the wei**: opening 100,000,000,000,000 == final balances (fund 418,102,594,534 + recipient 4,802,968,972,677 + sink 61,789,109,102,454) + total fees 32,989,819,330,335 |
| 3 | Store export / restart mid-battery | **PASS** (dispatch-layer scope; server-store restart remains sandbox-scope per bench-1) | Export → fresh process: sha-256 anchor verifies, policy reconstructs byte-identical (hash equal), persisted spend + desk-nonce stream survive, post-restart payment executes on the continued stream with caps continuing from persisted totals (not reset); exercised across two independent restarts |
| 4 | Per-tx cap (execution half) | **PASS** | amount == cap (3,000,000) executed to FINAL; refusal half stood (bench-1) |
| 5 | Daily caps | **FAIL** — per-asset leg PASS, USD-aggregate leg FAIL | Per-asset: under-cap executes; crossing → `OVER_DAILY_CAP` pre-broadcast ✓. Aggregate: a 2,000,000 payment that crossed the aggregate cap (7,500,000 + 2,000,000 > 9,000,000) was **allowed and executed** — root causes F-T1 (cap inert under live oracle staleness) + F-T2 (its submission then reported refused). Reproduced deterministically offline (see F-T1) |
| 6 | Allowlist tighten mid-flight | **PASS** | authorize → tighten → dispatch `NOT_ALLOWLISTED` (no broadcast, nonce intact) → restore → same intent executes to FINAL |
| 7 | Approval threshold / TTL | **PASS** | Above-threshold queues (`approval_required`); grant → executes; unapproved dispatch executes nothing; TTL expiry → `APPROVAL_EXPIRED` pre-broadcast. One transient: the first intent's authorize returned a refusal (code not captured) while the identical second intent queued correctly seconds later — environmental (consistent with a PRICE_STALE verdict at that instant); the full path is proven by the remaining legs |
| 8 | Expiry / freeze at re-check (execution halves) | **PASS** | Per-asset freeze between authorize and dispatch → `POLICY_FROZEN_ASSET`; grant expiring mid-window → `POLICY_EXPIRED` at dispatch; both execute after unfreeze/restore where applicable |
| 9 | Malformed battery | **PASS** (bench-1; testnet-surface spot re-verified live: bad recipient → `ADDRESS_INVALID_FOR_FAMILY`, no broadcast) | |
| 10 | Desk-auth v2 (live-chain half) | **PASS** | Replay of a consumed authorization → `DESK_AUTH_NONCE` pre-broadcast; amount-tampered authorization → `DESK_AUTH_INVALID` at the signer pre-broadcast; desk nonce unconsumed in both |
| 11 | Oracle kill / stale / diverge | **PASS** stands (bench-1). Live addendum: the PRICE_STALE degrade path fired repeatedly in production conditions during this battery — that behavior is finding F-T1's mechanism | |
| 12 | Freeze while idle (execution half) | **PASS** | `POLICY_FROZEN` refusal pre-broadcast; same intent executes after unfreeze |
| 13 | Freeze mid-flight | **PASS** | Bystander intent refused `POLICY_FROZEN` after the in-flight submission; in-flight payment settled FINAL under freeze (not recalled), exactly 1 FINAL receipt, spend recorded exactly once. Proven twice: phase-2b run (settled on chain; its product receipts were lost with a crashed harness process — recorded honestly) and a full re-run with complete receipt evidence |
| 14 | Nonce race | **PASS** | Two threads, same desk nonce, barrier-synchronized: exactly one executes (chain advanced by exactly one payment), loser refused `DESK_AUTH_NONCE` |
| 15 | Freeze during approval-pending | **PASS** | Approval grant under global freeze → `POLICY_FROZEN`, zero broadcasts |
| 16 | Confirmation depth (live-chain half) | **PASS** | On every receipt-bearing payment: provisional at CONFIRMED, exactly 1 FINAL at 6 confs, spend recorded once at FINAL; receipt hashes recomputed independently on all of them |
| 17 | Submission failure / RPC killed | **FAIL** (retry leg; not-landed leg PASS) | Proxy dropped only `eth_sendRawTransaction`: dispatch → `CHAIN_UNHEALTHY` refusal, intent QUEUED, tx proven ABSENT on chain (independent RPC), zero state change ✓. The re-authorized retry **landed on chain but was reported refused** (its own submit response was lost) — finding F-T2, occurrence #2 |
| 18 | Fee truth / fee cap | **FAIL** (fee-truth leg; fee-cap leg PASS) | Fee cap: `max_fee_base=1` → `FEE_OVER_CAP` pre-broadcast ✓. Fee truth: the receipt's `fee` is the pre-broadcast **quote** (780,000,000,000 on every sample) and never the actual fee — actuals (L2 + `l1Fee`) across samples: 2,401,947,317,938 · 1,568,311,758,888 · 1,487,306,091,148 · … — the quote also structurally omits the OP-Stack L1 data fee, the dominant component on this chain. Finding F-T3 |
| 19 | Gas insufficiency | **PASS** | Fund drained below one fee quote (418,102,594,534 < 780,000,000,000) via two verified drains; payment dispatch → `INSUFFICIENT_GAS_BALANCE` pre-broadcast, zero broadcasts, USDC untouched |
| 20 | Suite + sandbox regression | **PASS** | 179 passed + 11 subtests pre- and post-battery; sandbox ledger sha256 unchanged end-to-end (`7b06c9b1…`) |
| 21 | BTC reserve-only (testnet config) | **PASS** | Re-verified live in this battery: grant naming BTC → `ASSET_RESERVE_ONLY` |

## New findings (broadcast battery)

| ID | Severity | Finding |
|---|---|---|
| F-T1 | **Medium-High** (live-scope blocker) | **The USD aggregate daily cap does not bind in live operation for pegged assets.** Mechanism, reproduced offline deterministically: the live two-source oracle raises `PRICE_STALE` whenever either source's reported timestamp is >300 s old (CoinGecko's USDC/USD `last_updated_at` routinely is, because the pair barely moves); `evaluate_payment` then takes the designed pegged degrade-allow path — aggregate check skipped (`usd_value=None`), forced approval only above the per-asset approval threshold. A 2,000,000 aggregate-crossing payment at price 1.00 therefore allowed + executed. The verdict/receipt honestly record `price_unverified` + `oracle_failure_code=PRICE_STALE` (F-2 closure working as designed), so the gap is *visible* — but the anti-structuring control is inert in the common case. Disposition is the owner's policy call (staleness semantics vs source-timestamp choice vs hard-deny), exactly the class F-2 deferred |
| F-T2 | **High** (live-scope blocker) | **Ambiguous submit is reported as a refusal while the payment lands.** When the `eth_sendRawTransaction` response is lost after the tx reaches the chain, `Dispatcher.dispatch` returns `refused / CHAIN_UNHEALTHY`, leaves the intent QUEUED, records **no spend**, and issues **no receipt** — although the artifact's tx hash is known and the watcher stack can resolve it. Fired **3 times in 21 broadcasts** on this VM's public-RPC path (probe 5c, probe 17 retry, series payment 2): each time exactly one transfer landed (no double-pay occurred), but books diverged from chain by the full amount each time, caps were silently bypassed, and an operator retrying on the product's own refusal signal would double-pay. The dispatch path must reconcile (tx-by-hash / status) before declaring a submit refused |
| F-T3 | **Medium** | **Receipt fee is the quote, not the fee.** No code path writes actual gas into any receipt; on this OP-Stack chain the recorded 780,000,000,000 quote understates the true debit by up to ~3.1× (and the quote model omits `l1Fee` entirely). The `FEE_OVER_CAP` gate likewise bounds only the L2-quote leg. Receipts must record actual gas (L2 + L1) once FINAL, or the field must be renamed/re-scoped as a quote |

## Adaptations + gas accounting (all logged in the battery log)

- Probe amounts adapted only where the claim is amount-independent: battery payments ran 1 → 5,000,000 base units against the 30 USDC float; the series' final payment took the full remaining balance (11,126,542) and the recipient returned the entire float afterward (fund ends at exactly 30,000,000).
- Probe 4's grant set `approval_above == per-tx cap` so the cap-exact payment tests the cap, not the approval queue; probes 7/15 used dedicated approval-threshold grants (same pattern as the plan's own fixtures).
- Probe 19's sub-quote state was constructed by iterative drains (2) because the true OP-Stack fee is volatile (observed per-tx true fees 441,897,405,466 → 3,011,645,463,363 wei, driven by the L1 blob fee market); final fund ETH 418,102,594,534 wei remains, by design.
- Gas totals: 21 fund broadcasts (18 payments + 1 native gas grant + 2 drains) + 1 recipient return; total fees 32,989,819,330,335 wei ≈ 0.0330 ETH against the 0.0001 ETH float — **the float covered the whole battery with room**; per-tx true fees were far below the plan's ≥0.1 ETH planning figure because the L2 gas price sat at 0.006 gwei.
- The battery required four harness restarts (two public-RPC transport drops killed harness processes; one ambiguous harness-side send; one import fix). Every restart re-seeded from chain truth (dual-RPC), never from harness memory; all chain state in this record was recomputed independently of the harness's own model.

## Updated verdict + remaining gaps

**PASS-CONDITIONAL (testnet scope, this cell only).** §5's conditions: F-T1, F-T2, F-T3 must be fixed and delta-benched — or explicitly re-scoped by the owner — before this cell's pattern is relied on at any wider scope; F-T2 is the gating item (books-vs-chain divergence on a routine network event). Everything else in the 21-probe battery is now executed and passing on the live test chain: per-asset caps, allowlist, freezes (idle / mid-flight / approval-pending), expiry, approvals + TTL, nonce discipline incl. the concurrent race, desk-auth replay/tamper, persistence across restarts, finality + receipt integrity, the gas gate, the fee-cap gate, and the reserve fence. **Remaining gaps:** (i) probe 3's server-store restart property remains proven at sandbox scope only (the dispatch-layer persistence property is what this battery proves); (ii) the mandate-enforcement cell (F-1 closure) was benched separately at local/sandbox scope — this battery ran with `require_mandate=False` (the shipped default), so mandate-at-dispatch on the live test chain is still unproven; (iii) §6 live-activation gates are unchanged (live-scope PASS, counsel gate, KMS/HSM condition, recorded owner activation). No second cell may be attempted until F-T2 is dispositioned (bench-1 gate (b) applies with F-T2 at its head).

— Verity, W-003 · Director of Quality & Certification · re-run 2026-10-27

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
