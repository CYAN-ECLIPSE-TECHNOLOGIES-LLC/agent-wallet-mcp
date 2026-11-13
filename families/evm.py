# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""EvmAdapter: B0 sandbox-ledger mode + B1 chain-backed TESTNET mode.

B0 mode (default): no RPC, no network, no signing; EIP-55 validation
and unsigned payment-intent building only; submit() is a typed
NOT_IMPLEMENTED stub.

B1 chain-backed mode (chain_backed=True): ONLY eip155:84532
(Base Sepolia). Mainnet (and any other) chain ids are REFUSED at
adapter construction - this build is testnet-only by construction.
The RPC endpoint comes from config/env (never hard-coded here), the
adapter broadcasts only artifacts signed by the fund signer (a
separate key domain, fund_signer.py), and getStatus() reads
confirmations from the chain for the watcher/finality layer.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from .base import (AdapterError, AdapterNotImplemented, Balance,
                   FamilyAdapter, FeeQuote, SandboxLedger,
                   require_artifact_fields, require_intent_fields)

TESTNET_CHAIN_IDS = frozenset({"eip155:84532"})
CONFIRMED_AT_CONFIRMATIONS = 1
FINAL_AT_CONFIRMATIONS = 6


class JsonRpcClient:
    """Minimal JSON-RPC client over stdlib urllib. The transport is
    injectable for tests; the endpoint URL is supplied by config/env
    at construction, never hard-coded in the adapter."""

    def __init__(self, url: str, timeout_s: int = 20, transport=None,
                 retries: int = 2):
        if not url or not isinstance(url, str):
            raise AdapterError("RPC_NOT_CONFIGURED",
                               "chain-backed mode requires an RPC endpoint "
                               "from config/env")
        self.url = url
        self.timeout_s = timeout_s
        self._transport = transport
        self.retries = retries

    def _default_transport(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "agent-wallet-testnet/0.3"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.load(resp)

    def call(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": params}
        last = None
        for _ in range(self.retries + 1):
            try:
                transport = self._transport or self._default_transport
                out = transport(payload)
                if isinstance(out, dict) and "error" in out:
                    raise AdapterError(
                        "CHAIN_UNHEALTHY",
                        f"RPC {method} error: {out['error']}")
                return out["result"]
            except AdapterError:
                raise
            except Exception as e:  # transport failure = chain unhealthy
                last = e
        raise AdapterError("CHAIN_UNHEALTHY",
                           f"RPC {method} unreachable: {last}")


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

# OP-Stack GasPriceOracle predeploy: getL1Fee(bytes) prices the L1 data
# fee of a serialized transaction. F-T3: the fee quote on this chain
# class must include that component - a quote without it understated
# the true debit by up to ~3.1x on the bench's live samples.
GAS_PRICE_ORACLE_ADDRESS = "0x420000000000000000000000000000000000000F"
_GET_L1_FEE_SELECTOR = keccak256(b"getL1Fee(bytes)")[:4]


def _rlp_encode(item) -> bytes:
    """Minimal RLP encoding (ints, bytes, lists) - enough to serialize
    a representative EIP-1559 payload for L1-fee estimation."""
    if isinstance(item, int):
        if item == 0:
            return _rlp_encode(b"")
        return _rlp_encode(item.to_bytes((item.bit_length() + 7) // 8,
                                         "big"))
    if isinstance(item, (bytes, bytearray)):
        raw = bytes(item)
        if len(raw) == 1 and raw[0] < 0x80:
            return raw
        if len(raw) <= 55:
            return bytes([0x80 + len(raw)]) + raw
        len_bytes = len(raw).to_bytes((len(raw).bit_length() + 7) // 8,
                                      "big")
        return bytes([0xB7 + len(len_bytes)]) + len_bytes + raw
    if isinstance(item, (list, tuple)):
        payload = b"".join(_rlp_encode(x) for x in item)
        if len(payload) <= 55:
            return bytes([0xC0 + len(payload)]) + payload
        len_bytes = len(payload).to_bytes(
            (len(payload).bit_length() + 7) // 8, "big")
        return bytes([0xF7 + len(len_bytes)]) + len_bytes + payload
    raise TypeError(f"cannot RLP-encode {type(item).__name__}")


def _serialize_tx_for_l1_estimate(*, chain_id_num: int, nonce: int,
                                  max_priority_fee_per_gas: int,
                                  max_fee_per_gas: int, gas: int,
                                  to: str, value: int,
                                  data: bytes) -> bytes:
    """Length-faithful serialization of a type-2 transaction for the
    GasPriceOracle. The signature slots carry fixed 32-byte stand-ins:
    getL1Fee prices byte length + zero/non-zero mix, so the estimate is
    faithful (and, using non-zero stand-ins, marginally conservative).
    This serialization is ONLY an estimation input - it is never
    signed or broadcast."""
    fields = [chain_id_num, nonce, max_priority_fee_per_gas,
              max_fee_per_gas, gas, bytes.fromhex(to[2:]), value,
              bytes(data), [], 0, b"\x11" * 32, b"\x11" * 32]
    return b"\x02" + _rlp_encode(fields)


class EvmAdapter(FamilyAdapter):
    family_id = "evm"

    def __init__(self, registry, chain_id: str,
                 ledger: SandboxLedger | None = None, clock=None,
                 gas_price_base: int = 1, quote_ttl_s: int = 30,
                 chain_backed: bool = False, rpc_url: str | None = None,
                 rpc_client: JsonRpcClient | None = None,
                 fund_signer=None, from_address: str | None = None,
                 licence_gate=None):
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
        # ---- B1 chain-backed TESTNET mode -------------------------------
        self.chain_backed = bool(chain_backed)
        # Licence gate for the paid boundary (chain-backed dispatch).
        # None = no gate attached (sandbox + pre-product operator
        # posture); an attached gate is consulted at submit, before
        # any broadcast, and its typed refusal propagates.
        self.licence_gate = licence_gate
        self.fund_signer = fund_signer
        self.from_address = from_address or \
            (fund_signer.address if fund_signer is not None else None)
        self.rpc = None
        if self.chain_backed:
            if chain_id not in TESTNET_CHAIN_IDS:
                raise AdapterError(
                    "MAINNET_REFUSED",
                    f"chain-backed mode in this build is testnet-only "
                    f"({sorted(TESTNET_CHAIN_IDS)}); refusing chain_id "
                    f"{chain_id!r} at adapter construction")
            if rpc_client is not None:
                self.rpc = rpc_client
            else:
                url = rpc_url or os.environ.get(
                    "AGENT_WALLET_BASE_SEPOLIA_RPC")
                self.rpc = JsonRpcClient(url)  # raises if unset
            self.submit_supported = True

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
        if self.chain_backed:
            artifact["mode"] = "chain"
            artifact["from"] = intent.get("from_address") or self.from_address
            artifact["transfer_method"] = row["transfer_method"]
        return artifact

    # ---- chain-backed helpers (B1 testnet) ----------------------------
    def _call(self, method, params):
        if not self.chain_backed or self.rpc is None:
            raise AdapterNotImplemented("EvmAdapter chain call (B0 mode)")
        return self.rpc.call(method, params)

    def chain_nonce(self, address: str) -> int:
        return int(self._call("eth_getTransactionCount",
                              [address, "pending"]), 16)

    def chain_nonce_latest(self, address: str) -> int:
        """Next usable nonce counting MINED transactions only - the
        F-T2 reconciliation read: if this has not advanced past a
        submission's nonce, that submission provably did not land."""
        return int(self._call("eth_getTransactionCount",
                              [address, "latest"]), 16)

    def get_transaction(self, tx_hash: str) -> dict | None:
        """eth_getTransactionByHash - None when the chain has never
        seen this hash (F-T2 reconciliation read)."""
        if not self.chain_backed:
            raise AdapterNotImplemented("EvmAdapter.get_transaction "
                                        "(B0 mode)")
        return self._call("eth_getTransactionByHash", [tx_hash])

    def gas_price(self) -> int:
        return int(self._call("eth_gasPrice", []), 16)

    def estimate_l1_fee(self, *, nonce: int,
                        max_priority_fee_per_gas: int,
                        max_fee_per_gas: int, gas: int, to: str,
                        value: int, data: bytes) -> int:
        """L1 data-fee estimate from the OP-Stack GasPriceOracle for a
        transaction with these fields. FAIL-CLOSED (F-T3): any failure
        to obtain the estimate raises FEE_ESTIMATE_UNAVAILABLE - a fee
        quote on this chain class is never issued without its L1
        component, because an L2-only number is a known understatement.
        """
        payload = _serialize_tx_for_l1_estimate(
            chain_id_num=int(self.chain_id.split(":")[1]), nonce=nonce,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            max_fee_per_gas=max_fee_per_gas, gas=gas, to=to,
            value=value, data=data)
        pad = (32 - len(payload) % 32) % 32
        call_data = (_GET_L1_FEE_SELECTOR
                     + (32).to_bytes(32, "big")
                     + len(payload).to_bytes(32, "big")
                     + payload + b"\x00" * pad)
        try:
            result = self._call(
                "eth_call",
                [{"to": GAS_PRICE_ORACLE_ADDRESS,
                  "data": "0x" + call_data.hex()}, "latest"])
            if not isinstance(result, str) or not result.startswith("0x") \
                    or len(result) <= 2:
                raise ValueError(f"malformed getL1Fee result: {result!r}")
            return int(result, 16)
        except AdapterError as e:
            if e.code == "FEE_ESTIMATE_UNAVAILABLE":
                raise
            raise AdapterError(
                "FEE_ESTIMATE_UNAVAILABLE",
                f"L1 fee estimate unavailable ({e}); refusing to quote "
                "an L2-only fee on an OP-Stack chain") from e
        except Exception as e:
            raise AdapterError(
                "FEE_ESTIMATE_UNAVAILABLE",
                f"L1 fee estimate unavailable: {e}; refusing to quote "
                "an L2-only fee on an OP-Stack chain") from e

    def actual_fee(self, tx_ref: str) -> dict | None:
        """The fee ACTUALLY debited for a mined tx, to the wei (F-T3):
        L2 = gasUsed x effectiveGasPrice, plus the OP-Stack l1Fee the
        chain reports on the receipt. None while the tx is unmined or
        the receipt is unreadable - callers record actuals only when
        this returns a value."""
        receipt = self.get_receipt(tx_ref)
        if receipt is None:
            return None

        def _num(v):
            return int(v, 16) if isinstance(v, str) else int(v)

        try:
            l2 = _num(receipt["gasUsed"]) * \
                _num(receipt["effectiveGasPrice"])
            l1_raw = receipt.get("l1Fee")
            l1 = _num(l1_raw) if l1_raw is not None else 0
        except (KeyError, TypeError, ValueError):
            return None
        return {"amount_base": l2 + l1, "l2_amount_base": l2,
                "l1_fee_base": l1}

    def finalize_unsigned(self, artifact: dict) -> dict:
        """Fill chain fields (nonce, gas, EIP-1559 fees) on an unsigned
        artifact at dispatch time - never at queue time."""
        if not self.chain_backed:
            raise AdapterNotImplemented("finalize_unsigned (B0 mode)")
        out = dict(artifact)
        sender = out.get("from") or self.from_address
        if not sender:
            raise AdapterError("ARTIFACT_INVALID",
                               "chain artifact has no sender address")
        out["from"] = sender
        out["nonce"] = self.chain_nonce(sender)
        gas_units = 65000 if out.get("transfer_method") == "erc20" else 21000
        out["gas"] = gas_units
        price = self.gas_price()
        out["max_priority_fee_per_gas"] = price
        out["max_fee_per_gas"] = price * 2
        out["fee_cap_base"] = gas_units * out["max_fee_per_gas"]
        # F-T3: bind the L1 data-fee estimate for THIS exact artifact
        # into the artifact, so the fund signer can bound the
        # ESTIMATED TOTAL fee (L2 max + L1 estimate) before signing.
        # Estimation failure is fail-closed (FEE_ESTIMATE_UNAVAILABLE).
        data = out.get("data") or "0x"
        out["l1_fee_estimate_base"] = self.estimate_l1_fee(
            nonce=out["nonce"],
            max_priority_fee_per_gas=out["max_priority_fee_per_gas"],
            max_fee_per_gas=out["max_fee_per_gas"], gas=gas_units,
            to=out["to"], value=out.get("value_base") or 0,
            data=bytes.fromhex(data[2:]) if data.startswith("0x")
            and len(data) > 2 else b"")
        out["fee_estimate_total_base"] = (
            out["fee_cap_base"] + out["l1_fee_estimate_base"])
        return out

    def sign(self, artifact: dict, *, authorized_intent: dict,
             desk_public_key: bytes, fee_quote_max_base: int) -> dict:
        """Delegate signing to the fund signer (separate key domain).
        The dispatcher presents the desk-auth v2 authorization here;
        the signer refuses without a valid one."""
        if self.fund_signer is None:
            raise AdapterError("FUND_SIGNER_MISSING",
                               "chain-backed mode requires a fund signer")
        return self.fund_signer.sign_pay(
            artifact, authorized_intent=authorized_intent,
            desk_public_key=desk_public_key,
            fee_quote_max_base=fee_quote_max_base)

    def submit(self, artifact: dict) -> str:
        if not self.chain_backed:
            raise AdapterNotImplemented("EvmAdapter.submit (no chain in B0)")
        require_artifact_fields(artifact, ("signed", "raw_tx", "tx_hash",
                                           "chain_id"),
                                "EvmAdapter.submit")
        if artifact["signed"] is not True or not artifact["raw_tx"]:
            raise AdapterError("ARTIFACT_INVALID",
                               "refusing to broadcast an unsigned artifact")
        if artifact["chain_id"] != self.chain_id:
            raise AdapterError("ARTIFACT_INVALID",
                               "artifact chain does not match adapter chain")
        if self.licence_gate is not None:
            # Paid boundary: chain-backed dispatch. A typed
            # LicenceRefusal propagates; no RPC call has been made.
            self.licence_gate.require()
        tx_hash = self._call("eth_sendRawTransaction", [artifact["raw_tx"]])
        if tx_hash.lower() != str(artifact["tx_hash"]).lower():
            raise AdapterError("CHAIN_UNHEALTHY",
                               "RPC returned a tx hash that does not match "
                               "the signed artifact")
        return tx_hash

    def getStatus(self, tx_ref: str) -> str:
        if not self.chain_backed:
            raise AdapterNotImplemented("EvmAdapter.getStatus (no chain in B0)")
        receipt = self._call("eth_getTransactionReceipt", [tx_ref])
        if receipt is None:
            tx = self._call("eth_getTransactionByHash", [tx_ref])
            return "SUBMITTED" if tx is not None else "UNKNOWN"
        if receipt.get("status") == "0x0":
            return "FAILED_TERMINAL"
        head = int(self._call("eth_blockNumber", []), 16)
        block = int(receipt["blockNumber"], 16)
        confirmations = head - block + 1
        if confirmations >= FINAL_AT_CONFIRMATIONS:
            return "FINAL"
        if confirmations >= CONFIRMED_AT_CONFIRMATIONS:
            return "CONFIRMED"
        return "SUBMITTED"

    def get_receipt(self, tx_ref: str) -> dict | None:
        """Raw tx receipt (fee truth for the bench: gasUsed x
        effectiveGasPrice, to the wei)."""
        if not self.chain_backed:
            raise AdapterNotImplemented("EvmAdapter.get_receipt (B0 mode)")
        return self._call("eth_getTransactionReceipt", [tx_ref])

    def readBalance(self, address: str, asset_id: str) -> Balance:
        if self.chain_backed:
            row = self.registry.require(asset_id)
            if row["transfer_method"] == "erc20":
                data = "0x70a08231" + address[2:].lower().rjust(64, "0")
                out = self._call("eth_call",
                                 [{"to": row["contract_or_mint"],
                                   "data": data}, "latest"])
                amount = int(out, 16)
            else:
                amount = int(self._call("eth_getBalance",
                                        [address, "latest"]), 16)
            return Balance(amount, "spendable" if amount > 0 else "absent")
        return self.ledger.read(address, asset_id)

    def quoteFee(self, intent: dict) -> FeeQuote:
        require_intent_fields(intent, ("asset_id",),
                              "EvmAdapter.quoteFee")
        row = self.registry.require(intent["asset_id"])
        gas_units = 65000 if row["transfer_method"] == "erc20" else 21000
        at = self._clock()
        if self.chain_backed:
            # Live quote = ESTIMATED TOTAL fee (F-T3): the L2 leg
            # (current gas price x2 headroom, matching the EIP-1559
            # max fee the fund signer will be capped at) PLUS the L1
            # data-fee estimate from the GasPriceOracle for a
            # length-faithful representative transaction. The L1 leg is
            # fail-closed: no estimate, no quote.
            price = self.gas_price()
            l2_estimate = gas_units * price * 2
            amount = intent.get("amount_base")
            amount_ok = isinstance(amount, int) \
                and not isinstance(amount, bool) and amount > 0
            sender = intent.get("from_address") or self.from_address
            nonce = self.chain_nonce(sender) if sender else 0
            if row["transfer_method"] == "erc20":
                data = bytes.fromhex(_TRANSFER_SELECTOR) + bytes(32) \
                    + (amount if amount_ok else 0).to_bytes(32, "big")
                l1_estimate = self.estimate_l1_fee(
                    nonce=nonce, max_priority_fee_per_gas=price,
                    max_fee_per_gas=price * 2, gas=gas_units,
                    to=row["contract_or_mint"], value=0, data=data)
            else:
                l1_estimate = self.estimate_l1_fee(
                    nonce=nonce, max_priority_fee_per_gas=price,
                    max_fee_per_gas=price * 2, gas=gas_units,
                    to=intent.get("to") or
                    "0x0000000000000000000000000000000000000000",
                    value=amount if amount_ok else 0, data=b"")
            return FeeQuote(amount_base=l2_estimate + l1_estimate,
                            fee_asset_id=row["fee_asset_id"], quoted_at=at,
                            expires_at=at + self.quote_ttl_s,
                            l2_amount_base=l2_estimate,
                            l1_amount_base=l1_estimate)
        return FeeQuote(amount_base=gas_units * self.gas_price_base,
                        fee_asset_id=row["fee_asset_id"], quoted_at=at,
                        expires_at=at + self.quote_ttl_s)

    def finalityPolicy(self) -> dict:
        if self.chain_backed:
            return {"family": "evm", "chain_id": self.chain_id,
                    "confirmed_at": f"{CONFIRMED_AT_CONFIRMATIONS}-confirmation",
                    "final_at": f"{FINAL_AT_CONFIRMATIONS}-confirmations",
                    "confirmations_required": FINAL_AT_CONFIRMATIONS,
                    "confirmed_confirmations": CONFIRMED_AT_CONFIRMATIONS,
                    "finality": "probabilistic-testnet"}
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
        if self.chain_backed:
            try:
                cid = int(self._call("eth_chainId", []), 16)
                head = int(self._call("eth_blockNumber", []), 16)
                ok = cid == 84532 and self._healthy
                return {"ok": ok, "family": self.family_id,
                        "chain_id": self.chain_id, "head": head,
                        "finality_lag_s": 0}
            except Exception as e:
                return {"ok": False, "family": self.family_id,
                        "chain_id": self.chain_id, "head": None,
                        "finality_lag_s": None, "error": str(e)}
        return {"ok": self._healthy, "family": self.family_id,
                "chain_id": self.chain_id, "head": "sandbox-ledger",
                "finality_lag_s": 0}
