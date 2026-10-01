from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .ledger import Entry, InventoryLedger


@dataclass(frozen=True)
class Stock:
    lot: str
    expiry: date
    quantity: Decimal
    released: bool = True
    quarantined: bool = False


@dataclass(frozen=True)
class Pick:
    lot: str
    quantity: Decimal
    expiry: date


class InsufficientEligibleStock(ValueError):
    """Conservative (all-or-nothing) reservation cannot be covered by eligible stock."""

    def __init__(self, requested: Decimal, available: Decimal):
        self.requested = requested
        self.available = available
        super().__init__(
            f"eligible stock {available} cannot cover requested {requested}; "
            "pass allow_partial=True to split-fulfil instead"
        )


@dataclass(frozen=True)
class Reservation:
    """Outcome of one atomic FEFO decision.

    Eligibility, lot selection and the optional ledger posting are decided
    together, so a Reservation always describes a consistent state: either the
    picks (and their ledger entries) exist, or nothing was reserved at all.
    Iterating a Reservation yields its picks, so callers that loop over the
    result of reserve() keep working.
    """

    picks: tuple[Pick, ...]
    requested: Decimal
    shortfall: Decimal
    partial: bool
    entries: tuple[Entry, ...] = ()

    def __iter__(self) -> Iterator[Pick]:
        return iter(self.picks)

    def __len__(self) -> int:
        return len(self.picks)

    def __getitem__(self, index):
        return self.picks[index]

    @property
    def fulfilled(self) -> Decimal:
        return self.requested - self.shortfall


def _eligible(stock: Stock, ship_date: date, min_remaining_days: int) -> bool:
    if stock.quantity <= 0 or not stock.released or stock.quarantined:
        return False
    return (stock.expiry - ship_date).days >= min_remaining_days


def reserve(stocks, requested, ship_date, min_remaining_days, allow_partial=False,
            ledger=None, account="available", reference=None, reason="fefo-reservation"):
    """Reserve stock FEFO as a single consistent decision.

    Shelf-life eligibility (remaining days at ship_date >= min_remaining_days,
    released, not quarantined, positive quantity), lot selection (earliest
    expiry first, lot id as tiebreak) and the optional ledger posting are
    decided together: a reservation that cannot be decided reserves nothing,
    and a posting that fails leaves the ledger untouched.

    Shortfall policy is explicit via ``allow_partial``:

    - ``False`` (default, conservative reject): eligible supply must cover the
      whole request, otherwise InsufficientEligibleStock is raised and nothing
      is reserved.
    - ``True`` (split fulfilment): eligible stock is picked in FEFO order as
      far as it goes and the unmet remainder is reported as
      ``Reservation.shortfall`` (possibly the whole request).

    When ``ledger`` and ``reference`` are given, each pick is posted as a
    negative entry to account "{account}:{lot}" with idempotency reference
    "{reference}:{lot}". All entries are posted atomically via
    InventoryLedger.post_many: every one succeeds or none is written. Release
    the hold by reversing each entry in ``Reservation.entries``.
    """
    need = Decimal(str(requested))
    if need <= 0:
        raise ValueError("positive request required")
    if min_remaining_days < 0:
        raise ValueError("negative minimum remaining days")
    if (ledger is None) != (reference is None):
        raise ValueError("ledger and reference are required together")

    eligible = sorted(
        (s for s in stocks if _eligible(s, ship_date, min_remaining_days)),
        key=lambda s: (s.expiry, s.lot),
    )
    available = sum((s.quantity for s in eligible), Decimal("0"))
    if not allow_partial and available < need:
        raise InsufficientEligibleStock(need, available)

    picks: list[Pick] = []
    remaining = need
    for s in eligible:
        if remaining <= 0:
            break
        q = min(s.quantity, remaining)
        picks.append(Pick(s.lot, q, s.expiry))
        remaining -= q

    entries: tuple[Entry, ...] = ()
    if ledger is not None and picks:
        entries = ledger.post_many(
            (f"{account}:{p.lot}", f"{reference}:{p.lot}", -p.quantity, reason)
            for p in picks
        )
    return Reservation(tuple(picks), need, remaining, bool(allow_partial), entries)
