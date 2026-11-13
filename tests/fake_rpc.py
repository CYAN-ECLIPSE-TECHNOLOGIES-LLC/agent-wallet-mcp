# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""Fake JSON-RPC transport for Base Sepolia: deterministic chain state
for adapter/signer/dispatcher tests. No network, no real keys beyond
throwaway test accounts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from families.evm import GAS_PRICE_ORACLE_ADDRESS, keccak256  # noqa: E402

BALANCE_OF = "0x70a08231"


class FakeRpc:
    def __init__(self, gas_price=1_000_000, block=100, l1_fee=100_000_000_000,
                 receipt_l1_fee=900_000_000_000):
        self.gas_price = gas_price
        self.block = block
        # F-T3 chain model: the GasPriceOracle's getL1Fee answer for
        # estimates, and the l1Fee a mined receipt reports as actual.
        self.l1_fee = l1_fee
        self.receipt_l1_fee = receipt_l1_fee
        self.eth_balances = {}
        self.token_balances = {}
        self.nonces = {}
        self.txs = {}        # hash -> receipt block or None
        self.sent_raw = []

    def advance(self, blocks=1):
        self.block += blocks

    def __call__(self, payload):
        method, params = payload["method"], payload["params"]
        if method == "eth_chainId":
            return {"result": "0x14a34"}
        if method == "eth_blockNumber":
            return {"result": hex(self.block)}
        if method == "eth_gasPrice":
            return {"result": hex(self.gas_price)}
        if method == "eth_getTransactionCount":
            return {"result": hex(self.nonces.get(params[0].lower(), 0))}
        if method == "eth_getBalance":
            return {"result": hex(self.eth_balances.get(params[0].lower(), 0))}
        if method == "eth_call":
            call = params[0]
            if call.get("to") == GAS_PRICE_ORACLE_ADDRESS:
                return {"result": hex(self.l1_fee)}
            if call["data"].startswith(BALANCE_OF):
                addr = "0x" + call["data"][34:74]
                return {"result": hex(self.token_balances.get(addr.lower(), 0))}
            raise AssertionError(f"unexpected eth_call {call}")
        if method == "eth_sendRawTransaction":
            raw = params[0]
            self.sent_raw.append(raw)
            tx_hash = "0x" + keccak256(bytes.fromhex(raw[2:])).hex()
            self.txs[tx_hash] = self.block
            return {"result": tx_hash}
        if method == "eth_getTransactionReceipt":
            blk = self.txs.get(params[0])
            if blk is None:
                return {"result": None}
            return {"result": {"status": "0x1",
                                "blockNumber": hex(blk),
                                "gasUsed": hex(50000),
                                "effectiveGasPrice": hex(self.gas_price),
                                "l1Fee": hex(self.receipt_l1_fee),
                                "transactionHash": params[0]}}
        if method == "eth_getTransactionByHash":
            return {"result": {"hash": params[0]}
                    if params[0] in self.txs else None}
        raise AssertionError(f"unexpected RPC method {method}")
