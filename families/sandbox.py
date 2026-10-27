"""SandboxAdapter: executes against the in-memory SandboxLedger only.

LOCAL/SANDBOX ONLY. This is registry row #1's adapter - the only
spend-enabled row in B0. Finality is instant (FINAL in one step), exactly
as the v0 sandbox server behaved.
"""
from __future__ import annotations

import re
import time

from .base import (AdapterError, Balance, FamilyAdapter, FeeQuote,
                   SandboxLedger, require_artifact_fields,
                   require_intent_fields)

_ADDR_RE = re.compile(r"^(sandbox:[A-Za-z0-9_-]{3,}|0x[0-9a-fA-F]{40})$")


class SandboxAdapter(FamilyAdapter):
    family_id = "sandbox"
    chain_id = "sandbox:local"
    submit_supported = True

    def __init__(self, ledger: SandboxLedger | None = None, clock=None,
                 fee_ttl_s: int = 300):
        self.ledger = ledger if ledger is not None else SandboxLedger()
        self._clock = clock or (lambda: int(time.time()))
        self.fee_ttl_s = fee_ttl_s
        self._healthy = True

    def set_healthy(self, ok: bool):
        self._healthy = bool(ok)

    def validateAddress(self, address, asset_id=None) -> bool:
        return isinstance(address, str) and bool(_ADDR_RE.match(address))

    def buildPayment(self, intent: dict) -> dict:
        require_intent_fields(intent, ("intent_id", "asset_id",
                                       "from_address", "to",
                                       "amount_base"),
                              "SandboxAdapter.buildPayment")
        amount = intent["amount_base"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise AdapterError("AMOUNT_INVALID", "amount_base must be a positive int")
        if not self.validateAddress(intent["to"]):
            raise AdapterError("ADDRESS_INVALID_FOR_FAMILY",
                               f"bad sandbox address: {intent['to']!r}")
        return {"kind": "sandbox-payment-intent", "signed": False,
                "intent_id": intent["intent_id"], "chain_id": self.chain_id,
                "asset_id": intent["asset_id"], "from": intent["from_address"],
                "to": intent["to"], "amount_base": amount,
                "memo": intent.get("memo", "")}

    def submit(self, artifact: dict) -> str:
        """Apply the artifact to the sandbox ledger. Sandbox-only: there is
        no chain and no signature; policy is the gate, this is the book."""
        require_artifact_fields(artifact, ("from", "asset_id",
                                           "amount_base", "intent_id"),
                                "SandboxAdapter.submit")
        self.ledger.debit(artifact["from"], artifact["asset_id"],
                          artifact["amount_base"])
        self.ledger.credit(artifact["to"], artifact["asset_id"],
                           artifact["amount_base"])
        return f"sandbox-tx-{artifact['intent_id']}"

    def getStatus(self, tx_ref: str) -> str:
        return "FINAL"

    def readBalance(self, address: str, asset_id: str) -> Balance:
        return self.ledger.read(address, asset_id)

    def quoteFee(self, intent: dict) -> FeeQuote:
        at = self._clock()
        return FeeQuote(amount_base=0, fee_asset_id=intent["asset_id"],
                        quoted_at=at, expires_at=at + self.fee_ttl_s)

    def finalityPolicy(self) -> dict:
        return {"family": "sandbox", "confirmed_at": "instant",
                "final_at": "instant", "confirmations_required": 0,
                "finality": "instant-offchain"}

    def health(self) -> dict:
        return {"ok": self._healthy, "family": self.family_id,
                "chain_id": self.chain_id, "head": "sandbox",
                "finality_lag_s": 0}
