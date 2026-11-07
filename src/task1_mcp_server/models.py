"""Pydantic v2 argument models for the two tools.

Validation order for every string field: sanitize/normalize -> injection policy ->
field-specific rule on the *cleaned* value. The cleaned value is what the tool
handlers receive, store, and echo back, so the value that was validated is the value
that is used.

The ``Sanitizer`` instance is supplied through Pydantic's validation context:
``Model.model_validate(args, context={"sanitizer": sanitizer})``.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from .sanitizer import InjectionRejected, InputTooLong, Sanitizer

CUSTOMER_ID_PATTERN = r"^CUST-[A-Z0-9]{5}$"
_CUSTOMER_ID_RE = re.compile(CUSTOMER_ID_PATTERN)
REASON_MIN_LENGTH = 10
AMOUNT_MAX_DECIMALS = 2

_DEFAULT_SANITIZER = Sanitizer()


def _sanitizer(info: ValidationInfo) -> Sanitizer:
    ctx = info.context or {}
    return ctx.get("sanitizer", _DEFAULT_SANITIZER)


def _clean_string(value: Any, info: ValidationInfo) -> str:
    field = info.field_name or "?"
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "{field} must be a string", {"field": field})
    try:
        return _sanitizer(info).clean(field, value)
    except InjectionRejected as exc:
        raise PydanticCustomError(
            "injection_detected",
            "{field}: {detail}",
            {"field": field, "detail": str(exc), "category": exc.finding.category, "rule": exc.finding.rule},
        ) from None
    except InputTooLong as exc:
        raise PydanticCustomError("string_too_long", "{field}: {detail}", {"field": field, "detail": str(exc)}) from None


def _validate_customer_id(value: Any, info: ValidationInfo) -> str:
    cleaned = _clean_string(value, info)
    if not _CUSTOMER_ID_RE.fullmatch(cleaned):
        raise PydanticCustomError(
            "customer_id_format",
            "customer_id must match CUST-XXXXX where each X is an uppercase letter A-Z or a digit 0-9",
        )
    return cleaned


def amount_to_decimal(value: float) -> Decimal:
    """Exact decimal for a validated amount (repr gives the shortest round-trip form)."""
    return Decimal(repr(float(value))).quantize(Decimal("0.01"))


class GetCustomerRecordArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    customer_id: str

    @field_validator("customer_id", mode="before")
    @classmethod
    def _v_customer_id(cls, v: Any, info: ValidationInfo) -> str:
        return _validate_customer_id(v, info)


class TriggerRefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    customer_id: str
    amount: float
    reason: str

    @field_validator("customer_id", mode="before")
    @classmethod
    def _v_customer_id(cls, v: Any, info: ValidationInfo) -> str:
        return _validate_customer_id(v, info)

    @field_validator("amount", mode="before")
    @classmethod
    def _v_amount(cls, v: Any) -> float:
        # bool is a subclass of int in Python; JSON true/false must not become 1/0.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise PydanticCustomError("amount_type", "amount must be a JSON number (not a string or boolean)")
        try:
            as_float = float(v)
        except OverflowError:
            raise PydanticCustomError("amount_not_finite", "amount must be a finite number") from None
        if not math.isfinite(as_float):
            raise PydanticCustomError("amount_not_finite", "amount must be a finite number")
        try:
            exact = Decimal(v) if isinstance(v, int) else Decimal(repr(as_float))
        except InvalidOperation:  # pragma: no cover - finite floats always convert
            raise PydanticCustomError("amount_type", "amount must be a JSON number") from None
        if exact <= 0:
            raise PydanticCustomError("amount_not_positive", "amount must be greater than 0")
        exponent = exact.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -AMOUNT_MAX_DECIMALS:
            raise PydanticCustomError(
                "amount_precision", "amount may have at most {places} decimal places", {"places": AMOUNT_MAX_DECIMALS}
            )
        return as_float

    @field_validator("reason", mode="before")
    @classmethod
    def _v_reason(cls, v: Any, info: ValidationInfo) -> str:
        cleaned = _clean_string(v, info)
        if len(cleaned) < REASON_MIN_LENGTH:
            raise PydanticCustomError(
                "reason_too_short",
                "reason must be at least {min} characters after sanitization (got {got})",
                {"min": REASON_MIN_LENGTH, "got": len(cleaned)},
            )
        return cleaned


def validation_error_data(exc: ValidationError) -> list[dict[str, Any]]:
    """JSON-safe error details for the JSON-RPC ``error.data`` field.

    Inputs are deliberately excluded so attack payloads and PII are not reflected
    back to the caller.
    """
    out: list[dict[str, Any]] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        out.append(
            {
                "field": ".".join(str(p) for p in err.get("loc", ())) or "(arguments)",
                "type": err.get("type"),
                "message": err.get("msg"),
            }
        )
    return out
