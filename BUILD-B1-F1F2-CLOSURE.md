# BUILD — B1 testnet bench findings F-1 + F-2 closure (builder note)

Scope: LOCAL code only, worktree `agent-wallet-mcp-b0`, branch
`build/b0-asset-core-20261027`. Closes the two code findings from
`BENCH-B1-TESTNET-VERITY.md` (bench HEAD `92861ce`). F-3
(`reserve_addresses` empty) is out of scope for this pass. The funded
half of the testnet bench remains INCOMPLETE for the faucet reason
recorded in `BUILD-B1-TESTNET-REPORT.md` — nothing here changes that.

## F-2 — oracle cause preserved end to end (commit `e4fdcef`)

- `policy.py`: `Verdict` gains `oracle_failure_code`. `evaluate_payment`
  records the mapped cause on every oracle-failure leg: no-oracle
  (`ORACLE_UNAVAILABLE`), two-source disagreement (`ORACLE_DIVERGENCE`),
  stale price (`PRICE_STALE`), pegged-asset degrade except-branch, the
  volatile deny path, and the FX second-quote failure leg. A `_ofc()`
  helper threads the code into all post-oracle verdicts.
- `receipts.py`: receipt gains `oracle_failure_code` alongside
  `price_unverified` (previously the cause collapsed to
  `price_unverified` + refusal code `None`).
- Behavior unchanged: pegged assets still degrade-allow; only the
  recorded cause is now faithful.
- Tests: `tests/test_oracle_cause.py` — the three causes are
  distinguishable on verdicts and receipts.

## F-1 — mandate verification wired at dispatch (commit `d855057`)

Wiring design:

- `state_machine.py`: `PaymentIntent` gains `mandate_credential` /
  `mandate_jti`. `Dispatcher` accepts `mandate_verifier` (callable
  `(intent, payment_context) -> jti`) and `require_mandate=False`.
  `_check_mandate()` runs after the desk-auth check and BEFORE nonce
  peek/consume and BEFORE any chain-backed signing. The payment
  context is built from the asset row + intent (`asset`, `chain`,
  `action="pay"`, `counterparty`, `amount_base`,
  `aggregate_spent_base` from the spend tracker). On acceptance the jti
  is stamped on the intent and lands as `mandate_id` at BOTH
  `build_receipt` sites (dispatch + watch-finality factory), replacing
  the previous `null`.
- `mandate_check.py` (new): `HeraldMandateVerifier(jwks,
  status_list_path)` bridges to eclipse-herald-mcp's proven
  `mandates.verify_mandate_for_payment` + `status_list.StatusList` —
  imported, no cryptography re-implemented. The status list is loaded
  fresh per verification, so a revocation lands without a restart.
  Herald's refusal reasons map onto wallet refusal codes:
  REVOKED→`MANDATE_REVOKED`, EXPIRED→`MANDATE_EXPIRED`,
  SIGNATURE_INVALID/MALFORMED→`MANDATE_INVALID`,
  STATUS_UNAVAILABLE→`MANDATE_UNKNOWN` when the reason is an
  unallocated index, else `MANDATE_STATUS_UNAVAILABLE`,
  LIST_MISMATCH→`MANDATE_STATUS_UNAVAILABLE`,
  SCOPE_*/CONTEXT_INCOMPLETE→`MANDATE_SCOPE`.
  `production_verifier()` resolves the assets dir
  (`ECLIPSE_HERALD_DIR` env or the canonical repo) for
  `keys/jwks.json` + `state/status-list.json`, and the code dir
  separately (`ECLIPSE_HERALD_CODE_DIR` env or canonical repo);
  the code dir is APPENDED to `sys.path` because both repos ship a
  `server.py`. Missing assets → returns `None`, and a presented
  credential with no verifier configured fails closed with
  `MANDATE_STATUS_UNAVAILABLE`.
- `policy.py`: refusal codes `MANDATE_REQUIRED`, `MANDATE_INVALID`,
  `MANDATE_EXPIRED`, `MANDATE_REVOKED`, `MANDATE_UNKNOWN`,
  `MANDATE_SCOPE`, `MANDATE_STATUS_UNAVAILABLE`. All mandate refusals
  are pre-sign with the spend nonce unconsumed.
- `server.py`: `Runtime` builds the production verifier once;
  `dispatcher_for` injects it. The `pay` tool accepts a `mandate`
  argument (non-string/blank → `MANDATE_INVALID`), threads it through
  the approval flow (stored on the pending record, restored by
  `t_resolve_approval`), and the tool schema/description documents it.
- Compatibility: no credential presented + `require_mandate=False` =
  the legacy dispatch path, untouched. `require_mandate=True` with no
  credential → `MANDATE_REQUIRED`.

Tests: `tests/test_mandate_wiring.py` (15 tests, 8 subtests):

- Dispatcher level, hermetic scratch Herald (own 1024-bit JWKS + temp
  `StatusList`): valid mandate executes and the receipt carries the
  jti; revoked scratch mandate → `MANDATE_REVOKED` pre-sign with the
  nonce unconsumed, intent still QUEUED, zero receipts, zero balance
  movement; expired → `MANDATE_EXPIRED`; tampered signature →
  `MANDATE_INVALID`; unallocated status index → `MANDATE_UNKNOWN`;
  wrong-asset scope → `MANDATE_SCOPE`; aggregate over the mandate cap
  → `MANDATE_SCOPE`; no-mandate regression; `require_mandate` →
  `MANDATE_REQUIRED`; presented credential without a verifier →
  `MANDATE_STATUS_UNAVAILABLE`.
- Server level (stdio subprocess, fixture `ECLIPSE_HERALD_DIR`):
  valid mandate pays end to end; revoked mandate refused at the tool
  surface.
- Rollout corpus (read-only; skips if Herald state is absent): all 8
  real desk mandates from
  `eclipse-herald-mcp/state/desk-mandate-rollout-20261027.json` verify
  ALLOW against a payment context drawn from their own scope; status
  indices 0–7 not revoked; the rollout drill index 8 reads revoked.
  No real desk mandate was revoked or mutated by any test.

## Evidence

- Anchor venv (`~/workspace/tools/venv-anchor/bin/python`):
  `pytest tests/ -q` → **179 passed, 11 subtests passed**.
  Collection at the F-2 commit was 164; F-1 adds exactly the 15
  mandate-wiring tests; nothing dropped.
- System `python3`: `python3 -m unittest discover -s tests` →
  **179 ran, OK, 11 skipped** (signer-dependency skips, the standing
  convention on this interpreter).
- Sandbox ledger untouched: sha256
  `7b06c9b1ae2f34096f9d1b3f02db1cbcd5c10325cbd279fda5ac3ea23a1fdeab`
  before and after — matches the B1 record exactly.

## Commits

- `e4fdcef` — F-2 closure (policy + receipts + oracle-cause tests).
- `d855057` — F-1 closure (mandate wiring + tests).

Delta bench: Verity, independent harness — see
`BENCH-B1-F1F2-DELTA-VERITY.md` (verdict recorded there, not here;
builders do not certify their own work).
