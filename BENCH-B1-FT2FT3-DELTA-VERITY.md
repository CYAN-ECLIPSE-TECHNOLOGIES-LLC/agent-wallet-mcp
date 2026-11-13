# BENCH B1 — F-T2 + F-T3 DELTA — Verity, W-003, independent bench

- **Scope:** delta bench of the F-T2 (ambiguous submit) and F-T3 (fee truth)
  closures from `BENCH-B1-TESTNET-VERITY.md` (broadcast battery re-run,
  PASS-CONDITIONAL (testnet)); F-T1 explicitly NOT a fix target — verified
  unchanged (owner policy decision).
- **Build benched:** worktree `agent-wallet-mcp-b0`, branch
  `build/b0-asset-core-20261027`, HEAD `44b2349`; fix commits verified
  present and on top of the battery commit `98b9836`: `9c97477` (F-T3),
  `44b2349` (F-T2).
- **Method:** bencher's own harness (`/tmp/verity_ft2ft3/`: `chain.py`,
  `bench.py`, `sandbox_probe.py`, `bench_log.json`) — own chain model
  behind the product's public `JsonRpcClient` transport injection, own
  desk key, own fixtures and economics (gas price 2,000,000 wei, actual
  gasUsed 46,123, L1 fee a byte-length FUNCTION computed by the bench
  chain for both estimate and actual — the builder's model used unrelated
  flat constants). Transfers actually move balances on the bench chain;
  raw txs are RLP-decoded and sender-recovered by the bench
  (eth_account/eth_utils, keccak cross-checked against known vectors);
  receipt hashes recomputed with the bench's own canonical-JSON SHA-256.
  No builder test fixtures (`tests/*`) were used. **46/46 bench checks
  passed.** Full suite re-run independently: **191 passed + 11 subtests**
  (venv-anchor, pytest) and **191 ran, OK (22 signer-dep skips)** under
  system python3 — the builder's claimed numbers verified exactly, no
  regressions from the 179 + 11 baseline.

## VERDICT: F-T2 CLOSED · F-T3 CLOSED — at testnet scope (this cell only)

Both conditions on the broadcast battery's PASS-CONDITIONAL are closed by
the fixes as built. The cell verdict therefore stands **PASS-CONDITIONAL
(testnet)** with **F-T1 as the sole remaining condition** — and F-T1 is an
owner policy disposition, not a code defect (see §3). §6 live-activation
gates are unchanged and unaffected by this delta bench. Per bench-1 gate
(b), F-T2's closure removes the gating item at the head of second-cell
entry; the remaining gate-(b) conditions (this cell's standing, per-cell
build, F-1/F-2 dispositions — both already closed and delta-benched) are
as recorded in `BENCH-B1-TESTNET-VERITY.md`.

## 1. F-T2 — ambiguous submit reconciles before any refusal: CLOSED

| # | Question | Result | Bench evidence |
|---|---|---|---|
| 1 | Lost response AFTER landing → adopted | **PASS** | Drop-after-landing transport: dispatch returns `executed`, never refused (FT2-1a.1/2). Exactly **1** broadcast reached the bench chain (FT2-1a.3); the bench RLP-decodes it to the authorized payment — sender recovered == fund address, ERC-20 recipient/amount exact (FT2-1a.4). **Books == chain:** bench-chain token balances moved exactly the payment amount (FT2-1a.5); spend recorded exactly once, at FINAL (FT2-1a.6/7); exactly **1** FINAL receipt, whose hash and tx hash the bench recomputed independently (FT2-1a.8). Already-FINAL-at-reconcile variant: dispatch returns with exactly one receipt total, the FINAL (FT2-1b). Mempool variant (tx visible, no receipt): adopted via tx lookup, reported `submitted`, completed by the watcher with 1 FINAL receipt / 1 spend (FT2-M.1/2) |
| 2 | Lost response WITHOUT landing → refusal only on proven non-inclusion, evidence attached | **PASS** | Refusal `CHAIN_UNHEALTHY` stands only after the full window with hash unknown AND sender mined nonce unconsumed (FT2-2.1/2.2: 7 polls, `transport_errors=0`, `last_sender_nonce=0`). The evidence binds the real artifact: its `tx_hash` == the bench's own keccak of the dropped raw tx, `chain_nonce` == the bench-decoded nonce, `from_address` == the bench-recovered sender (FT2-2.3). Zero chain effects, zero receipts, zero spend; intent returns to QUEUED (FT2-2.4); desk nonce consumed exactly once by the refused attempt — the retry contract (FT2-2.5) |
| 3 | Adversarial: nonce consumed by a DIFFERENT tx during the window | **PASS** | Foreign tx takes the sender's slot while our response is lost: our tx is **not adopted** (no receipts, no spend, never CONFIRMED/FINAL) and **not declared proven-absent** (no refusal issued; evidence `conclusion=inconclusive`, `last_sender_nonce=1`) (FT2-3.1/3.2). Chain truth checked directly: only the foreign tx occupies the slot; our hash exists nowhere on the bench chain (FT2-3.3) |
| 4 | Inconclusive → parked UNKNOWN, re-dispatch impossible | **PASS** | Intent parks `UNKNOWN` on the recorded hash (== bench-computed hash) (FT2-4.1); re-dispatch raises and produces no second broadcast attempt (FT2-4.2). Resolution route is `watch_finality` only |
| 5 | Retry discipline | **PASS** | Same intent after proven absence: executes with exactly **one** fresh broadcast across both attempts and the desk nonce still consumed exactly **once** in total (FT2-5a.1–3). Twin intent reusing the (wallet, idempotency key) after the original FINAL: `DUPLICATE_SUBMISSION` with landed evidence, **zero** broadcasts, twin's desk nonce never consumed (FT2-5b.1/2). Twin arriving while the original is inconclusive: `DUPLICATE_SUBMISSION` (cannot be proven absent), zero further broadcasts (FT2-5c) |
| 6 | Transport errors during reconciliation never read as absence | **PASS** | Every reconciliation read down for the whole window: outcome is inconclusive/parked, NOT a proven-absent refusal; evidence counts 14 transport errors and records `last_sender_nonce=None` (FT2-6a). Flaky-then-healed reads (6 failed calls, then healthy): refusal stands only once a real nonce read proves non-inclusion, and the evidence carries `transport_errors=6` alongside `conclusion=absent` (FT2-6b) |

## 2. F-T3 — fee truth: CLOSED

| # | Question | Result | Bench evidence |
|---|---|---|---|
| 1 | Quote = L2 + L1 estimate, components exposed; fail-closed | **PASS** | Quote L2 == bench formula (65,000 × bench gas price × 2 = 260,000,000,000); L1 == the bench chain's GasPriceOracle answer, recomputed by the bench from the recorded payload length via its own fee function (payload 179 B → 400,001,253); total == L2 + L1 with both components exposed on the quote (FT3-1a.1–3). Oracle dead: `quoteFee` raises `FEE_ESTIMATE_UNAVAILABLE` — no L2-only quote is ever issued — and dispatch fails closed with the same code, **zero broadcasts, zero receipts, zero spend** (FT3-1b.1/2; surface note: dispatch propagates the typed error rather than returning a refusal result — fail-closed either way) |
| 2 | Cap between L2-only and estimated total refuses at BOTH layers | **PASS** | Policy layer: grant `max_fee_base = total − 1` (above the L2-only figure) → `FEE_OVER_CAP` pre-broadcast, zero broadcasts (FT3-2a). Signer layer, driven directly: `sign_pay` refuses at `total − 1` (`FEE_OVER_CAP`), signs at exactly the total (signed hash == bench keccak, sender recovered == fund address), and refuses an artifact stripped of its L1 estimate even under a huge cap — fail-closed (FT3-2b.2). The finalized artifact's bound L1 estimate equals the bench chain's oracle answer for that exact artifact (FT3-2b.1) |
| 3 | FINAL receipts carry quoted + actual (L1/L2 split) read from chain data; failed fee read never fails a proven payment | **PASS** | FINAL receipt: `quoted_amount_base` == the measured quote total; `actual_l2_amount_base` == 46,123 × 2,000,000 (bench chain's gasUsed × price), `actual_l1_fee_base` == the bench fee function over the **actual broadcast raw tx**, `actual_amount_base` == their sum; the sender's on-chain ETH fell by exactly the actual total; receipt hash recomputed independently (FT3-3a.1/2). Transient fee-read failure engineered at exactly the receipt-build read: the payment still finalizes, the receipt carries the quote only (no fabricated actuals), hash verifies (FT3-3b). `adapter.actual_fee` on an unknown tx returns None — no exception escapes (FT3-3c) |
| 4 | Sandbox (non-chain) receipt shape byte-unchanged | **PASS** | The same fixed sandbox dispatch was executed under the pre-fix tree (`98b9836`, via a temporary git worktree) and the post-fix tree: the resulting receipts are **byte-identical** once `created_at`/`receipt_hash` are stripped, and the fee object carries exactly the four pre-fix keys (`amount_base`, `fee_asset_id`, `quoted_at`, `expires_at`) — no `actual_*` keys leak into non-chain receipts |

## 3. F-T1 — verified UNCHANGED (owner policy item, not a fix target)

- **Control:** with fresh two-source prices, a $6.00 USDC payment against
  a $5.00 aggregate daily cap is refused `OVER_USD_DAILY_CAP`,
  pre-broadcast — the aggregate cap itself binds whenever the price is
  verified (FT1-C).
- **Experiment:** with one oracle source's snapshot 3,600 s old
  (`PRICE_STALE`), the identical payment **degrade-allows and executes**,
  exactly as the F-T1 finding describes (FT1-1). Its FINAL receipt stays
  honest — `price_unverified=true`, `oracle_failure_code=PRICE_STALE`,
  `usd_value_micro=null`, hash recomputed by the bench — and the USD
  spend memory is untouched while the per-asset spend records (FT1-2).
- **Boundary:** stale price + amount above the approval threshold still
  forces `approval_required` with nothing broadcast (FT1-3).

F-T1's disposition (staleness semantics vs source-timestamp choice vs
hard-deny for pegged assets) remains the owner's policy call, exactly as
the broadcast battery left it. This delta bench neither fixes nor
mitigates it.

## 4. Observations (Info; neither blocks closure)

- **Attempt-registry boundary (as disclosed by the builder):** the
  retry guard's twin protection lives on the Dispatcher instance; the
  in-dispatch reconciliation needs no registry (it keys off
  `intent.submission`). A twin submitted to a *fresh* Dispatcher process
  after the original landed would not be caught by the registry — in
  this cell's operating shape (one Dispatcher per dispatch session; the
  idempotency key is also bound into the desk-auth v2 signature, and a
  twin carrying a *fresh* desk nonce is a new authorization) this is a
  recorded boundary, not a defect against the brief's questions. A
  cross-process idempotency store belongs with the persistence work if
  the pattern is ever replicated.
- The same-intent *adoption* branch of the retry guard (prior landed,
  same intent) is unreachable through the public surface in this build —
  a landed first dispatch terminates CONFIRMED/FINAL/SUBMITTED/UNKNOWN,
  none of which `dispatch` accepts — it stands as defense-in-depth
  behind the state guard. The reachable adoption path (in-dispatch
  reconciliation) is the one exercised throughout §1.

## 5. Integrity

- Bench harness + logs retained at `/tmp/verity_ft2ft3/` (`bench.py`
  46/46, `bench_log.json`, `sandbox_probe.py`, pre/post sandbox receipt
  outputs byte-identical). The temporary pre-fix worktree was removed.
- The benched worktree was left unmodified by this bench (HEAD
  `44b2349`; this record is the only addition). All bench state lived in
  temp dirs; no testnet store was created; no real chain was touched —
  every broadcast in this bench went to the bench's own chain model.

— Verity, W-003 · Director of Quality & Certification · 2026-10-27
