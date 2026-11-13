# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""Payment state machine + dispatcher for Agent Wallet B0.

LOCAL/SANDBOX ONLY. States (ARCHITECTURE.md S4):
QUEUED -> AUTHORIZED -> BUILT -> SIGNED -> SUBMITTED -> CONFIRMED -> FINAL
plus UNKNOWN and REORGED -> REVERSED.

House laws enforced here:
- The queue stores intents, never signed artifacts.
- Check errors / refusals return intents to QUEUED, never FAILED, and
  NEVER consume the desk nonce (fixes F3).
- Dispatch re-runs the FULL policy check at execution time (fixes F4):
  frozen -> policy present/unexpired -> allowlist -> caps (per-asset +
  USD aggregate) -> desk-auth -> Herald mandate status (F-1: signature
  + expiry + status-list revocation, verified pre-sign via
  mandate_check; an accepted mandate's jti binds onto the receipt) ->
  balance + gas -> quote/depeg freshness -> adapter health. The desk
  nonce is consumed exactly once, only after every check passes.
- F-T2: a broadcast whose submit RESPONSE is lost is ambiguous, not
  refused. Dispatch reconciles before it may refuse: the signed
  artifact's tx hash (and the sender nonce) are read back from the
  chain; a landed tx is adopted and reported truthfully, a refusal
  stands only when reconciliation PROVES non-inclusion (sender nonce
  unconsumed at the end of the confirmation window) and carries the
  reconciliation evidence, and an inconclusive outcome parks the
  intent in UNKNOWN on the recorded hash - never a second broadcast.
  Attempts are registered by intent id and by (wallet, idempotency
  key), so a retry of the same intent - or a twin intent reusing the
  idempotency key - can never produce a second broadcast either.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from policy import Refusal, RefusalCode, evaluate_payment
from receipts import build_receipt


class State(str, Enum):
    QUEUED = "QUEUED"
    AUTHORIZED = "AUTHORIZED"
    BUILT = "BUILT"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FINAL = "FINAL"
    UNKNOWN = "UNKNOWN"
    REORGED = "REORGED"
    REVERSED = "REVERSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


LEGAL_TRANSITIONS = {
    State.QUEUED: {State.AUTHORIZED, State.EXPIRED, State.CANCELLED},
    State.AUTHORIZED: {State.BUILT, State.QUEUED, State.CANCELLED,
                       State.EXPIRED},
    State.BUILT: {State.SIGNED, State.QUEUED},
    State.SIGNED: {State.SUBMITTED, State.QUEUED},
    State.SUBMITTED: {State.CONFIRMED, State.UNKNOWN, State.QUEUED},
    State.CONFIRMED: {State.FINAL, State.REORGED, State.UNKNOWN},
    State.FINAL: {State.REORGED},
    State.UNKNOWN: {State.CONFIRMED, State.FINAL, State.QUEUED},
    State.REORGED: {State.REVERSED},
    State.REVERSED: set(),
    State.EXPIRED: set(),
    State.CANCELLED: set(),
}


class TransitionError(Exception):
    code = "ILLEGAL_TRANSITION"


class NonceError(Exception):
    code = "DESK_AUTH_NONCE"


class MandateCheckFailed(Exception):
    """Raised by a Dispatcher's mandate_verifier (see mandate_check).
    .code is a policy.RefusalCode value naming the refusal
    (MANDATE_REVOKED / MANDATE_EXPIRED / MANDATE_INVALID /
    MANDATE_UNKNOWN / MANDATE_SCOPE / MANDATE_STATUS_UNAVAILABLE)."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class NonceLedger:
    """Per-wallet desk nonce book. A nonce is consumed exactly once and
    only by consume(), which the dispatcher calls after all checks pass."""

    def __init__(self):
        self._next: dict[str, int] = {}
        self._consumed: dict[str, set] = {}

    def peek(self, wallet_id: str) -> int:
        return self._next.get(wallet_id, 0)

    def consume(self, wallet_id: str, nonce: int) -> None:
        consumed = self._consumed.setdefault(wallet_id, set())
        if nonce in consumed:
            raise NonceError(f"nonce {nonce} for {wallet_id} already consumed")
        if nonce != self.peek(wallet_id):
            raise NonceError(f"nonce {nonce} is not the next nonce "
                             f"{self.peek(wallet_id)} for {wallet_id}")
        consumed.add(nonce)
        self._next[wallet_id] = nonce + 1

    def is_consumed(self, wallet_id: str, nonce: int) -> bool:
        return nonce in self._consumed.get(wallet_id, set())


@dataclass
class PaymentIntent:
    wallet_id: str
    from_address: str
    asset_id: str
    chain_id: str
    to: str
    amount_base: int
    memo: str = ""
    intent_id: str = field(default_factory=lambda: "int_" + uuid.uuid4().hex[:12])
    desk_nonce: int | None = None
    desk_auth_ok: bool = False
    approval_granted: bool = False
    state: State = State.QUEUED
    history: list = field(default_factory=list)  # (state, ts, note)
    refusal_codes: list = field(default_factory=list)
    artifact: dict | None = None  # built artifact only; never a signature
    tx_ref: str | None = None
    receipts: list = field(default_factory=list)
    # Intent TTL (bench condition C-3 / defect D-4, silas S5.3
    # pending-expiry): an intent that sits queued/authorized past its
    # TTL is refused APPROVAL_EXPIRED and moved to EXPIRED - a stale
    # authorization is never dispatched. None = no TTL (B0 behaviour).
    created_at: int = field(default_factory=lambda: int(time.time()))
    ttl_s: int | None = None
    # Client-supplied idempotency key (bound into the desk-auth v2
    # message; one signature authorizes one logical payment).
    idempotency_key: str = ""
    # Desk-auth v2 presentation for chain-backed dispatch (B1 testnet):
    # the dispatcher presents these to the fund signer, which refuses
    # without a valid authorization. Signature is hex; the public key
    # is the desk key's 64-byte X||Y. Neither is key material.
    desk_auth_signature: str | None = None
    desk_public_key: bytes | None = None
    # F-1: a presented Herald mandate credential (compact JWS). When
    # set, dispatch verifies it with Herald BEFORE signing; on success
    # mandate_jti is populated and binds onto every receipt.
    mandate_credential: str | None = None
    mandate_jti: str | None = None
    # F-T2: the broadcast attempt record, set the moment a signed
    # artifact exists ({tx_hash, chain_nonce, from_address}). The tx
    # hash is known BEFORE submit - it is what reconciliation reads
    # back when the submit response is lost. reconciliation holds one
    # evidence dict per reconciliation run.
    submission: dict | None = None
    reconciliation: list = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.amount_base, bool) or not isinstance(self.amount_base, int):
            raise ValueError("amount_base must be an int of base units")
        if self.amount_base <= 0:
            raise ValueError("amount_base must be positive")
        if self.ttl_s is not None and (isinstance(self.ttl_s, bool)
                                       or not isinstance(self.ttl_s, int)
                                       or self.ttl_s <= 0):
            raise ValueError("ttl_s must be a positive int or None")
        self.history.append((self.state.value, int(time.time()), "created"))

    @property
    def expires_at(self) -> int | None:
        return None if self.ttl_s is None else self.created_at + self.ttl_s

    def is_expired(self, at: int) -> bool:
        exp = self.expires_at
        return exp is not None and at > exp

    def transition(self, target: State, note: str = "") -> None:
        if target not in LEGAL_TRANSITIONS[self.state]:
            raise TransitionError(
                f"illegal transition {self.state.value} -> {target.value}")
        self.state = target
        self.history.append((target.value, int(time.time()), note))


@dataclass
class DispatchResult:
    status: str  # executed | refused | approval_required
    intent: PaymentIntent
    refusal_code: RefusalCode | None = None
    reason: str = ""
    receipt: dict | None = None
    # F-T2: reconciliation evidence, when a result was decided by
    # reading the chain back (ambiguous submit / retry guard).
    evidence: dict | None = None


class SpendTracker:
    """Per-wallet daily spend in base units per asset + USD micro total
    + peg-currency micro total per asset (the EURC dual cap's memory).
    Updated only on successful dispatch (never on a refusal)."""

    def __init__(self, clock=None):
        self._clock = clock or (lambda: int(time.time()))
        self._base: dict[tuple, int] = {}
        self._usd: dict[tuple, int] = {}
        self._peg: dict[tuple, int] = {}

    def _day(self) -> int:
        return self._clock() // 86400

    def spent_base(self, wallet_id: str, asset_id: str) -> int:
        return self._base.get((wallet_id, asset_id, self._day()), 0)

    def spent_usd_micro(self, wallet_id: str) -> int:
        return self._usd.get((wallet_id, self._day()), 0)

    def spent_peg_micro(self, wallet_id: str, asset_id: str) -> int:
        return self._peg.get((wallet_id, asset_id, self._day()), 0)

    def record(self, wallet_id: str, asset_id: str, amount_base: int,
               usd_micro: int | None, peg_micro: int | None = None):
        day = self._day()
        key = (wallet_id, asset_id, day)
        self._base[key] = self._base.get(key, 0) + amount_base
        if usd_micro is not None:
            ukey = (wallet_id, day)
            self._usd[ukey] = self._usd.get(ukey, 0) + usd_micro
        if peg_micro is not None:
            self._peg[key] = self._peg.get(key, 0) + peg_micro


class Dispatcher:
    """Owns authorize + dispatch for intents against a PolicyV2."""

    def __init__(self, *, registry, adapters: dict, oracle, policy,
                 nonces: NonceLedger | None = None,
                 tracker: SpendTracker | None = None, clock=None,
                 desk_auth_verifier=None, max_staleness_s: int = 300,
                 mandate_verifier=None, require_mandate: bool = False,
                 reconcile_window_s: int = 180, reconcile_poll_s: int = 2,
                 sleep=None):
        self.registry = registry
        self.adapters = adapters
        self.oracle = oracle
        self.policy = policy
        self.nonces = nonces if nonces is not None else NonceLedger()
        self.tracker = tracker if tracker is not None else SpendTracker(clock)
        self._clock = clock or (lambda: int(time.time()))
        self.max_staleness_s = max_staleness_s
        # F-T2: reconciliation reads the chain back for up to
        # reconcile_window_s (the finality confirmation window) before
        # an ambiguous submit may be called refused or unresolved.
        if reconcile_window_s <= 0 or reconcile_poll_s <= 0:
            raise ValueError("reconcile window/poll must be positive")
        self.reconcile_window_s = reconcile_window_s
        self.reconcile_poll_s = reconcile_poll_s
        self._sleep = sleep or time.sleep
        # F-T2: broadcast attempts, by intent id and by
        # (wallet_id, idempotency_key) - the retry guard's memory.
        self._attempts_by_intent: dict = {}
        self._attempts_by_idem: dict = {}
        # desk_auth_verifier(intent) -> bool; default: intent.desk_auth_ok
        self._verify_auth = desk_auth_verifier or (lambda i: i.desk_auth_ok)
        # F-1: mandate_verifier(intent, payment_context) -> jti, raising
        # MandateCheckFailed on refusal (see mandate_check for the
        # Herald bridge). require_mandate makes a credential mandatory
        # on this dispatch path; a presented credential is ALWAYS
        # verified, and fails closed when no verifier is configured.
        self.mandate_verifier = mandate_verifier
        self.require_mandate = require_mandate
        # B1 testnet: last verdict per intent (watcher receipt factory)
        # + intents whose spend has already been recorded in the tracker
        # (recorded exactly once, at FINAL).
        self._last_verdict: dict = {}
        self._tracker_recorded: set = set()

    def _evaluate(self, intent: PaymentIntent, fee_quote=None):
        return evaluate_payment(
            registry=self.registry, adapters=self.adapters,
            oracle=self.oracle, policy=self.policy,
            wallet_id=intent.wallet_id, from_address=intent.from_address,
            asset_id=intent.asset_id, to=intent.to,
            amount_base=intent.amount_base,
            spent_today_base=self.tracker.spent_base(intent.wallet_id,
                                                     intent.asset_id),
            spent_today_usd_micro=self.tracker.spent_usd_micro(intent.wallet_id),
            spent_today_peg_micro=self.tracker.spent_peg_micro(
                intent.wallet_id, intent.asset_id),
            max_staleness_s=self.max_staleness_s,
            clock=self._clock, fee_quote=fee_quote)

    def _check_mandate(self, intent: PaymentIntent) -> DispatchResult | None:
        """F-1: Herald mandate enforcement at dispatch — BEFORE any
        signing and BEFORE the desk nonce is consumed. A presented
        credential is verified (signature + expiry + status-list
        revocation + scope) through the configured verifier; an
        accepted mandate's jti is recorded on the intent and binds
        onto the receipt. Returns a refusal DispatchResult, or None
        when dispatch may proceed."""
        credential = intent.mandate_credential
        if credential is None:
            if self.require_mandate:
                return self._refuse(
                    intent, RefusalCode.MANDATE_REQUIRED,
                    "this dispatch path requires a Herald mandate "
                    "credential; none was presented")
            return None
        if self.mandate_verifier is None:
            return self._refuse(
                intent, RefusalCode.MANDATE_STATUS_UNAVAILABLE,
                "a mandate credential was presented but no mandate "
                "verifier is configured on this dispatcher; failing "
                "closed (an unverifiable mandate is never ignored)")
        row = self.registry.require(intent.asset_id)
        context = {
            "asset": row["symbol"], "chain": intent.chain_id,
            "action": "pay", "counterparty": intent.to,
            "amount_base": intent.amount_base,
            "aggregate_spent_base": self.tracker.spent_base(
                intent.wallet_id, intent.asset_id)}
        try:
            jti = self.mandate_verifier(intent, context)
        except MandateCheckFailed as e:
            try:
                code = RefusalCode(e.code)
            except ValueError:
                code = RefusalCode.MANDATE_INVALID
            return self._refuse(intent, code, e.reason)
        except Exception as e:  # verifier blew up: fail closed
            return self._refuse(
                intent, RefusalCode.MANDATE_STATUS_UNAVAILABLE,
                f"mandate verifier failed: {e}")
        if not isinstance(jti, str) or not jti:
            return self._refuse(
                intent, RefusalCode.MANDATE_INVALID,
                "mandate verifier returned no jti for an accepted "
                "mandate; failing closed")
        intent.mandate_jti = jti
        return None

    def _refuse(self, intent: PaymentIntent, code, reason: str,
                evidence: dict | None = None) -> DispatchResult:
        if intent.state != State.QUEUED:
            # Check errors return the intent to QUEUED, never FAILED,
            # provided the state machine permits it from here.
            if State.QUEUED in LEGAL_TRANSITIONS[intent.state]:
                intent.transition(State.QUEUED, f"refused: {code.value}")
        intent.refusal_codes.append(code.value if isinstance(code, RefusalCode)
                                    else str(code))
        return DispatchResult("refused", intent,
                              code if isinstance(code, RefusalCode) else None,
                              reason, evidence=evidence)

    def _expiry_refusal(self, intent: PaymentIntent) -> DispatchResult | None:
        """C-3/D-4: refuse an intent past its TTL. Moves it to EXPIRED
        when the state machine allows; never consumes the nonce."""
        if not intent.is_expired(self._clock()):
            return None
        if State.EXPIRED in LEGAL_TRANSITIONS[intent.state]:
            intent.transition(State.EXPIRED,
                              f"intent TTL {intent.ttl_s}s elapsed")
        intent.refusal_codes.append(RefusalCode.APPROVAL_EXPIRED.value)
        return DispatchResult(
            "refused", intent, RefusalCode.APPROVAL_EXPIRED,
            f"payment intent expired at {intent.expires_at} "
            f"(ttl {intent.ttl_s}s); a stale authorization is never "
            "dispatched")

    # ---- F-T2: broadcast attempts + reconciliation -------------------
    def _register_attempt(self, intent: PaymentIntent,
                          signed_artifact: dict) -> dict:
        """Record a broadcast attempt the moment its signed artifact
        exists - BEFORE submit. The tx hash is determined by the
        signature, so it is known even if the submit response is
        never seen; that is what makes reconciliation possible."""
        record = {"intent_id": intent.intent_id,
                  "wallet_id": intent.wallet_id,
                  "idempotency_key": intent.idempotency_key,
                  "tx_hash": signed_artifact.get("tx_hash"),
                  "chain_nonce": signed_artifact.get("nonce"),
                  "from_address": signed_artifact.get("from"),
                  "desk_nonce": intent.desk_nonce}
        intent.submission = {"tx_hash": record["tx_hash"],
                             "chain_nonce": record["chain_nonce"],
                             "from_address": record["from_address"]}
        self._attempts_by_intent[intent.intent_id] = record
        if intent.idempotency_key:
            self._attempts_by_idem[(intent.wallet_id,
                                    intent.idempotency_key)] = record
        return record

    def _prior_attempt(self, intent: PaymentIntent) -> dict | None:
        record = self._attempts_by_intent.get(intent.intent_id)
        if record is None and intent.idempotency_key:
            record = self._attempts_by_idem.get(
                (intent.wallet_id, intent.idempotency_key))
        return record

    def _reconcile(self, adapter, *, from_address, tx_hash,
                   chain_nonce) -> tuple[str, dict]:
        """Read the chain back for an ambiguous submission. Returns
        (conclusion, evidence) where conclusion is:
        - "landed": the tx is on chain (receipt or tx object found);
        - "absent": PROVEN non-inclusion - the hash is unknown to the
          chain AND the sender's mined nonce never advanced past the
          submission's nonce within the confirmation window;
        - "inconclusive": neither proven (e.g. the nonce slot was
          consumed but the tx cannot be located) - the caller must
          NOT treat this as a refusal and must NOT broadcast again.
        Transport errors during reconciliation are counted, never
        treated as absence: a dead RPC proves nothing."""
        started = self._clock()
        polls = 0
        transport_errors = 0
        last_sender_nonce = None
        found_by = None
        while True:
            polls += 1
            try:
                if adapter.get_receipt(tx_hash) is not None:
                    found_by = "receipt"
                elif adapter.get_transaction(tx_hash) is not None:
                    found_by = "tx"
            except Exception:
                transport_errors += 1
            if found_by is not None:
                conclusion = "landed"
                break
            try:
                last_sender_nonce = adapter.chain_nonce_latest(
                    from_address)
            except Exception:
                transport_errors += 1
            if self._clock() - started >= self.reconcile_window_s:
                if last_sender_nonce is not None \
                        and chain_nonce is not None \
                        and last_sender_nonce <= chain_nonce:
                    conclusion = "absent"
                else:
                    conclusion = "inconclusive"
                break
            self._sleep(self.reconcile_poll_s)
        evidence = {"tx_hash": tx_hash, "chain_nonce": chain_nonce,
                    "from_address": from_address, "polls": polls,
                    "window_s": self.reconcile_window_s,
                    "transport_errors": transport_errors,
                    "last_sender_nonce": last_sender_nonce,
                    "found_by": found_by, "conclusion": conclusion,
                    "reconciled_at": int(self._clock())}
        return conclusion, evidence

    def _actual_fee(self, adapter, tx_ref) -> dict | None:
        """F-T3: the actual on-chain fee for receipts, when the
        adapter can read it. A fee read must never fail a payment
        whose finality is already proven - failures yield None (the
        receipt then carries the quote only, as before)."""
        if not getattr(adapter, "chain_backed", False) \
                or not hasattr(adapter, "actual_fee"):
            return None
        try:
            return adapter.actual_fee(tx_ref)
        except Exception:
            return None

    def _finish_submission(self, intent: PaymentIntent, adapter, row,
                           verdict, tx_ref: str) -> DispatchResult:
        """The post-submit tail shared by the normal path, the F-T2
        reconciled-landing path, and the retry-guard adoption path:
        status read -> exactly-one-receipt contract -> spend recorded
        exactly once at FINAL."""
        intent.tx_ref = tx_ref
        intent.transition(State.SUBMITTED, f"submitted: {tx_ref}")
        status = adapter.getStatus(tx_ref) if hasattr(adapter, "getStatus") \
            else "FINAL"
        if status in ("SUBMITTED", "UNKNOWN"):
            # No receipt yet: provisional belongs to CONFIRMED, FINAL
            # to FINAL. Timeouts/uncertainty route to UNKNOWN in the
            # watcher - never silently FINAL, never FAILED.
            if status == "UNKNOWN":
                intent.transition(State.UNKNOWN,
                                  "submission outcome unobserved")
            return DispatchResult(
                "submitted", intent, None,
                f"submitted ({status}); no receipt until CONFIRMED", None)
        if status == "FAILED_TERMINAL":
            return DispatchResult(
                "failed_terminal", intent, None,
                "on-chain terminal failure (tx reverted); intent stays "
                "SUBMITTED for audit, no receipt emitted", None)
        # Receipt contract (C-2 / defect D-3): exactly ONE receipt per
        # state transition. A dispatch reaching FINAL in one step emits
        # exactly one receipt (FINAL, provisional=false) - B0 previously
        # emitted a duplicate FINAL pair that a receipt-counting
        # consumer would read as two payments. A dispatch reaching only
        # CONFIRMED emits one provisional receipt; the FINAL receipt
        # belongs to the later transition that actually reaches FINAL.
        receipt = build_receipt(
            payment_id=intent.intent_id, intent_id=intent.intent_id,
            wallet_id=intent.wallet_id, asset_row=row,
            amount_base=intent.amount_base, fee_quote=verdict.fee_quote,
            fee_actual=self._actual_fee(adapter, tx_ref),
            verdict=verdict, policy=self.policy, tx_ref=tx_ref,
            finality_state="FINAL" if status == "FINAL" else "CONFIRMED",
            desk_auth_nonce=intent.desk_nonce,
            mandate_id=intent.mandate_jti,
            provisional=(status != "FINAL"))
        intent.receipts.append(receipt)
        if status == "FINAL":
            intent.transition(State.CONFIRMED, "finality reached")
            intent.transition(State.FINAL, "final")
            self._record_spend(intent, verdict)
            return DispatchResult("executed", intent, None,
                                  "executed and FINAL", receipt)
        intent.transition(State.CONFIRMED, "confirmed, not yet final")
        return DispatchResult("executed", intent, None,
                              "submitted; provisional receipt issued",
                              receipt)

    def authorize(self, intent: PaymentIntent) -> DispatchResult:
        """Intent-time check. allow -> AUTHORIZED; approve -> stays QUEUED
        until the owner grants approval; deny -> stays QUEUED with code."""
        if intent.state != State.QUEUED:
            raise TransitionError("authorize requires a QUEUED intent")
        expired = self._expiry_refusal(intent)
        if expired is not None:
            return expired
        verdict = self._evaluate(intent)
        if verdict.decision == "deny":
            return self._refuse(intent, verdict.code, verdict.reason)
        if verdict.decision == "approve" and not intent.approval_granted:
            return DispatchResult("approval_required", intent, None,
                                  verdict.reason)
        intent.transition(State.AUTHORIZED, "policy check passed")
        return DispatchResult("authorized", intent, None, verdict.reason)

    def grant_approval(self, intent: PaymentIntent) -> DispatchResult:
        intent.approval_granted = True
        return self.authorize(intent)

    def dispatch(self, intent: PaymentIntent) -> DispatchResult:
        """Execution-time FULL re-check, then nonce consumption exactly
        once, then build -> sign-step -> submit -> confirm -> final.

        Signing is out of B0 scope: for the sandbox adapter the artifact
        is applied directly; for EVM the build artifact is as far as B0
        goes and submission is a typed NOT_IMPLEMENTED refusal that
        returns the intent to QUEUED without consuming the nonce...
        except the nonce point: build happens AFTER nonce consumption in
        the real flow, so for a family whose submit is not implemented
        the dispatcher refuses BEFORE consuming the nonce.
        """
        if intent.state == State.QUEUED:
            auth = self.authorize(intent)
            if auth.status != "authorized":
                return auth
        if intent.state != State.AUTHORIZED:
            raise TransitionError("dispatch requires AUTHORIZED (or QUEUED, "
                                  "which is authorized first)")
        expired = self._expiry_refusal(intent)
        if expired is not None:
            return expired
        row = self.registry.require(intent.asset_id)
        adapter = self.adapters.get(row["family"])
        verdict = self._evaluate(intent)
        if verdict.decision == "deny":
            return self._refuse(intent, verdict.code, verdict.reason)
        if verdict.decision == "approve" and not intent.approval_granted:
            if State.QUEUED in LEGAL_TRANSITIONS[intent.state]:
                intent.transition(State.QUEUED, "dispatch re-check: approval "
                                                "required")
            return DispatchResult("approval_required", intent, None,
                                  verdict.reason)
        if not self._verify_auth(intent):
            return self._refuse(intent, RefusalCode.DESK_AUTH_INVALID,
                                "desk authorization missing or invalid")
        # F-1: mandate status is enforced HERE — after the policy and
        # desk-auth re-checks, before the nonce is consumed and before
        # the fund signer ever sees the intent.
        mandate_refusal = self._check_mandate(intent)
        if mandate_refusal is not None:
            return mandate_refusal
        if intent.desk_nonce is None:
            intent.desk_nonce = self.nonces.peek(intent.wallet_id)
        # ---- F-T2 retry guard: a prior broadcast attempt for this
        # intent (or for this wallet + idempotency key) is reconciled
        # BEFORE anything new is signed or broadcast. A retry can
        # never produce a second broadcast: a landed prior submission
        # is adopted (same intent) or refused as a duplicate (twin
        # intent), an inconclusive one parks on the recorded hash,
        # and only a PROVEN-absent prior attempt falls through to a
        # fresh broadcast under the original authorization.
        prior = self._prior_attempt(intent)
        prior_own = prior is not None \
            and prior["intent_id"] == intent.intent_id
        if prior is not None and getattr(adapter, "chain_backed", False):
            conclusion, evidence = self._reconcile(
                adapter, from_address=prior["from_address"],
                tx_hash=prior["tx_hash"],
                chain_nonce=prior["chain_nonce"])
            evidence = {**evidence, "phase": "retry-guard"}
            intent.reconciliation.append(evidence)
            if conclusion == "landed":
                if not prior_own:
                    return self._refuse(
                        intent, RefusalCode.DUPLICATE_SUBMISSION,
                        "an earlier submission for this wallet + "
                        f"idempotency key already landed on chain "
                        f"(tx {prior['tx_hash']}); refusing to "
                        "broadcast a duplicate", evidence)
                intent.artifact = {"tx_hash": prior["tx_hash"],
                                   "chain_nonce": prior["chain_nonce"],
                                   "reconciled": True}
                intent.transition(State.BUILT,
                                  "retry: prior submission adopted")
                intent.transition(State.SIGNED,
                                  "retry: prior submission adopted")
                self._last_verdict[intent.intent_id] = verdict
                result = self._finish_submission(
                    intent, adapter, row, verdict, prior["tx_hash"])
                result.evidence = evidence
                return result
            if conclusion == "inconclusive":
                if not prior_own:
                    return self._refuse(
                        intent, RefusalCode.DUPLICATE_SUBMISSION,
                        "an earlier submission for this wallet + "
                        "idempotency key could not be proven absent "
                        f"(tx {prior['tx_hash']}); refusing to "
                        "broadcast while its outcome is unresolved",
                        evidence)
                intent.artifact = {"tx_hash": prior["tx_hash"],
                                   "chain_nonce": prior["chain_nonce"],
                                   "reconciled": True}
                intent.tx_ref = prior["tx_hash"]
                intent.transition(State.BUILT,
                                  "retry: prior submission unresolved")
                intent.transition(State.SIGNED,
                                  "retry: prior submission unresolved")
                intent.transition(State.SUBMITTED,
                                  f"submitted (unresolved): "
                                  f"{prior['tx_hash']}")
                intent.transition(State.UNKNOWN,
                                  "reconciliation inconclusive - "
                                  "parked on the recorded hash, never "
                                  "re-broadcast")
                return DispatchResult(
                    "submitted", intent, None,
                    "prior submission outcome unresolved; intent "
                    "parked UNKNOWN on its recorded tx hash - "
                    "resolve via watch_finality, never re-broadcast",
                    None, evidence)
            # conclusion == "absent": the prior submission provably
            # never landed - fall through to ONE fresh broadcast. For
            # this intent's own retry the desk nonce was consumed by
            # the earlier attempt; the consume step below recognizes
            # that and does not consume it twice.
        # A family that cannot submit in B0 must refuse BEFORE the nonce
        # is consumed - a check error never burns a nonce.
        if not getattr(adapter, "submit_supported", False):
            return self._refuse(intent, RefusalCode.NOT_IMPLEMENTED,
                                f"{row['family']} adapter has no submit path "
                                "in B0 scope")
        try:
            artifact_preview = adapter.buildPayment(intent.__dict__ | {
                "from_address": intent.from_address})
        except Exception as e:  # adapter build errors are check errors
            code = getattr(e, "code", "CHAIN_UNHEALTHY")
            try:
                code = RefusalCode(code)
            except ValueError:
                code = RefusalCode.CHAIN_UNHEALTHY
            return self._refuse(intent, code, str(e))
        from families.base import AdapterNotImplemented
        # ---- chain-backed path (B1 testnet): the dispatcher presents
        # the desk-auth v2 authorization to the fund signer BEFORE the
        # desk nonce is consumed. Any signer refusal is a check error:
        # the intent returns to QUEUED and the nonce is never burned.
        signed_artifact = None
        if getattr(adapter, "chain_backed", False):
            if intent.desk_auth_signature is None or \
                    intent.desk_public_key is None:
                return self._refuse(
                    intent, RefusalCode.DESK_AUTH_INVALID,
                    "chain-backed dispatch requires the desk-auth v2 "
                    "signature + desk public key to present to the "
                    "fund signer")
            try:
                finalized = adapter.finalize_unsigned(artifact_preview)
                authorized_intent = {
                    "wallet_id": intent.wallet_id,
                    "asset_id": intent.asset_id,
                    "chain_id": intent.chain_id, "to": intent.to,
                    "amount_base": intent.amount_base,
                    "memo": intent.memo, "desk_nonce": intent.desk_nonce,
                    "idempotency_key": intent.idempotency_key,
                    "desk_auth_signature": intent.desk_auth_signature}
                signed_artifact = adapter.sign(
                    finalized, authorized_intent=authorized_intent,
                    desk_public_key=intent.desk_public_key,
                    fee_quote_max_base=verdict.fee_quote.amount_base
                    if verdict.fee_quote is not None else None)
            except Exception as e:
                code = getattr(e, "code", "CHAIN_UNHEALTHY")
                try:
                    code = RefusalCode(code)
                except ValueError:
                    code = RefusalCode.CHAIN_UNHEALTHY
                return self._refuse(intent, code, str(e))
            # F-T2: register the attempt NOW - the signed artifact
            # determines the tx hash, so the submission is reconcilable
            # from this moment, whatever happens to the submit call.
            self._register_attempt(intent, signed_artifact)
        # ---- every check passed: consume the nonce exactly once. For
        # this intent's own retry after a PROVEN-absent submission the
        # nonce was already consumed by that earlier attempt of THIS
        # intent; it is not consumed twice (F-T2).
        nonce_already_held = prior_own and prior is not None \
            and prior.get("desk_nonce") == intent.desk_nonce \
            and self.nonces.is_consumed(intent.wallet_id,
                                        intent.desk_nonce)
        if not nonce_already_held:
            try:
                self.nonces.consume(intent.wallet_id, intent.desk_nonce)
            except NonceError as e:
                return self._refuse(intent, RefusalCode.DESK_AUTH_NONCE,
                                    str(e))
        intent.artifact = signed_artifact or artifact_preview
        intent.transition(State.BUILT, "unsigned artifact built")
        intent.transition(State.SIGNED,
                            "fund-signer signature obtained (testnet)"
                            if signed_artifact else
                            "B0 sandbox: no chain signature; "
                            "desk-auth is the authorization")
        self._last_verdict[intent.intent_id] = verdict
        try:
            tx_ref = adapter.submit(intent.artifact)
        except AdapterNotImplemented as e:
            # Should be unreachable for sandbox; for safety the intent
            # returns to QUEUED. The nonce stays consumed only if submit
            # actually ran - sandbox submit is atomic, EVM never reaches
            # here because its build path is exercised in tests directly.
            return self._refuse(intent, RefusalCode.NOT_IMPLEMENTED, str(e))
        except Exception as e:
            # F-T2: a failed submit call is AMBIGUOUS on a chain - the
            # response may have been lost after the tx landed. Never
            # refuse on the transport error alone: reconcile first.
            if getattr(adapter, "chain_backed", False) \
                    and intent.submission \
                    and intent.submission.get("tx_hash"):
                conclusion, evidence = self._reconcile(
                    adapter,
                    from_address=intent.submission["from_address"],
                    tx_hash=intent.submission["tx_hash"],
                    chain_nonce=intent.submission["chain_nonce"])
                evidence = {**evidence, "phase": "submit",
                            "submit_error": str(e)}
                intent.reconciliation.append(evidence)
                if conclusion == "landed":
                    intent.history.append(
                        (State.SIGNED.value, int(self._clock()),
                         "submit response lost; reconciled by tx "
                         "hash: the submission landed"))
                    result = self._finish_submission(
                        intent, adapter, row, verdict,
                        intent.submission["tx_hash"])
                    result.evidence = evidence
                    return result
                if conclusion == "inconclusive":
                    tx_hash = intent.submission["tx_hash"]
                    intent.tx_ref = tx_hash
                    intent.transition(State.SUBMITTED,
                                      f"submitted (unresolved): "
                                      f"{tx_hash}")
                    intent.transition(State.UNKNOWN,
                                      "reconciliation inconclusive - "
                                      "parked on the recorded hash, "
                                      "never re-broadcast")
                    return DispatchResult(
                        "submitted", intent, None,
                        "submission outcome unresolved after "
                        "reconciliation (sender nonce consumed, tx "
                        "not locatable); intent parked UNKNOWN on "
                        "its recorded tx hash - resolve via "
                        "watch_finality, never re-broadcast",
                        None, evidence)
                # conclusion == "absent": non-inclusion is PROVEN, so
                # the refusal stands - carrying the evidence.
                intent.transition(State.QUEUED,
                                  f"submit failed; reconciliation "
                                  f"proved non-inclusion: {e}")
                code = getattr(e, "code", "CHAIN_UNHEALTHY")
                try:
                    code = RefusalCode(code)
                except ValueError:
                    code = RefusalCode.CHAIN_UNHEALTHY
                intent.refusal_codes.append(code.value)
                return DispatchResult(
                    "refused", intent, code,
                    f"{e} [reconciliation: tx "
                    f"{intent.submission['tx_hash']} absent on chain; "
                    f"sender nonce {evidence['last_sender_nonce']} "
                    f"unconsumed after {evidence['polls']} polls over "
                    f"the {evidence['window_s']}s confirmation "
                    "window]", None, evidence)
            intent.transition(State.QUEUED, f"submit failed: {e}")
            code = getattr(e, "code", "CHAIN_UNHEALTHY")
            try:
                code = RefusalCode(code)
            except ValueError:
                code = RefusalCode.CHAIN_UNHEALTHY
            intent.refusal_codes.append(code.value)
            return DispatchResult("refused", intent, code, str(e))
        return self._finish_submission(intent, adapter, row, verdict,
                                       tx_ref)

    # ---- B1 testnet watcher integration --------------------------------
    def _record_spend(self, intent: PaymentIntent, verdict) -> None:
        if intent.intent_id in self._tracker_recorded:
            return
        self._tracker_recorded.add(intent.intent_id)
        self.tracker.record(intent.wallet_id, intent.asset_id,
                            intent.amount_base,
                            verdict.usd_value_micro if verdict else None,
                            verdict.peg_value_micro if verdict else None)

    def watch_finality(self, intent: PaymentIntent, *, timeout_s: int = 180,
                       poll_interval_s: int = 2, sleep=None) -> dict:
        """Drive a SUBMITTED/CONFIRMED/UNKNOWN intent to FINAL (or
        UNKNOWN on timeout) via the FinalityWatcher, emitting the
        provisional receipt at CONFIRMED and exactly one FINAL receipt
        at FINAL through the receipt contract."""
        from watcher import FinalityWatcher
        row = self.registry.require(intent.asset_id)
        adapter = self.adapters[row["family"]]
        verdict = self._last_verdict.get(intent.intent_id)

        def _factory(it, finality_state, provisional):
            receipt = build_receipt(
                payment_id=it.intent_id, intent_id=it.intent_id,
                wallet_id=it.wallet_id, asset_row=row,
                amount_base=it.amount_base,
                fee_quote=verdict.fee_quote if verdict else None,
                fee_actual=self._actual_fee(adapter, it.tx_ref),
                verdict=verdict, policy=self.policy, tx_ref=it.tx_ref,
                finality_state=finality_state,
                desk_auth_nonce=it.desk_nonce,
                mandate_id=it.mandate_jti, provisional=provisional)
            it.receipts.append(receipt)
            if finality_state == "FINAL":
                self._record_spend(it, verdict)
            return receipt

        watcher = FinalityWatcher(adapter, timeout_s=timeout_s,
                                  poll_interval_s=poll_interval_s,
                                  clock=self._clock, sleep=sleep)
        return watcher.watch(intent, _factory)
