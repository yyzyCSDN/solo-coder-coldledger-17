from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Iterable

@dataclass(frozen=True)
class Entry:
    sequence: int
    account: str
    reference: str
    quantity: Decimal
    reason: str
    reversal_of: int | None = None

class InventoryLedger:
    def __init__(self):
        self._rows: list[Entry] = []
        self._refs: set[tuple[str, str]] = set()
        self._lock = RLock()

    def post(self, account: str, reference: str, quantity, reason: str) -> Entry:
        return self.post_many([(account, reference, quantity, reason)])[0]

    def post_many(self, items: Iterable[tuple[str, str, object, str]]) -> tuple[Entry, ...]:
        """Post several entries atomically.

        Every item is validated (required fields, non-zero quantity, unique
        idempotency reference within the batch and against existing rows)
        before any row is appended, so a failed batch leaves the ledger
        unchanged instead of half-posted.
        """
        prepared: list[tuple[str, str, Decimal, str]] = []
        seen: set[tuple[str, str]] = set()
        for account, reference, quantity, reason in items:
            q = Decimal(str(quantity))
            if not account or not reference or not reason:
                raise ValueError("account, reference and reason required")
            if q == 0:
                raise ValueError("zero inventory entry")
            key = (account, reference)
            if key in seen:
                raise ValueError("duplicate idempotency reference")
            seen.add(key)
            prepared.append((account, reference, q, reason))
        with self._lock:
            if any(key in self._refs for key in seen):
                raise ValueError("duplicate idempotency reference")
            base = len(self._rows)
            entries = tuple(
                Entry(base + i + 1, account, reference, q, reason)
                for i, (account, reference, q, reason) in enumerate(prepared)
            )
            self._rows.extend(entries)
            self._refs.update(seen)
            return entries

    def reverse(self, sequence: int, reference: str, reason: str) -> Entry:
        with self._lock:
            originals = [r for r in self._rows if r.sequence == sequence]
            if not originals:
                raise KeyError(sequence)
            original = originals[0]
            if any(r.reversal_of == sequence for r in self._rows):
                raise ValueError("entry already reversed")
            key = (original.account, reference)
            if key in self._refs:
                raise ValueError("duplicate reversal reference")
            row = Entry(len(self._rows) + 1, original.account, reference, -original.quantity, reason, sequence)
            self._rows.append(row)
            self._refs.add(key)
            return row

    def balance(self, account: str) -> Decimal:
        with self._lock:
            return sum((r.quantity for r in self._rows if r.account == account), Decimal("0"))

    def entries(self, account: str | None = None) -> tuple[Entry, ...]:
        with self._lock:
            rows = self._rows if account is None else [r for r in self._rows if r.account == account]
            return tuple(rows)

    def reconcile(self, expected: dict[str, Decimal]) -> dict[str, Decimal]:
        accounts = set(expected) | {r.account for r in self._rows}
        return {a: self.balance(a) - expected.get(a, Decimal("0")) for a in sorted(accounts)}

    def transfer(self, source: str, target: str, quantity, reference: str, reason: str) -> tuple[Entry, Entry]:
        q = Decimal(str(quantity))
        if q <= 0:
            raise ValueError("transfer quantity must be positive")
        with self._lock:
            if self.balance(source) < q:
                raise ValueError("insufficient source balance")
            debit_ref = reference + ":debit"
            credit_ref = reference + ":credit"
            if (source, debit_ref) in self._refs or (target, credit_ref) in self._refs:
                raise ValueError("duplicate transfer reference")
            debit = Entry(len(self._rows)+1, source, debit_ref, -q, reason)
            credit = Entry(len(self._rows)+2, target, credit_ref, q, reason)
            self._rows.extend([debit, credit])
            self._refs.add((source, debit_ref)); self._refs.add((target, credit_ref))
            return debit, credit

    def audit(self) -> None:
        seen_sequences = set()
        seen_refs = set()
        for row in self._rows:
            if row.sequence in seen_sequences:
                raise ValueError("duplicate sequence")
            seen_sequences.add(row.sequence)
            key = (row.account, row.reference)
            if key in seen_refs:
                raise ValueError("duplicate reference")
            seen_refs.add(key)
            if row.reversal_of is not None and row.reversal_of >= row.sequence:
                raise ValueError("invalid reversal pointer")


def net(entries: Iterable[Entry]) -> Decimal:
    return sum((r.quantity for r in entries), Decimal("0"))


def by_reason(entries: Iterable[Entry]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in entries:
        out[r.reason] = out.get(r.reason, Decimal("0")) + r.quantity
    return out
