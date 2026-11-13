# BUILD B1-INTEGRATION — Agent Wallet: B0 core wired into the server

- **Scope: LOCAL / SANDBOX ONLY.** No testnet transactions were executed, no live chain touched, no real funds moved. This step closes the B0 bench conditions C-1–C-3 and the four integration steps the B0 report owed, and produces the testnet bench plan for Verity. It does **not** bench anything itself.
- **Branch:** `build/b0-asset-core-20261027` · **Worktree:** `~/workspace/cyan-eclipse/agent-wallet-mcp-b0`
- **Tool surface:** unchanged — exactly 10 tools (asserted by test). Server version 0.3.0.

## Commits (each pushed)

| Commit | Step |
|---|---|
| `b9d0534` | C-1 policy-layer staleness + EURC dual cap (d) + PolicyV2 dict codec |
| `860318a` | C-3: one receipt per transition (D-3) + PaymentIntent TTL (D-4) |
| `a220040` | C-2: typed adapter errors (D-2) |
| `610164e` | (b) desk-auth v2 canonical message (desk_verify / desk_keys) |
| `4a30b0c` | (a) server.py runs on the B0 core; float path retired |
| `7cbccb1` | (b, client) desk_cli pays with v2 signatures; server.json 0.3.0 |
| `daa4b33` | (c) operator ledger migration tool + codec desk-state carry |
| `01df811` | Test batteries (47 new tests) |

## Per-step results

### (a) server.py → policy v2 + Dispatcher — DONE
- Every tool call assembles a per-call `Runtime` over the persisted v2 store: `SandboxLedger` seeded from stored balances, `SpendTracker` seeded from today's persisted FINAL receipts, `WalletNonceBook` over the wallet records. Policy state therefore survives process restarts exactly (proven: daily cap enforced across a server restart in `test_server_integration`).
- `pay` / `approve_payment` run the full Dispatcher path: authorize → dispatch-time **full re-check** (F4) → nonce consumed exactly once (F3) → exactly one receipt v2, persisted in the store.
- The v1 float path is **retired**: `parse_amount`/`MICRO`/`fmt(float)` deleted from the server; amounts enter only via `amounts.parse_amount` into integer base units (float JSON amounts are refused typed). `desk_cli`'s client-side float conversion retired the same way.
- State is a schema-v2 `DeltaStore` (anchor-checked, fail-closed). A v1-shaped ledger is **refused** with migration instructions — never silently misread.

### (b) desk-auth v2 — DONE
- `desk_verify.spend_message_v2` implements silas-view §3.2 verbatim: `agent-wallet-spend/v2|{wallet_id}|{asset_id}|{chain_id}|{to}|{amount_base}|{memo}|{nonce}|{idempotency_key}`.
- Server verifies v2 first and **accepts v1 as fallback** (the v0.2.0 desk flow keeps working unchanged — proven by stdio test: v1-signed pay executes). `DeskSigner.sign_spend_v2` added over the shared sign path (same wallet-binding refusal; key transient in memory only). v1 functions untouched.
- Desk pays additionally enforce **idempotency-key binding**: a key already used for a different (to, amount) on the wallet is refused even with a valid signature.

### (c) ledger migration (operator step) — DONE for the sandbox ledger
- Tool: `tools/migrate_ledger.py` — sha256 before → pure codec → **field-level losslessness accounting** (any unmapped v1 field aborts with nothing written) → byte-identical rollback copy (hash-verified) → DeltaStore write → anchor-checked reload → accounting re-run.
- **Run against the real sandbox ledger** `~/workspace/cyan-eclipse/agent-wallet-mcp/ledger.json`: sha256 before `a3d36ce86b86215b07d695a6478f9ba7f1d822c7fa5606232c73a51add96c02f` (matches the B0 bench's recorded hash exactly) → after `7b06c9b1ae2f34096f9d1b3f02db1cbcd5c10325cbd279fda5ac3ea23a1fdeab`; rollback `ledger.json.v1-backup-20261027162735`; 1 wallet / 12 audit / 1 pending; reload + accounting PASS, re-verified in a fresh process.
- **`team-ledger.json` was NOT migrated** (dry-run only): it is desk state, outside this step's scope. Its accounting passes with the fixed codec (below), so it *can* migrate whenever the desk program chooses.

### (d) EURC EUR-denominated second cap — DONE
- `AssetPolicy.daily_cap_peg_micro` (micro-units of the peg target): the peg-currency cap and the USD aggregate cap **both bind, first hit refuses** (`OVER_PEG_DAILY_CAP`). Cap is inside `policy_hash`, validated at grant time (pegged assets only, non-negative int), tracked per-day by `SpendTracker.spent_peg_micro`, and round-trips through the policy dict codec + store validation. Proven on a sandbox-family EURC registry variant both directions (peg binds alone, USD binds alone, accumulation binds).

## Bench conditions closed (evidence)

| Condition | Defect | Fix | Evidence |
|---|---|---|---|
| **C-1** | D-1: staleness enforced only inside TwoSourceOracle; stale StaticOracle accepted as verified | `policy._fresh_or_raise`: the policy layer age-checks **every** snapshot it consumes (peg quote + FX USD quote), any Oracle implementation; stale degrades/denies exactly like oracle-side PRICE_STALE — never silently verified. `max_staleness_s` is a policy/Dispatcher parameter (default 300 s) | `tests/test_policy_staleness.py` (7): fresh-verified control, stale small → `price_unverified`, stale above threshold → forced approval, future price stale, parameter sensitivity, TwoSource control, staleness arriving between authorize and dispatch lands on the receipt |
| **C-2** | D-2: `EvmAdapter.buildPayment({})` raised bare `KeyError`. D-3: duplicate FINAL receipts | `require_intent_fields` / `require_artifact_fields` in `families/base.py`; EVM + Sandbox build/quote/submit validate up front → `AdapterError(INTENT_FIELD_MISSING / ARTIFACT_INVALID)`. Receipt contract: **exactly one receipt per state transition** (see C-3 row) | `tests/test_adapter_typing.py` (7) incl. the exact Verity D-2 probe; `test_malformed` adapter battery tightened to require AdapterError specifically |
| **C-3** | D-4: no intent TTL; queued approvals never expired | `PaymentIntent.created_at + ttl_s + expires_at`; Dispatcher refuses `APPROVAL_EXPIRED` at authorize **and** dispatch, moving QUEUED/AUTHORIZED intents to EXPIRED (new legal transition), nonce never consumed. Server wiring: intents carry a 900 s TTL; queued approval requests persist `expires_at` (900 s) and resolve as `expired` — never executed | `tests/test_receipt_ttl.py` (7) + server pending-expiry stdio test with exactly-one-receipt assertions in `test_server_integration.py` |

## Suites

- **130 tests, all green** (was 83): +7 staleness, +7 adapter typing, +7 receipt/TTL, +8 desk-auth v2, +7 EURC dual cap, +10 server integration, +1 malformed tightening.
- Run under system `python3` (1 skip: DeskSigner roundtrip needs `eth_account`) **and** under `~/workspace/tools/venv-anchor/bin/python` (0 skips, all green) — the DeskSigner v2 roundtrip + wallet-binding refusal are proven, not skipped, in the venv run.
- `test_server_smoke` passes unchanged against the integrated server.

## Behaviour changes vs v0.2.0 (deliberate, disclosed)

1. **Approval requests expire after 900 s** (C-3). An expired approval resolves as `expired`, executes nothing.
2. **`set_policy` validates allowlist addresses at grant time** (policy v2 grant validation): arbitrary strings (e.g. the old demo's `0xMerchantA`) are now refused; sandbox addresses or EVM-shaped `0x…` only.
3. **Desk nonce gaps are refused** (`DESK_AUTH_NONCE`): the benched v2 semantics — strictly sequential nonces (desk_cli already issues last+1).
4. The migrated `ledger.json` is schema v2: the **main-repo v0.2.0 server can no longer read it** (it fails loudly on the new shape). The byte-identical rollback copy restores v1 if ever needed. `team-ledger.json` + main-repo server are untouched and mutually compatible, exactly as before.

## Findings

- **F-B1-1 (fixed in `daa4b33`):** the B0 v1→v2 codec silently dropped desk-extension state (`desk`, `spend_nonce`) from wallets — a migration of any desk-bound ledger would have been lossy. Caught by the migration tool's own field accounting on the team-ledger dry-run (which aborted, nothing written). Codec now carries both fields verbatim; accounting passes for both ledgers.
- **No other blockers.** Nothing in this step required touching desk custody or desk policies; none were touched.

## Next gate

Verity bench of the B1 testnet cell per `TESTNET-BENCH-PLAN.md` (program docs: `~/workspace/cyan-eclipse/agent-wallet-multicrypto-20261027/`). The remaining pre-bench build work (EVM submission path + watcher + testnet registry rows + fund envelope) is enumerated there as bench-entry tasks — it is B1 build proper, not part of this integration step. All live gates in BUILD-PLAN §3 remain closed.

— Silas Forge, W-002 · Director of Product Engineering · 2026-10-27
