"""Desk spend-authorization verification (stdlib only).

Shared by server.py (verifies) and the desk signer (builds the same
canonical message to sign). Desk keys are secp256k1 (the house fund
domain, per Eclipse Anchor): a desk wallet address is a real EVM
address derived from the desk public key by Keccak-256, so it is
mint-native on any EVM chain. Signatures are EIP-191 personal-sign
over the canonical spend message; verification here is pure-Python
ECDSA over secp256k1 — no key material, no signing, stdlib only.
"""
import hashlib

# ---------------------------------------------------------------- spend msg
def spend_message(wallet_id: str, to: str, amount_micro: int, memo: str, nonce: int) -> bytes:
    """Canonical bytes a desk key signs to authorize one spend (v1).

    v1 remains the message of the certified v0.2.0 surface and its
    wallets are not migrated under it (silas-view S3.2); the B0/B1
    integrated server uses v2 below and accepts v1 as a fallback."""
    return (f"agent-wallet-spend/v1|{wallet_id}|{to}|{int(amount_micro)}"
            f"|{memo}|{int(nonce)}").encode()


def spend_message_v2(wallet_id: str, asset_id: str, chain_id: str,
                     to: str, amount_base: int, memo: str, nonce: int,
                     idempotency_key: str) -> bytes:
    """Canonical desk-authorization v2 bytes (silas-view S3.2). Same
    secp256k1 desk key, EIP-191 digest; the message now binds the
    authorization to the exact asset x chain and the amount in base
    units (v1 bound only a bare micro amount), plus an idempotency key
    so one signature authorizes exactly one logical payment:

        agent-wallet-spend/v2|{wallet_id}|{asset_id}|{chain_id}|{to}|
            {amount_base}|{memo}|{nonce}|{idempotency_key}
    """
    return (f"agent-wallet-spend/v2|{wallet_id}|{asset_id}|{chain_id}|{to}"
            f"|{int(amount_base)}|{memo}|{int(nonce)}"
            f"|{idempotency_key}").encode()


def verify_spend_v2(public_key: bytes, signature: bytes, *,
                    wallet_id: str, asset_id: str, chain_id: str,
                    to: str, amount_base, memo: str, nonce,
                    idempotency_key: str) -> bool:
    """True iff `signature` is a valid desk signature over the v2
    canonical message for exactly these intent fields. Never raises."""
    try:
        msg = spend_message_v2(wallet_id, asset_id, chain_id, to,
                               amount_base, memo, nonce, idempotency_key)
    except Exception:
        return False
    return ecdsa_verify(public_key, signature, spend_digest(msg))


def verify_spend_v1(public_key: bytes, signature: bytes, *,
                    wallet_id: str, to: str, amount_micro, memo: str,
                    nonce) -> bool:
    """True iff `signature` is valid over the v1 canonical message.
    Never raises."""
    try:
        msg = spend_message(wallet_id, to, amount_micro, memo, nonce)
    except Exception:
        return False
    return ecdsa_verify(public_key, signature, spend_digest(msg))


def spend_digest(message: bytes) -> bytes:
    """EIP-191 personal-sign digest of the canonical spend message."""
    prefix = b"\x19Ethereum Signed Message:\n" + str(len(message)).encode()
    return keccak256(prefix + message)


def desk_address(public_key_bytes: bytes) -> str:
    """EVM address for an uncompressed secp256k1 public key (64-byte X||Y)."""
    if len(public_key_bytes) != 64:
        raise ValueError("public key must be 64 bytes (X||Y, no prefix)")
    return "0x" + keccak256(public_key_bytes)[-20:].hex()


# ---------------------------------------------------------------- Keccak-256
# Original Keccak padding (0x01..0x80), rate 136 bytes — NOT NIST SHA3-256.
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_R = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_MASK64 = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK64 if n else x


def _keccak_f(state):
    for rc in _RC:
        # theta
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
             for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]
        # rho + pi
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y], _R[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = (b[x + 5 * y]
                                    ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])) & _MASK64
        # iota
        state[0] ^= rc
    return state


def keccak256(data: bytes) -> bytes:
    rate = 136
    # Keccak pad10*1: append 0x01, zero-fill to a whole number of rate
    # blocks, then set the top bit of the FINAL byte. When the message
    # ends exactly one byte short of a block boundary, both marks land
    # in the same byte (0x81) — Verity bench finding F-1, 2026-10-27.
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f(state)
    out = bytearray()
    for i in range(4):
        out += state[i].to_bytes(8, "little")
    return bytes(out[:32])


# ---------------------------------------------------------------- secp256k1
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _point_add(P, Q):
    if P is None:
        return Q
    if Q is None:
        return P
    x1, y1 = P
    x2, y2 = Q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if P == Q:
        m = (3 * x1 * x1) * pow(2 * y1, -1, _P) % _P
    else:
        m = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    return (x3, (m * (x1 - x3) - y1) % _P)


def _point_mul(k: int, P):
    R = None
    Q = P
    while k:
        if k & 1:
            R = _point_add(R, Q)
        Q = _point_add(Q, Q)
        k >>= 1
    return R


def ecdsa_verify(public_key: bytes, signature: bytes, digest: bytes) -> bool:
    """True iff `signature` (r||s, 64 bytes; a trailing 27/28 v byte is
    tolerated and ignored) is a valid ECDSA signature of `digest`
    (32 bytes) under the secp256k1 `public_key` (64-byte X||Y).
    Never raises on malformed input."""
    try:
        if len(public_key) != 64 or len(digest) != 32:
            return False
        if len(signature) == 65:
            signature = signature[:64]
        if len(signature) != 64:
            return False
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if not (1 <= r < _N and 1 <= s < _N):
            return False
        Q = (int.from_bytes(public_key[:32], "big"),
             int.from_bytes(public_key[32:], "big"))
        if (Q[1] * Q[1] - (Q[0] ** 3 + 7)) % _P != 0:
            return False  # pubkey point not on curve
        z = int.from_bytes(digest, "big")
        w = pow(s, -1, _N)
        point = _point_add(_point_mul((z * w) % _N, _G),
                           _point_mul((r * w) % _N, Q))
        if point is None:
            return False
        return point[0] % _N == r
    except Exception:
        return False
