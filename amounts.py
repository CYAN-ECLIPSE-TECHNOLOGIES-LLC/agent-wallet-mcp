"""Integer base-unit amounts for the Agent Wallet B0 core.

LOCAL/SANDBOX ONLY. Money is integer base units everywhere. Parsing and
formatting happen once, at the boundary, per the asset registry's decimals.
Accepted boundary types: str, int, decimal.Decimal. float and bool inputs
are rejected outright (bool is an int subclass in Python and must be
checked first). Negative amounts, over-precision, and values that overflow
a 256-bit word are rejected. There is no float arithmetic in this module.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext

MAX_UINT256 = (1 << 256) - 1
_AMOUNT_RE = re.compile(r"^([0-9]+)(?:\.([0-9]+))?$")


class AmountError(ValueError):
    """Typed amount rejection. `code` is stable for tests and receipts."""

    def __init__(self, message: str, code: str = "AMOUNT_INVALID"):
        super().__init__(message)
        self.code = code


def _check_decimals(decimals) -> int:
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        raise AmountError("decimals must be an int", "AMOUNT_DECIMALS_INVALID")
    if decimals < 0 or decimals > 36:
        raise AmountError("decimals out of supported range 0..36",
                          "AMOUNT_DECIMALS_INVALID")
    return decimals


def _check_base(value, name="amount_base_units") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AmountError(f"{name} must be an int of base units, "
                          f"got {type(value).__name__}", "AMOUNT_TYPE_INVALID")
    if value < 0:
        raise AmountError(f"{name} must not be negative", "AMOUNT_NEGATIVE")
    if value > MAX_UINT256:
        raise AmountError(f"{name} exceeds uint256", "AMOUNT_OVERFLOW")
    return value


def parse_amount(value, decimals: int) -> int:
    """Parse a human amount (str | int | Decimal) into integer base units.

    int means whole units. str must be a plain non-negative decimal literal
    (no sign, no exponent, no separators). Fractional digits beyond
    `decimals` are rejected, never rounded.
    """
    decimals = _check_decimals(decimals)
    if isinstance(value, bool) or isinstance(value, float):
        raise AmountError(
            f"amount type {type(value).__name__} is not accepted; "
            "use str, int or Decimal", "AMOUNT_TYPE_INVALID")
    if isinstance(value, int):
        if value < 0:
            raise AmountError("amount must not be negative", "AMOUNT_NEGATIVE")
        result = value * (10 ** decimals)
        return _check_base(result)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AmountError("amount must be finite", "AMOUNT_INVALID")
        if value < 0:
            raise AmountError("amount must not be negative", "AMOUNT_NEGATIVE")
        # Exact tuple arithmetic: Decimal.scaleb honours the ambient
        # context precision (28 digits by default) and would silently
        # round wei-scale values. as_tuple() never rounds.
        _sign, digits, exp = value.as_tuple()
        mantissa = 0
        for d in digits:
            mantissa = mantissa * 10 + d
        shift = exp + decimals
        if shift >= 0:
            return _check_base(mantissa * (10 ** shift))
        divisor = 10 ** (-shift)
        if mantissa % divisor:
            raise AmountError("amount has more precision than the asset "
                              "supports", "AMOUNT_OVER_PRECISION")
        return _check_base(mantissa // divisor)
    if isinstance(value, str):
        text = value.strip()
        m = _AMOUNT_RE.match(text)
        if not m:
            raise AmountError(f"malformed amount literal: {value!r}",
                              "AMOUNT_INVALID")
        whole, frac = m.group(1), m.group(2) or ""
        if len(frac) > decimals:
            raise AmountError("amount has more precision than the asset "
                              "supports", "AMOUNT_OVER_PRECISION")
        result = int(whole) * (10 ** decimals)
        if frac:
            result += int(frac.ljust(decimals, "0"))
        return _check_base(result)
    raise AmountError(f"amount type {type(value).__name__} is not accepted; "
                      "use str, int or Decimal", "AMOUNT_TYPE_INVALID")


def parse_base_units(value, decimals: int | None = None) -> int:
    """Validate a value that is already in integer base units (int or a
    plain integer string). Anything fractional or float-typed is rejected."""
    if isinstance(value, bool) or isinstance(value, float):
        raise AmountError("base units must be an int, got "
                          f"{type(value).__name__}", "AMOUNT_TYPE_INVALID")
    if isinstance(value, int):
        return _check_base(value)
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[0-9]+", text):
            raise AmountError(f"malformed base-unit literal: {value!r}",
                              "AMOUNT_INVALID")
        return _check_base(int(text))
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise AmountError("base units must be integral", "AMOUNT_INVALID")
        return _check_base(int(value))
    raise AmountError("base units must be int, str or Decimal, got "
                      f"{type(value).__name__}", "AMOUNT_TYPE_INVALID")


def format_amount(base_units, decimals: int) -> str:
    """Format integer base units as a canonical decimal string."""
    decimals = _check_decimals(decimals)
    base_units = _check_base(base_units)
    if decimals == 0:
        return str(base_units)
    whole, frac = divmod(base_units, 10 ** decimals)
    frac_s = str(frac).rjust(decimals, "0").rstrip("0")
    return f"{whole}.{frac_s}" if frac_s else str(whole)


def decimal_price_to_micro_usd(price) -> int:
    """Convert a Decimal/str USD price to integer micro-USD (exact)."""
    if isinstance(price, bool) or isinstance(price, float):
        raise AmountError("price must be str or Decimal, never float",
                          "AMOUNT_TYPE_INVALID")
    try:
        d = price if isinstance(price, Decimal) else Decimal(str(price))
    except (InvalidOperation, ValueError):
        raise AmountError(f"malformed price: {price!r}", "AMOUNT_INVALID")
    if not d.is_finite() or d < 0:
        raise AmountError("price must be finite and non-negative",
                          "AMOUNT_INVALID")
    with localcontext() as ctx:
        ctx.prec = 100
        scaled = d.scaleb(6)
        if scaled != scaled.to_integral_value():
            raise AmountError("price exceeds micro-USD precision",
                              "AMOUNT_OVER_PRECISION")
        return int(scaled)


def usd_value_micro(amount_base: int, decimals: int, price) -> int:
    """USD value (micro-USD, integer) of `amount_base` at `price` per whole
    unit. Computed with Decimal internally; result floored to micro-USD."""
    amount_base = _check_base(amount_base)
    decimals = _check_decimals(decimals)
    if isinstance(price, bool) or isinstance(price, float):
        raise AmountError("price must be str or Decimal, never float",
                          "AMOUNT_TYPE_INVALID")
    try:
        d = price if isinstance(price, Decimal) else Decimal(str(price))
    except (InvalidOperation, ValueError):
        raise AmountError(f"malformed price: {price!r}", "AMOUNT_INVALID")
    if not d.is_finite() or d < 0:
        raise AmountError("price must be finite and non-negative",
                          "AMOUNT_INVALID")
    with localcontext() as ctx:
        ctx.prec = 100
        value = (Decimal(amount_base) * d) / (Decimal(10) ** decimals)
        return int(value.scaleb(6).to_integral_value(rounding=ROUND_FLOOR))
