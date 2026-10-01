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

class InsufficientStock(ValueError):
    """Raised when an atomic reservation cannot cover the requested quantity."""


class InventoryLedger:
    def __init__(self):
        self._rows: list[Entry] = []
        self._refs: set[tuple[str, str]] = set()
        self._batch_refs: set[str] = set()
        self._lock = RLock()

    def post(self, account: str, reference: str, quantity, reason: str) -> Entry:
        q = Decimal(str(quantity))
        if not account or not reference or not reason:
            raise ValueError("account, reference and reason required")
        if q == 0:
            raise ValueError("zero inventory entry")
        key = (account, reference)
        with self._lock:
            if key in self._refs:
                raise ValueError("duplicate idempotency reference")
            row = Entry(len(self._rows) + 1, account, reference, q, reason)
            self._rows.append(row)
            self._refs.add(key)
            return row

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

    def reserve_batch(
        self,
        candidates: Iterable[tuple[str, str, Decimal]],
        quantity,
        reference: str,
        reason: str,
        partial: str = "reject",
    ) -> tuple[tuple[tuple[str, str, Decimal], ...], Decimal]:
        """Atomically reserve quantity against free balances of ordered candidate lots.

        ``candidates`` is FEFO ordered: (source_free_account, target_reserved_account,
        cap). Each candidate may contribute up to ``cap`` and up to its current free
        balance.

        partial="reject": the whole request must be covered from one snapshot of
        balances under a single lock, otherwise nothing is posted.
        partial="split": greedily cover as much as possible and post exactly that;
        the returned shortfall tells the caller what was left unmet.

        Returns (moves, shortfall). Either every move posts or none do.
        """
        q = Decimal(str(quantity))
        if q <= 0:
            raise ValueError("reserve quantity must be positive")
        if not reference or not reason:
            raise ValueError("reference and reason required")
        if partial not in ("reject", "split"):
            raise ValueError("partial must be 'reject' or 'split'")
        with self._lock:
            ordered = [
                (src, tgt, min(Decimal(str(cap)), self.balance(src)))
                for src, tgt, cap in candidates
            ]
            ordered = [c for c in ordered if c[2] > 0]
            if any(
                ref.startswith(reference + ":") or ref == reference
                for _acc, ref in self._refs
            ) or reference in self._batch_refs:
                raise ValueError("duplicate idempotency reference")

            moves: list[tuple[str, str, Decimal]] = []
            filled = Decimal("0")
            for src, tgt, cap in ordered:
                if filled >= q:
                    break
                take = min(cap, q - filled)
                if take <= 0:
                    continue
                if self.balance(src) < take:
                    raise InsufficientStock(f"insufficient free balance on {src}")
                for i, (msrc, mtgt, mq) in enumerate(moves):
                    if msrc == src and mtgt == tgt:
                        moves[i] = (msrc, mtgt, mq + take)
                        break
                else:
                    moves.append((src, tgt, take))
                filled += take
            shortfall = q - filled
            if partial == "reject" and shortfall > 0:
                raise InsufficientStock("insufficient eligible stock")
            if not moves:
                return (), q

            seq = len(self._rows)
            rows: list[Entry] = []
            for src, tgt, amount in moves:
                seq += 1
                rows.append(Entry(seq, src, f"{reference}:{src}:debit", -amount, reason))
                seq += 1
                rows.append(Entry(seq, tgt, f"{reference}:{tgt}:credit", amount, reason))
            self._rows.extend(rows)
            for row in rows:
                self._refs.add((row.account, row.reference))
            self._batch_refs.add(reference)
            return tuple(moves), shortfall

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
