"""FamilyAdapter interface (ARCHITECTURE.md S3.1) + shared value types.

LOCAL/SANDBOX ONLY. Amounts are integer base units. Balance reads
distinguish spendable / frozen (issuer-frozen, never spendable) / absent.
Anchor-pattern methods exist on the interface and are stubbed as typed
NOT_IMPLEMENTED where they are out of B0 scope.
"""
from __future__ import annotations

from dataclasses import dataclass

BALANCE_STATES = ("spendable", "frozen", "absent")


class AdapterNotImplemented(Exception):
    code = "NOT_IMPLEMENTED"

    def __init__(self, method: str):
        super().__init__(f"{method} is not implemented in B0 scope")
        self.method = method


class AdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Balance:
    amount_base: int
    state: str  # spendable | frozen | absent

    def __post_init__(self):
        if isinstance(self.amount_base, bool) or not isinstance(self.amount_base, int):
            raise AdapterError("AMOUNT_TYPE_INVALID", "balance must be int base units")
        if self.amount_base < 0:
            raise AdapterError("AMOUNT_NEGATIVE", "balance must not be negative")
        if self.state not in BALANCE_STATES:
            raise AdapterError("BALANCE_STATE_INVALID", f"bad balance state {self.state!r}")
        if self.state == "absent" and self.amount_base != 0:
            raise AdapterError("BALANCE_STATE_INVALID", "absent balance must be zero")


@dataclass(frozen=True)
class FeeQuote:
    amount_base: int
    fee_asset_id: str
    quoted_at: int
    expires_at: int

    @property
    def ttl_s(self) -> int:
        return self.expires_at - self.quoted_at

    def is_expired(self, at: int) -> bool:
        return at > self.expires_at


class SandboxLedger:
    """In-memory sandbox balance book. Keys: (address, asset_id).

    This is the only 'chain' B0 adapters touch. It lives in memory or a
    temp dir in tests; it is never the live ledger.json.
    """

    def __init__(self):
        self._balances: dict[tuple[str, str], list] = {}

    @staticmethod
    def _check_amount(amount):
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise AdapterError("AMOUNT_TYPE_INVALID", "amount must be int base units")
        if amount < 0:
            raise AdapterError("AMOUNT_NEGATIVE", "amount must not be negative")

    def credit(self, address: str, asset_id: str, amount: int, frozen=False):
        self._check_amount(amount)
        entry = self._balances.setdefault((address, asset_id), [0, bool(frozen)])
        entry[0] += amount
        entry[1] = bool(frozen)

    def set_frozen(self, address: str, asset_id: str, frozen: bool):
        entry = self._balances.setdefault((address, asset_id), [0, False])
        entry[1] = bool(frozen)

    def debit(self, address: str, asset_id: str, amount: int):
        self._check_amount(amount)
        bal = self.read(address, asset_id)
        if bal.state != "spendable" or bal.amount_base < amount:
            raise AdapterError("INSUFFICIENT_BALANCE",
                               "debit exceeds spendable sandbox balance")
        self._balances[(address, asset_id)][0] -= amount

    def read(self, address: str, asset_id: str) -> Balance:
        entry = self._balances.get((address, asset_id))
        if entry is None:
            return Balance(0, "absent")
        amount, frozen = entry
        return Balance(amount, "frozen" if frozen else "spendable")


class FamilyAdapter:
    """One adapter per chain family per chain. Signing is NOT part of the
    adapter: adapters build unsigned artifacts and read state only."""

    family_id: str = ""
    chain_id: str = ""
    submit_supported: bool = False

    def chainId(self) -> str:  # Anchor-pattern name, kept deliberately
        return self.chain_id

    def validateAddress(self, address, asset_id=None) -> bool:
        raise NotImplementedError

    def buildPayment(self, intent: dict) -> dict:
        raise NotImplementedError

    def readBalance(self, address: str, asset_id: str) -> Balance:
        raise NotImplementedError

    def quoteFee(self, intent: dict) -> FeeQuote:
        raise NotImplementedError

    def finalityPolicy(self) -> dict:
        raise NotImplementedError

    def health(self) -> dict:
        raise NotImplementedError

    # Anchor-pattern methods: out of B0 wallet scope, typed stubs.
    def submitAnchor(self, data):
        raise AdapterNotImplemented("submitAnchor")

    def getConfirmation(self, tx_ref):
        raise AdapterNotImplemented("getConfirmation")

    def fetchAnchorData(self, tx_ref):
        raise AdapterNotImplemented("fetchAnchorData")

    def estimateAnchorCost(self, data):
        raise AdapterNotImplemented("estimateAnchorCost")
