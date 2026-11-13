# BUILD B1 — F-T2 + F-T3 closure (builder record; NOT a bench verdict)

- **Findings addressed:** F-T2 (High, gating) and F-T3 (Medium) from
  `BENCH-B1-TESTNET-VERITY.md` (broadcast battery re-run, verdict
  PASS-CONDITIONAL (testnet)). **F-T1 was NOT touched** — the pegged
  degrade-allow behavior is an owner policy decision, exactly as the
  bench left it.
- **Branch:** `build/b0-asset-core-20261027`. Scope unchanged:
  chain-backed mode remains testnet-only (`eip155:84532`); mainnet
  refusal at adapter + signer construction is unmodified and covered
  by the standing tests.
- **Suite:** 191 passed + 11 subtests (venv, pytest AND unittest);
  system python3 unittest: 191 ran, OK (22 signer-dep skips, the
  pre-existing pattern). Baseline was 179 + 11 — no regressions.
  Sandbox invariance: no sandbox code path changed; receipts built
  without a chain actual are byte-identical in shape (proven by
  `SandboxInvarianceTests`), and `_actual_fee` returns None for any
  non-chain-backed adapter.

## F-T2 — ambiguous submit reconciles before any refusal

`state_machine.py` (Dispatcher), `families/evm.py` (reads):

- The signed artifact determines the tx hash, so the dispatcher now
  registers the broadcast attempt (`_register_attempt`) the moment
  signing succeeds — before submit — keyed by intent id and by
  (wallet_id, idempotency_key); the record also lands on
  `intent.submission`.
- A submit exception on a chain-backed adapter no longer returns
  `refused / CHAIN_UNHEALTHY` on the transport error alone.
  `_reconcile` reads the chain back (receipt, then tx-by-hash, then
  the sender's mined nonce) for up to `reconcile_window_s`
  (default 180 s — the finality confirmation window; injectable):
  - **landed** → the submission is adopted: the normal post-submit
    tail runs on the recorded hash (status, exactly-one-receipt
    contract, spend recorded once at FINAL). Books cannot diverge
    from chain on this path.
  - **absent** (hash unknown AND sender nonce unconsumed at window
    end — proven non-inclusion) → the refusal stands, and the
    `DispatchResult` carries the reconciliation evidence
    (`evidence` field; also appended to `intent.reconciliation`,
    and summarized in the refusal reason).
  - **inconclusive** (nonce slot consumed, tx not locatable) → NOT
    a refusal: the intent is parked SUBMITTED → UNKNOWN on its
    recorded hash for `watch_finality` resolution. It can never be
    re-dispatched into a second broadcast (dispatch raises on the
    UNKNOWN state).
  - Transport errors during reconciliation are counted in the
    evidence, never read as absence.
- Retry guard: dispatch of an intent with a prior attempt reconciles
  that attempt first. Same intent + landed → adopted, no new
  broadcast. Same intent + proven absent → ONE fresh broadcast; the
  desk nonce consumed by the earlier attempt of THIS intent is
  recognized and not consumed twice. Twin intent reusing the
  (wallet, idempotency key) → `DUPLICATE_SUBMISSION` refusal when
  the prior submission landed or cannot be proven absent.
- New adapter reads: `get_transaction`, `chain_nonce_latest`
  ("latest" — mined-only, distinct from the "pending" nonce used to
  build new transactions).
- Boundary, stated honestly: the attempt registry lives on the
  Dispatcher instance (the testnet cell's harness holds one). The
  in-dispatch reconciliation itself needs no registry — it keys off
  `intent.submission`, which is recorded on the intent before submit.

## F-T3 — receipts record the actual fee; caps bind the estimated total

`families/evm.py`, `families/base.py`, `fund_signer.py`,
`receipts.py`, `policy.py`:

- **Quote = estimated total.** Chain-backed `quoteFee` now returns
  `amount_base = L2 estimate + L1 estimate`, with the components on
  the quote (`FeeQuote.l2_amount_base` / `l1_amount_base`; None
  outside chain-backed scope). The L1 leg comes from the OP-Stack
  GasPriceOracle (`getL1Fee(bytes)` at the 0x4200…000F predeploy)
  over a length-faithful representative type-2 serialization
  (in-adapter RLP; the stand-in signature slots are non-zero, so the
  estimate errs conservative). **Fail-closed:** any failure to
  obtain the L1 estimate raises `FEE_ESTIMATE_UNAVAILABLE` — no
  L2-only quote is ever issued on this chain class.
- **Caps bind the total.** The policy-level `max_fee_base` check and
  the fund signer's `FEE_OVER_CAP` check both already bind
  `amount_base`; with the quote redefined as the total, both now
  bind the estimated total. The signer additionally binds the
  artifact's own numbers: `finalize_unsigned` embeds
  `l1_fee_estimate_base` (+ `fee_estimate_total_base`) for the exact
  artifact, and `sign_pay` refuses `FEE_OVER_CAP` when
  L2 max + L1 estimate exceeds the presented cap — or when the
  artifact carries no L1 estimate at all (fail-closed).
- **Receipts record actuals.** `build_receipt` accepts `fee_actual`;
  when the dispatcher builds a receipt for a chain-backed payment
  (at dispatch and in `watch_finality`'s factory) it reads the
  actual fee from the mined receipt via `EvmAdapter.actual_fee` —
  L2 = gasUsed × effectiveGasPrice plus the receipt's `l1Fee` — and
  the receipt's `fee` then carries `quoted_amount_base` (the quote,
  also still in `amount_base`) AND `actual_amount_base`,
  `actual_l2_amount_base`, `actual_l1_fee_base`. A fee-read failure
  never fails a proven payment; the receipt then carries the quote
  only. Receipt hashes remain self-consistent (fields are part of
  the hashed body, as before).
- `FakeRpc` (test chain model) learned the GasPriceOracle eth_call
  and reports `l1Fee` on receipts, so the suite models both legs.

## Proofs (tests/test_ft2_ft3_fixes.py, 12 tests)

Mock transports over FakeRpc: send-response dropped after landing;
dropped without landing; nonce eaten but tx unlocatable; GasPriceOracle
dead. Coverage: reconciled landing reported executed (one broadcast,
one FINAL receipt, spend once); proven-absent refusal carries
evidence; same-intent retry after proven absence broadcasts exactly
once (desk nonce consumed once); inconclusive parks UNKNOWN and
re-dispatch is impossible; twin intent with reused idempotency key
refused `DUPLICATE_SUBMISSION` with zero new broadcasts; quote ==
L2 + L1 with components; policy cap between L2-only and total now
refuses `FEE_OVER_CAP` pre-broadcast; signer refuses cap below the
total and artifacts lacking an L1 estimate; estimate failure fails
closed at quote and at dispatch (zero broadcasts); FINAL receipt
carries quote + actual (L2/L1 split) and verifies its hash; sandbox
receipt shape unchanged.

**STOP — awaiting Verity delta bench.** This record is the builder's
closure note, not a verdict; F-T2/F-T3 stand closed only when the
delta bench says so.
