# BENCH B1 F-1/F-2 DELTA — Verity, W-003, independent bench

- **Scope:** LOCAL/SANDBOX + testnet-path **code** only.
- **Findings under bench:** F-1 and F-2 from `BENCH-B1-TESTNET-VERITY.md`
  (read verbatim before this bench). F-3 was out of scope for the fix
  pass and is not graded here.
- **Build benched:** worktree `agent-wallet-mcp-b0`, branch
  `build/b0-asset-core-20261027`, HEAD `1e83d6e` (builder closure note);
  fix commits under bench: `e4fdcef` (F-2), `d855057` (F-1). Builder
  claims in `BUILD-B1-F1F2-CLOSURE.md` were treated as claims to
  falsify, not as evidence.
- **Date:** 2026-10-27.

## VERDICT (delta only)

| Finding | Verdict |
|---|---|
| F-1 — no mandate-status check in the dispatch path; `mandate_id` permanently null | **CLOSED** (at this scope) |
| F-2 — policy layer collapses oracle failure codes to `price_unverified` / `code=None` | **CLOSED** (at this scope) |

**The funded-broadcast half of the original testnet bench remains
INCOMPLETE** (faucet unfunded — owner/browser action, unchanged by
this delta). **This verdict does not upgrade the testnet cell:** the
USDC × Base Sepolia cell verdict remains **INCOMPLETE** from
`BENCH-B1-TESTNET-VERITY.md` until the funded probes 1–8, 12–15,
17–19 and the live-chain halves of 10/16 run. Nothing here is a live
claim.

## Harness (Verity's own; builder tests not used as evidence)

- `/tmp/verity_delta/harness.py` (32 checks), `server_probe.py`
  (3 checks), `watcher_probe.py` (1 check) — **36/36 PASS**.
- No `tests.b0fixtures` import. Own world builder (own
  `AssetRegistry`/`SandboxLedger`/`SandboxAdapter`/`StaticOracle`/
  `validate_grant` wiring), own constants throughout: wallet
  `wal_verity_delta_9x`, sender `sandbox:verity-sender`, recipient
  `sandbox:verity-recip`, fund 12,345,678 base, payment amount
  777,000 base, per-tx cap 6,000,000, approval threshold 4,500,000.
- Own scratch Herald in a temp dir: own **RSA-2048** keypair
  (kid `verity-delta-20261027`), own status list
  `urn:verity:delta-bench:status-list:v1`, own task/card hashes,
  own agent DID, own JTIs (`mdt_verity_delta_*`). Scratch mandates
  only were ever revoked.
- Receipt hashes were recomputed by the harness itself from
  canonical JSON + SHA-256 — the product's `receipt_hash` helper was
  not used as its own proof.
- Interpreter: `~/workspace/tools/venv-anchor/bin/python`.

## Probe evidence

### P1 — Revoked mandate refused at dispatch, proven pre-sign

Scratch mandate `mdt_verity_delta_revoked_01` issued, then revoked in
Verity's scratch status list (fresh `StatusList.load(...).revoke(idx)`).

```
res.status = refused
refusal_code = MANDATE_REVOKED
before = {nonce_peek:0, nonce0_consumed:False, sender:12345678,
          recip:0, spent_base:0, receipts_on_intent:0}
after  = identical in every field
intent.state = QUEUED, intent.mandate_jti = None
```

Nonce unconsumed, zero receipts, zero balance movement, spend tracker
at zero — the refusal is strictly pre-sign / pre-consume.

### P2 — Valid mandate passes, jti binds onto the receipt

```
res.status = executed
receipt.mandate_id = "mdt_verity_delta_valid_01"  (== the mandate's jti, not null)
receipt_hash recomputed independently: 731229cc093942483f7c7acde087c923eafd60dfce1f732db89d2be184f62daa ✓
balances: recip 0 -> 777000, sender 12345678 -> 11568678
nonce: 0 consumed, peek 1
```

Watcher site (second `build_receipt` site): `watch_finality` driven
from the exact post-dispatch chain-backed state (SUBMITTED, tx_ref
set, `mandate_jti` populated as `_check_mandate` leaves it) emitted
2 receipts — CONFIRMED + FINAL — **both** carrying
`mandate_id = mdt_verity_delta_watch_01`. (Simulated post-dispatch
state over the sandbox adapter; a live chain-backed submit remains
part of the still-unfunded broadcast half — see Residual risks.)

Server surface (own stdio probe, own Herald fixture dir, amounts
33.33 fund / 2.77 pay): valid scratch mandate pays, server receipt
`mandate_id = mdt_verity_delta_srv_valid`; revoked scratch mandate
rejected at the tool surface with `refusal_code = MANDATE_REVOKED`;
wallet balance afterwards exactly **30.56** (only the valid payment
moved money).

### P3 — Expired and tampered mandates, distinct typed codes

```
expired  (issued now-10000s, ttl 3600)  -> refused / MANDATE_EXPIRED,  snapshots identical
tampered signature (sig prefix swapped) -> refused / MANDATE_INVALID, snapshots identical
tampered payload (jti rewritten, sig kept) -> refused / MANDATE_INVALID
```

`MANDATE_REVOKED` / `MANDATE_EXPIRED` / `MANDATE_INVALID` are three
distinct typed codes, each proven pre-sign with untouched snapshots.

Fresh-load proof (same dispatcher + same verifier instance): a valid
mandate executes first (nonce advances to 1); a second mandate is
then revoked **after** both objects were built; dispatch refuses it
`MANDATE_REVOKED` with before/after snapshots identical — revocation
lands without a restart, i.e. the status list is genuinely re-loaded
per verification.

### P4 — Oracle causes distinguishable on verdicts and receipts (F-2)

Pegged sandbox USDC, amount 777,000 (below approval threshold, so the
pegged degrade-allow path executes — the old collapse point). In
every case `verdict.code = None` and `receipt.refusal_code = None`
exactly as before; the **cause** is what now survives:

| Driven failure | Verdict `oracle_failure_code` | Receipt `price_unverified` | Receipt `oracle_failure_code` |
|---|---|---|---|
| Two-source divergence (1.0000 vs 1.0300, TwoSourceOracle) | `ORACLE_DIVERGENCE` | true | `ORACLE_DIVERGENCE` |
| Stale price (single feed, observed 20,000s ago) | `PRICE_STALE` | true | `PRICE_STALE` |
| Oracle unavailable (empty oracle) | `ORACLE_UNAVAILABLE` | true | `ORACLE_UNAVAILABLE` |

All three receipts executed, hashes recomputed independently ✓,
pairwise-distinct causes ✓. Extra paths: stale inside a
TwoSourceOracle (one fresh + one stale source) → receipt
`PRICE_STALE`; `oracle=None` at the policy layer → verdict
`ORACLE_UNAVAILABLE`. Healthy contrast: fresh verified price →
executed with `price_unverified = False`,
`oracle_failure_code = None`.

### P5 — Corpus, read-only

All **8/8** real desk mandates from
`eclipse-herald-mcp/state/desk-mandate-rollout-20261027.json` verify
ALLOW through the production verifier against a payment context drawn
from each desk's own scope (amount 1 base), each returning its
recorded jti (jett, argus, silas, verity, rook, marlowe, sterling,
fathom — all True). Status indices 0–7 read not-revoked; the rollout
drill index 8 reads revoked. No real mandate was revoked, mutated or
written. Corpus file hashes byte-identical before/after the whole
bench:

```
status-list.json                  56f26ec4487a3e07de0609487c9d1ca3cee69695d01e1a621ed6a49254ec858e
desk-mandate-rollout-20261027.json 6d2781b4d55e91e15e849530a7e5b270248aa129937b110ec923c90ad936d351
keys/jwks.json                     29e0bd82617f58f4418af61fa39e9805c650a7d74c5c6d5574db3569b33e99a1
```

### Regression

```
Anchor venv:  pytest tests/ -q  ->  179 passed, 11 subtests passed in 19.38s
```

Builder's count (179 + 11 subtests) reproduced exactly, independently.

Sandbox ledger `~/workspace/cyan-eclipse/agent-wallet-mcp/ledger.json`
sha256 **unchanged before and after** the entire delta bench:

```
7b06c9b1ae2f34096f9d1b3f02db1cbcd5c10325cbd279fda5ac3ea23a1fdeab
```

## Per-finding grading

- **F-1: CLOSED.** Mandate verification is wired at dispatch
  (`Dispatcher._check_mandate`, after desk-auth, before nonce
  peek/consume and before any signing), via the Herald bridge
  (signature + expiry + status-list revocation + scope, status list
  re-loaded per verification). Revoked/expired/tampered are typed
  pre-sign refusals with proven zero side effects (P1, P3); a valid
  mandate's jti lands as `mandate_id` at the dispatch receipt site,
  at the watcher receipt site, and over the MCP server surface
  (P2). The production corpus verifies 8/8 read-only (P5).
  Caveat recorded, not a reopening: wiring is opt-in per dispatcher —
  with `require_mandate=False` (default) a mandate-**less** payment
  still follows the legacy path; a presented credential is always
  verified and fails closed when no verifier is configured. Any
  identity-bound claim for a deployment must therefore be made only
  where `require_mandate=True` is configured.
- **F-2: CLOSED.** The oracle failure cause is preserved end to end:
  policy `Verdict.oracle_failure_code` → receipt
  `oracle_failure_code`, for divergence, staleness and unavailability
  alike, at both verdict and receipt level (P4). Behaviour is
  unchanged by design (pegged degrade-allow still proceeds
  unverified); what changed is that the reason is now recorded and
  auditable on the receipt instead of collapsing to a bare
  `price_unverified` with code `None`.

## Residual risks

1. **Funded testnet half still INCOMPLETE.** Faucet unfunded; no
   broadcast, confirmation-depth, fee-truth or gas probe at live
   testnet scope was re-run. The testnet cell stays INCOMPLETE and
   exports nothing to a second cell.
2. **Divergence still degrades, now visibly.** A below-threshold
   pegged payment proceeds on actively divergent feeds (original
   F-2's "consider hard-deny" was a consideration, not the graded
   defect). The receipt now proves *why* it was unverified; making
   divergence a hard deny is an owner policy decision, not a code
   defect found here.
3. **Watcher mandate binding executed at simulated post-dispatch
   state**, not from a real chain-backed submit (that path needs the
   funded window or a scripted-RPC chain adapter run, which was
   original-bench probe 16 territory, not this delta's mandate
   question).
4. **F-3 untouched** (`reserve_addresses` empty in shipped configs) —
   out of scope for this fix pass, still open as recorded.
5. Mandate scope context at dispatch is built from the asset row +
   intent with `aggregate_spent_base` from the local spend tracker;
   cross-wallet aggregate enforcement under one mandate is only as
   strong as that tracker (local scope fact, not a new finding).

— Verity, W-003 · Director of Quality & Certification · 2026-10-27
