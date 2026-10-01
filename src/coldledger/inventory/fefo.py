from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .ledger import InsufficientStock, InventoryLedger


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


# Re-exported for callers that want to catch shortage specifically; it is a
# ValueError subclass so existing ``except ValueError`` code keeps working.
ShortageError = InsufficientStock


def free_lot_account(lot: str) -> str:
    return f"lot:{lot}:free"


def reserved_lot_account(lot: str) -> str:
    return f"lot:{lot}:reserved"


def is_eligible(stock: Stock, ship_date: date, min_remaining_days: int) -> bool:
    """Expiry qualification is decided strictly on remaining shelf life.

    FEFO ordering never overrides this: an expiring lot is skipped, not taken
    "first and noticed later".
    """
    return (stock.expiry - ship_date).days >= min_remaining_days


def eligible_stocks(stocks, ship_date, min_remaining_days):
    """Eligible, on-hand lots in FEFO order (earliest expiry first)."""
    candidates = [
        s
        for s in stocks
        if s.quantity > 0 and s.released and not s.quarantined
        and is_eligible(s, ship_date, min_remaining_days)
    ]
    candidates.sort(key=lambda s: (s.expiry, s.lot))
    return candidates


def reserve(stocks, requested, ship_date, min_remaining_days, partial: str = "reject"):
    """Plan FEFO picks from expiry-qualified lots.

    Pure planning: touches no inventory accounts. Conservative by default:
    partial="reject" raises ShortageError unless the full request is covered.
    partial="split" returns as many picks as qualified stock allows; the caller
    can see the unmet quantity via reserve_plan().
    """
    return reserve_plan(stocks, requested, ship_date, min_remaining_days, partial)[0]


def reserve_plan(stocks, requested, ship_date, min_remaining_days, partial: str = "reject"):
    """Return (picks, shortfall) without mutating any account."""
    if partial not in ("reject", "split"):
        raise ValueError("partial must be 'reject' or 'split'")
    need = Decimal(str(requested))
    if need <= 0:
        raise ValueError("positive request required")
    picks = []
    for s in eligible_stocks(stocks, ship_date, min_remaining_days):
        if need <= 0:
            break
        q = min(s.quantity, need)
        picks.append(Pick(s.lot, q, s.expiry))
        need -= q
    shortfall = need
    if partial == "reject" and shortfall > 0:
        raise InsufficientStock("insufficient eligible stock")
    return picks, shortfall


def place_reservation(
    stocks,
    requested,
    ship_date,
    min_remaining_days,
    ledger: InventoryLedger,
    reference: str,
    reason: str = "reservation",
    partial: str = "reject",
):
    """Decide expiry eligibility, FEFO selection and ledger occupation atomically.

    Qualification (released/quarantined/remaining shelf life) is evaluated first;
    only qualified lots are offered to the ledger. The ledger then covers the
    request against one snapshot of free balances under a single lock and posts
    free->reserved moves only when the chosen policy succeeds in full:

    * partial="reject" (default, conservative): any shortage makes the whole
      reservation fail with ShortageError and no ledger row is written.
    * partial="split": qualified stock is occupied greedily and the returned
      ReservationResult reports the unmet shortfall for partial fulfilment.

    There is no window in which accounts are occupied before eligibility or
    coverage is known, and a failed decision posts nothing.
    """
    need = Decimal(str(requested))
    if need <= 0:
        raise ValueError("positive request required")
    if not reference:
        raise ValueError("reference required")
    # Eligibility + FEFO ordering first; caps are the on-hand quantities.
    ordered = eligible_stocks(stocks, ship_date, min_remaining_days)
    candidates = [
        (free_lot_account(s.lot), reserved_lot_account(s.lot), s.quantity)
        for s in ordered
    ]
    moves, shortfall = ledger.reserve_batch(
        candidates, need, reference, reason, partial=partial
    )
    by_lot = {s.lot: s for s in ordered}
    picks = tuple(
        Pick(_lot_of(src), amount, by_lot[_lot_of(src)].expiry)
        for src, _tgt, amount in moves
    )
    return ReservationResult(
        picks=picks,
        reserved=need - shortfall,
        shortfall=shortfall,
        reference=reference,
    )


def _lot_of(free_account: str) -> str:
    return free_account[len("lot:") : -len(":free")]


@dataclass(frozen=True)
class ReservationResult:
    picks: tuple[Pick, ...]
    reserved: Decimal
    shortfall: Decimal
    reference: str

    @property
    def complete(self) -> bool:
        return self.shortfall == 0
