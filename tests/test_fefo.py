import unittest
from datetime import date
from decimal import Decimal

from coldledger.inventory.fefo import (
    InsufficientEligibleStock,
    Reservation,
    Stock,
    reserve,
)
from coldledger.inventory.ledger import InventoryLedger

SHIP = date(2026, 10, 1)


def stock(lot, expiry, qty, **kw):
    return Stock(lot, expiry, Decimal(str(qty)), **kw)


class FefoReservationTests(unittest.TestCase):
    def test_short_dated_earliest_lot_is_not_picked(self):
        # Earliest expiry but below the remaining-shelf-life minimum: FEFO must
        # skip it during selection, not grab it by date and fail downstream.
        rows = [
            stock("SHORT", date(2026, 10, 10), 5),
            stock("OK", date(2027, 1, 1), 10),
        ]
        decision = reserve(rows, 5, SHIP, 30)
        self.assertEqual([p.lot for p in decision], ["OK"])
        self.assertEqual(decision.shortfall, Decimal("0"))

    def test_conservative_reject_is_all_or_nothing(self):
        rows = [stock("OK", date(2027, 1, 1), 4)]
        with self.assertRaises(InsufficientEligibleStock) as ctx:
            reserve(rows, 5, SHIP, 30)
        self.assertEqual(ctx.exception.requested, Decimal("5"))
        self.assertEqual(ctx.exception.available, Decimal("4"))

    def test_conservative_reject_posts_nothing(self):
        ledger = InventoryLedger()
        rows = [stock("OK", date(2027, 1, 1), 4)]
        with self.assertRaises(InsufficientEligibleStock):
            reserve(rows, 5, SHIP, 30, ledger=ledger, reference="r1")
        self.assertEqual(ledger.entries(), ())
        self.assertEqual(ledger.balance("available:OK"), Decimal("0"))

    def test_partial_split_reports_shortfall(self):
        rows = [
            stock("A", date(2026, 12, 1), 4),
            stock("B", date(2027, 1, 1), 6),
        ]
        decision = reserve(rows, 15, SHIP, 30, allow_partial=True)
        self.assertEqual([p.lot for p in decision], ["A", "B"])
        self.assertEqual(decision.fulfilled, Decimal("10"))
        self.assertEqual(decision.shortfall, Decimal("5"))

    def test_partial_with_no_eligible_stock_reserves_nothing(self):
        ledger = InventoryLedger()
        rows = [stock("OLD", date(2026, 10, 2), 9)]
        decision = reserve(rows, 3, SHIP, 30, allow_partial=True,
                           ledger=ledger, reference="r1")
        self.assertEqual(list(decision), [])
        self.assertEqual(decision.shortfall, Decimal("3"))
        self.assertEqual(ledger.entries(), ())

    def test_success_posts_atomic_negative_entries_per_lot(self):
        ledger = InventoryLedger()
        rows = [
            stock("A", date(2026, 12, 1), 5),
            stock("B", date(2027, 1, 1), 10),
        ]
        decision = reserve(rows, 8, SHIP, 30, ledger=ledger, reference="r1")
        self.assertEqual(len(decision.entries), 2)
        self.assertEqual(ledger.balance("available:A"), Decimal("-5"))
        self.assertEqual(ledger.balance("available:B"), Decimal("-3"))
        # reversing every entry fully releases the hold
        for e in decision.entries:
            ledger.reverse(e.sequence, f"cancel:{e.reference}", "cancel")
        self.assertEqual(ledger.balance("available:A"), Decimal("0"))
        self.assertEqual(ledger.balance("available:B"), Decimal("0"))

    def test_conflicting_reference_leaves_ledger_untouched(self):
        ledger = InventoryLedger()
        ledger.post("available:A", "r1:A", Decimal("-1"), "existing")
        rows = [
            stock("A", date(2026, 12, 1), 5),
            stock("B", date(2027, 1, 1), 10),
        ]
        before = ledger.entries()
        with self.assertRaises(ValueError):
            reserve(rows, 8, SHIP, 30, ledger=ledger, reference="r1")
        self.assertEqual(ledger.entries(), before)

    def test_quarantined_and_unreleased_are_ineligible(self):
        rows = [
            stock("Q", date(2027, 1, 1), 5, quarantined=True),
            stock("U", date(2027, 1, 1), 5, released=False),
        ]
        with self.assertRaises(InsufficientEligibleStock):
            reserve(rows, 1, SHIP, 30)

    def test_result_iterates_like_pick_list(self):
        rows = [stock("A", date(2027, 1, 1), 5)]
        decision = reserve(rows, 2, SHIP, 30)
        self.assertIsInstance(decision, Reservation)
        self.assertEqual([p.lot for p in decision], ["A"])
        self.assertEqual(decision[0].quantity, Decimal("2"))
        self.assertEqual(len(decision), 1)


if __name__ == "__main__":
    unittest.main()
