"""In-memory mock customer store. Real systems would call a database or API here."""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    currency: str


@dataclass(frozen=True)
class Refund:
    refund_id: str
    customer_id: str
    amount: Decimal
    currency: str
    reason: str
    status: str


@dataclass
class Store:
    customers: dict[str, Customer] = field(default_factory=dict)
    refunds: list[Refund] = field(default_factory=list)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, customer_id: str) -> Customer | None:
        return self.customers.get(customer_id)

    def add_refund(self, customer: Customer, amount: Decimal, reason: str) -> Refund:
        with self._lock:
            refund = Refund(
                refund_id=f"RF-{next(self._seq):06d}",
                customer_id=customer.customer_id,
                amount=amount,
                currency=customer.currency,
                reason=reason,
                status="queued",
            )
            self.refunds.append(refund)
            return refund


def default_store() -> Store:
    customers = [
        Customer("CUST-00001", "Ada Example", "ada@example.com", "gold", "USD"),
        Customer("CUST-A1B2C", "Grace Sample", "grace@example.com", "silver", "USD"),
        Customer("CUST-ZZ999", "Alan Demo", "alan@example.com", "bronze", "EUR"),
    ]
    return Store(customers={c.customer_id: c for c in customers})
