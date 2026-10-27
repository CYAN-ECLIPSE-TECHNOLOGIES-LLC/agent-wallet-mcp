"""EvmAdapter for B0: operates against the sandbox ledger ONLY.

No RPC, no network imports, no chain transactions, no signing. It does
the chain-shaped work that can be proven locally: EIP-55 address
validation/normalization (pure-Python Keccak-256, stdlib only) and
unsigned payment-intent building (native value intent / ERC-20 transfer
calldata). Submission is a typed NOT_IMPLEMENTED stub in B0.
"""
from __future__ import annotations

import re
import time

from .base import (AdapterError, AdapterNotImplemented, Balance,
                   FamilyAdapter, FeeQuote, SandboxLedger,
                   require_intent_fields)

# ---------------------------------------------------------------- Keccak-256
# Pure-Python Keccak-256 (the pre-NIST padding variant Ethereum uses;
# hashlib.sha3_256 is a different function and must not be substituted).
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
       0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
       0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
       0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
       0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
       0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_MASK64 = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK64 if n else x


def _keccak_f1600(state: list) -> None:
    for rnd in range(24):
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^
             state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y],
                                                         _ROT[x][y])
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = (b[x + 5 * y] ^
                                    ((~b[(x + 1) % 5 + 5 * y]) &
                                     b[(x + 2) % 5 + 5 * y])) & _MASK64
        state[0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    rate = 136  # 1088-bit rate for a 256-bit output
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0x00)
    padded.append(0x80)
    state = [0] * 25
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f1600(state)
    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            out += state[i].to_bytes(8, "little")
        if len(out) >= 32:
            break
        _keccak_f1600(state)
    return bytes(out[:32])


# ------------------------------------------------------------------- EIP-55
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def to_checksum_address(address: str) -> str:
    if not isinstance(address, str) or not _ADDR_RE.match(address):
        raise AdapterError("ADDRESS_INVALID_FOR_FAMILY",
                           f"malformed EVM address: {address!r}")
    lower = address[2:].lower()
    digest = keccak256(lower.encode("ascii")).hex()
    out = ["0x"]
    for i, ch in enumerate(lower):
        out.append(ch.upper() if ch in "abcdef" and int(digest[i], 16) >= 8
                   else ch)
    return "".join(out)


def is_valid_evm_address(address) -> bool:
    """All-lower / all-upper hex is accepted (unnormalized); mixed-case
    input must carry a valid EIP-55 checksum."""
    if not isinstance(address, str) or not _ADDR_RE.match(address):
        return False
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True
    return to_checksum_address(address) == address


_TRANSFER_SELECTOR = "a9059cbb"  # ERC-20 transfer(address,uint256)


class EvmAdapter(FamilyAdapter):
    family_id = "evm"

    def __init__(self, registry, chain_id: str,
                 ledger: SandboxLedger | None = None, clock=None,
                 gas_price_base: int = 1, quote_ttl_s: int = 30):
        self.registry = registry
        self.chain_id = chain_id
        self.ledger = ledger if ledger is not None else SandboxLedger()
        self._clock = clock or (lambda: int(time.time()))
        if isinstance(gas_price_base, bool) or not isinstance(gas_price_base, int) \
                or gas_price_base < 0:
            raise AdapterError("AMOUNT_INVALID", "gas_price_base must be a "
                               "non-negative int")
        self.gas_price_base = gas_price_base
        self.quote_ttl_s = quote_ttl_s
        self._healthy = True

    def set_healthy(self, ok: bool):
        self._healthy = bool(ok)

    def validateAddress(self, address, asset_id=None) -> bool:
        return is_valid_evm_address(address)

    def normalizeAddress(self, address: str) -> str:
        return to_checksum_address(address)

    def buildPayment(self, intent: dict) -> dict:
        """Build an UNSIGNED payment intent artifact. Never signs, never
        submits, never touches a network."""
        require_intent_fields(intent, ("asset_id", "amount_base", "to",
                                       "intent_id"),
                              "EvmAdapter.buildPayment")
        row = self.registry.require(intent["asset_id"])
        if row["chain_id"] != self.chain_id or row["family"] != "evm":
            raise AdapterError("ASSET_NOT_ENABLED",
                               "asset row does not belong to this adapter")
        amount = intent["amount_base"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise AdapterError("AMOUNT_INVALID",
                               "amount_base must be a positive int")
        if not self.validateAddress(intent["to"]):
            raise AdapterError("ADDRESS_INVALID_FOR_FAMILY",
                               f"bad EVM address: {intent['to']!r}")
        to = to_checksum_address(intent["to"])
        if row["transfer_method"] == "erc20":
            if not row["contract_or_mint"]:
                raise AdapterError("ASSET_NOT_ENABLED",
                                   "token contract not populated in registry")
            data = ("0x" + _TRANSFER_SELECTOR +
                    to[2:].lower().rjust(64, "0") +
                    format(amount, "x").rjust(64, "0"))
            artifact = {"kind": "evm-unsigned-intent", "signed": False,
                        "chain_id": self.chain_id, "to": row["contract_or_mint"],
                        "value_base": 0, "data": data,
                        "payment_to": to, "asset_id": row["asset_id"],
                        "amount_base": amount,
                        "intent_id": intent["intent_id"]}
        elif row["transfer_method"] == "native-evm":
            artifact = {"kind": "evm-unsigned-intent", "signed": False,
                        "chain_id": self.chain_id, "to": to,
                        "value_base": amount, "data": "0x",
                        "payment_to": to, "asset_id": row["asset_id"],
                        "amount_base": amount,
                        "intent_id": intent["intent_id"]}
        else:
            raise AdapterError("ASSET_NOT_ENABLED",
                               f"transfer method {row['transfer_method']!r} "
                               "is not EVM-buildable")
        return artifact

    def submit(self, artifact: dict) -> str:
        raise AdapterNotImplemented("EvmAdapter.submit (no chain in B0)")

    def getStatus(self, tx_ref: str) -> str:
        raise AdapterNotImplemented("EvmAdapter.getStatus (no chain in B0)")

    def readBalance(self, address: str, asset_id: str) -> Balance:
        return self.ledger.read(address, asset_id)

    def quoteFee(self, intent: dict) -> FeeQuote:
        require_intent_fields(intent, ("asset_id",),
                              "EvmAdapter.quoteFee")
        row = self.registry.require(intent["asset_id"])
        gas_units = 65000 if row["transfer_method"] == "erc20" else 21000
        at = self._clock()
        return FeeQuote(amount_base=gas_units * self.gas_price_base,
                        fee_asset_id=row["fee_asset_id"], quoted_at=at,
                        expires_at=at + self.quote_ttl_s)

    def finalityPolicy(self) -> dict:
        if self.chain_id == "eip155:8453":
            return {"family": "evm", "chain_id": self.chain_id,
                    "confirmed_at": "sequencer-soft-confirm",
                    "final_at": "l1-batch-posted",
                    "confirmations_required": None,
                    "finality": "deterministic"}
        return {"family": "evm", "chain_id": self.chain_id,
                "confirmed_at": "1-block", "final_at": "~13min-2-epochs",
                "confirmations_required": None, "finality": "deterministic"}

    def health(self) -> dict:
        return {"ok": self._healthy, "family": self.family_id,
                "chain_id": self.chain_id, "head": "sandbox-ledger",
                "finality_lag_s": 0}
