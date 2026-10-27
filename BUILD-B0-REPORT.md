# BUILD B0 — Agent Wallet Asset-Aware Core

- **Scope: LOCAL / SANDBOX ONLY.** No live chains, no real funds, no RPC, no network calls, no external publication. No counsel-gated surface touched.
- **Branch:** `build/b0-asset-core-20261027` · **Worktree:** `~/workspace/cyan-eclipse/agent-wallet-mcp-b0`
- **Tool surface:** unchanged — 10 tools. `server.py` was **not modified** by B0 (see Integration).

## Concurrency handling

Main worktree was dirty at start (coordinator's warning confirmed): `M README.md, M server.json, M server.py`; untracked `desk_cli.py, desk_envelope.py, desk_keys.py, desk_verify.py, team-ledger.json`. Start-of-build hashes recorded for `server.py / ledger.json / team-ledger.json / README.md / server.json`. B0 therefore built in the isolated worktree above from HEAD `f84478e`. No stash/reset/overwrite of the other agent's work; no live vaults, desk policies, `ledger.json`, or custody state touched (the worktree at HEAD contains no `ledger.json` at all).

**Mid-build, the issuance agent landed:** main advanced to `516636f` (v0.2.0 desk-key extension) and went clean. B0 merged main into this branch — **zero conflicts** (no overlapping files: B0 adds new modules only; issuance added `desk_*.py` + `server.py` changes). Full suite re-run green after the merge.

## What was built

| File | Contents |
|---|---|
| `amounts.py` | Integer base units everywhere. `parse_amount` / `parse_base_units` / `format_amount` per registry decimals; str/int/Decimal at the boundary only; **float and bool rejected outright**, negatives, over-precision (never rounded), uint256 overflow. Decimal parsing uses exact tuple arithmetic — see "bug found" below. |
| `assets.py` + `assets.json` | Registry, one row per asset×network (Silas §4.1 shape): CAIP-2/CAIP-19 ids, decimals, asset_class, volatility_class, peg bands, `fee_asset_id`, `slate_tier` **separate from** `implementation_status`, three-field lifecycle `listed/live/used` as separate booleans, `live_gate`. 14 rows: sandbox USDC (row #1, the only spend-enabled row), USDC Base/Eth-L1/Solana, EURC Eth/Base (addresses dual-sourced in silas-view §4.2 only), ETH Base/L1 + SOL as gas-role rows (spend-disabled), BTC as reserve-role row, PYUSD/USDT/DAI/USDS with `contract_or_mint=null` + `population_required=true` (no invented addresses). Validation fails closed: duplicate (asset,network) or asset_id, unknown family, decimals mismatch vs canonical, bad address format, non-sandbox spend-enabled row, reserve row spend-enabled. |
| `families/base.py` | `FamilyAdapter` interface (ARCHITECTURE §3.1: chainId, validateAddress, buildPayment, readBalance incl. frozen state, quoteFee w/ TTL, finalityPolicy, health; anchor-pattern methods typed `NOT_IMPLEMENTED` stubs) + `SandboxLedger` + `Balance{spendable|frozen|absent}`. |
| `families/sandbox.py` | `SandboxAdapter` executing against the in-memory sandbox ledger only; instant FINAL. |
| `families/evm.py` | `EvmAdapter` against the sandbox ledger only — **no RPC, no network imports, no signing, no submission** (`submit` is a typed NOT_IMPLEMENTED stub). Pure-Python Keccak-256 (stdlib `sha3_256` is a different function) + EIP-55 validate/normalize; unsigned payment-intent building (native value / ERC-20 `transfer` calldata). |
| `oracle.py` | Injected oracle interface; `StaticOracle` (tests/sandbox); `TwoSourceOracle` — ≥2 distinct sources, staleness and divergence beyond bound raise typed `OracleError` (never silent single-feed valuation). Prices are Decimal strings. |
| `policy.py` | Policy v2, transport-free. Per asset×chain caps in native base units + one USD-normalized aggregate daily cap (anti-structuring). Frozen states: global + per-asset + issuer-frozen balance (`BALANCE_ISSUER_FROZEN`, never spendable). Depeg bands from the registry row: warn band → forced approval; ≥ refuse band → `DEPEG_BAND_EXCEEDED`. Pegged-asset oracle failure degrades loudly (`price_unverified`, forced approval above threshold); volatile-asset oracle failure refuses. Allowlist v2 typed entries validated **at grant time** via the family adapter. Full Silas §5.4 refusal-code enum (+ `ASSET_RESERVE_ONLY`, `BALANCE_ISSUER_FROZEN`, `ORACLE_DIVERGENCE`). Structured policy with canonical `policy_hash` (fixes F8 prose-only receipt). |
| `state_machine.py` | `QUEUED → AUTHORIZED → BUILT → SIGNED → SUBMITTED → CONFIRMED → FINAL` + `UNKNOWN` + `REORGED → REVERSED` (+ EXPIRED/CANCELLED). Queue stores intents, never signed artifacts. **F3 fix:** refusals/check-errors return intents to QUEUED (never FAILED) and never consume the desk nonce. **F4 fix:** dispatch re-runs the full ordered check (frozen → policy → allowlist → caps per-asset + aggregate → desk-auth → balance + gas → quote/depeg freshness → adapter health); nonce consumed **exactly once**, only after every check passes — including a pre-check that a family with no B0 submit path refuses *before* nonce consumption. |
| `receipts.py` | Receipt v2 binding asset_id, chain_id, `amount_base_units` (int), fee, policy_hash, policy verdict, refusal_code, oracle refs/values or `price_unverified`, finality state + provisional flag. Identity fields `agent_id / principal_commitment / mandate_id` present, reserved, null in B0. SHA-256 over canonical JSON; hash recomputation verified in tests; recursive float guard. |
| `ledger_v2.py` | Pure v1→v2 codec (migration wraps v1 sandbox shape into the sandbox USDC row; input never mutated; **live `ledger.json` not migrated**) + `DeltaStore` implementing the 14-point delta law: stored-read-verbatim, hash + set anchors, anchor-presence required, cross-artifact commitments (audit head), derived indexes regenerated (`build_index`, never persisted as truth), fail-closed on missing anchor/corruption/tail-truncation, temporal monotonicity, whole-store validation on load, defect-gated writes, integer-only amounts. |

### BTC invariant R/P-1 (in code, grep-provable)

- **No bitcoin family module exists** — `families/` contains only `base.py`, `evm.py`, `sandbox.py`. No BTC `buildPayment` path exists anywhere.
- Grant naming a reserve-role asset → typed refusal `ASSET_RESERVE_ONLY` at grant time; grant allowlisting a registered reserve address → same refusal.
- Payment attempt in BTC (or to a registered reserve address) → `ASSET_RESERVE_ONLY`.

## Tests — 83, all green (stdlib `unittest`, temp dirs / in-memory only)

Run: `python3 -m unittest discover -s tests -t .`

| Suite | Count | Covers |
|---|---|---|
| `test_amounts` | 9 | parse/format, wei-scale, rejections, seeded fuzz roundtrip (0/6/8/9/18 decimals, 300 cases each) |
| `test_assets` | 11 | registry validation battery, dual-sourced addresses exact, unpopulated rows, tier≠status, lifecycle fields |
| `test_families` | 9 | Keccak empty vector + 4 EIP-55 spec vectors, EVM build (ERC-20/native), NOT_IMPLEMENTED submit, sandbox roundtrip, balance states |
| `test_policy` | 17 | grant-time validation (reserve/NEXT/gas/allowlist/float caps/reserve address), caps, aggregate anti-structuring, frozen states, depeg warn/refuse at 110/300 bps, oracle divergence + staleness |
| `test_state_machine` | 14 | happy path to FINAL + receipt, nonce sequencing, refusal ⇒ QUEUED + nonce intact, approval flow, **dispatch-recheck races** (freeze / cap cut / depeg / balance-freeze / adapter-health mutated between AUTHORIZED and dispatch), legal/illegal transitions incl. REORGED→REVERSED, nonce replay/skip refusal |
| `test_receipts_ledger` | 9 | receipt hash recomputation + tamper detection, reserved identity fields, migration purity, DeltaStore roundtrip / missing anchor / tamper / tail-truncation / defect-gated write / monotonicity |
| `test_btc_invariant` | 5 | no bitcoin module (file list), no BTC pay path (source scan), grant + pay typed refusals, NEXT/WATCH/gas assets cannot be spent |
| `test_malformed` | 7 | malformed-args battery over every public function (wrong types, missing fields, unknown asset/chain, malformed addresses, bad caps) |
| `test_server_smoke` | 1 | existing surface: untouched `server.py` handshake + 10 tools + create/fund/policy/pay flow against a **temp** ledger (re-run green against v0.2.0 after the merge) |
| (existing suite) | — | HEAD shipped no test files; CI = compileall + JSON validation + handshake. `compileall` clean; `assets.json` validates via the registry loader. |

**Bug found by the fuzz suite and fixed:** Decimal `scaleb` honours the ambient context precision (28 digits) and silently rounded a 29-digit wei-scale amount by 2 wei. `parse_amount`'s Decimal path now uses exact `as_tuple()` arithmetic; price conversions run under a 100-digit local context.

## Grep proofs

- **No float in money paths:** `grep -rn "float(" amounts.py assets.py policy.py oracle.py receipts.py state_machine.py ledger_v2.py families/*.py` → the only hits are the receipt module's `_assert_no_float` guard itself. Float appears elsewhere only in `isinstance(..., float)` **rejection** checks and doc comments. (Legacy `server.py` still parses via float — that is the pre-B0 v1 surface, deliberately untouched here; rewiring it is the integration step below.)
- **No BTC pay path:** `families/` file list + source scan in `test_btc_invariant` (asserts no bitcoin adapter class and no `buildPayment` co-occurring with `bip122`).
- **No network in families:** `grep -rnE "urllib|socket|requests|http.client" families/ + B0 modules` → NONE.

## Integration status — what remains (honest)

B0 is a **parallel core**, not yet wired into the MCP transport:

1. **Wire `server.py` (now v0.2.0 on main) to the B0 core** — route `pay` / `check_payment` / `set_policy` / `fund_wallet` through `policy.evaluate_payment` + `Dispatcher`, map v1 policy fields to the sandbox USDC row, and retire the float `parse_amount` / `MICRO` path. Not attempted here: `server.py` was the concurrent agent's file for the whole build window.
2. **Desk-auth v2 message** (`agent-wallet-spend/v2|…|asset_id|chain_id|amount_base|…`) in `desk_verify.py` / `desk_keys.py` — the Dispatcher already takes an injected `desk_auth_verifier`; B0's default checks the intent's auth flag only.
3. **Ledger migration run** — `ledger_v2.migrate_v1_to_v2` is proven on fixtures; running it against the real sandbox `ledger.json` (and the team ledger) is an operator step with read-back verification, not done here.
4. **EURC dual caps** (EUR cap + USD-equivalent, first to bind) — the aggregate USD cap and FX depeg-vs-EUR are implemented; the per-asset EUR-denominated second cap from ASSET-SLATE §1 is a policy-shape addition for B5/EURC enablement.
5. Verity bench at **local** scope is the next gate (BUILD-PLAN B0 exit criteria). Nothing here claims bench, certification, testnet, or live status.

## Out of scope by design (not built)

Solana / Bitcoin / Lightning adapters · Solana/BTC/Lightning families · signing of any kind · watcher process · EVM submission · any live chain, real funds, or counsel-gated surface.
