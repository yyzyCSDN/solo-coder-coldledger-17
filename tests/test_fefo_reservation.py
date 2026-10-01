import unittest
from datetime import date
from decimal import Decimal

from coldledger.api import ColdLedgerService
from coldledger.inventory.fefo import (
    Stock, Pick, ShortageError, place_reservation, reserve, reserve_plan,
    free_lot_account, reserved_lot_account,
)
from coldledger.inventory.ledger import InventoryLedger


def seeded_ledger(stocks):
    ledger = InventoryLedger()
    for i, s in enumerate(stocks):
        ledger.post(free_lot_account(s.lot), f"seed:{s.lot}", s.quantity, "on-hand")
    return ledger


class EligibilityTests(unittest.TestCase):
    def test_earliest_lot_below_remaining_shelf_life_is_skipped(self):
        ship = date(2026, 10, 1)
        rows = [
            Stock('A', date(2026, 10, 20), Decimal('5')),   # 19 days < 30
            Stock('B', date(2027, 2, 1), Decimal('10')),
        ]
        picks = reserve(rows, 5, ship, 30)
        self.assertEqual([p.lot for p in picks], ['B'])
        self.assertEqual(picks[0].quantity, Decimal('5'))

    def test_old_code_forced_candidates_zero_even_when_other_stock_qualifies(self):
        # Regression: the previous `or s is candidates[0]` clause took the
        # soonest-expiring lot regardless of shelf life.
        ship = date(2026, 10, 1)
        rows = [
            Stock('A', ship, Decimal('5')),
            Stock('B', date(2027, 2, 1), Decimal('10')),
        ]
        picks = reserve(rows, 5, ship, 30)
        self.assertEqual([p.lot for p in picks], ['B'])

    def test_boundary_remaining_days_is_eligible(self):
        ship = date(2026, 10, 1)
        rows = [Stock('A', date(2026, 10, 31), Decimal('5'))]  # exactly 30 days
        self.assertEqual([p.lot for p in reserve(rows, 5, ship, 30)], ['A'])

    def test_quarantined_and_unreleased_lots_are_not_eligible(self):
        ship = date(2026, 10, 1)
        rows = [
            Stock('A', date(2027, 1, 1), Decimal('5'), released=False),
            Stock('B', date(2027, 1, 2), Decimal('5'), quarantined=True),
            Stock('C', date(2027, 1, 3), Decimal('5')),
        ]
        self.assertEqual([p.lot for p in reserve(rows, 5, ship, 30)], ['C'])

    def test_fefo_order_holds_among_qualified_lots(self):
        ship = date(2026, 10, 1)
        rows = [
            Stock('A', date(2027, 1, 1), Decimal('5')),
            Stock('B', date(2027, 2, 1), Decimal('10')),
        ]
        self.assertEqual([p.lot for p in reserve(rows, 8, ship, 30)], ['A', 'B'])


class ShortagePolicyTests(unittest.TestCase):
    def test_reject_is_default_and_conservative(self):
        ship = date(2026, 10, 1)
        rows = [Stock('A', date(2027, 1, 1), Decimal('5'))]
        with self.assertRaises(ShortageError):
            reserve(rows, 8, ship, 30)
        self.assertIsInstance(ShortageError(), ValueError)

    def test_split_reports_shortfall_without_raising(self):
        ship = date(2026, 10, 1)
        rows = [Stock('A', date(2027, 1, 1), Decimal('5'))]
        picks, shortfall = reserve_plan(rows, 8, ship, 30, partial="split")
        self.assertEqual(picks, [Pick('A', Decimal('5'), date(2027, 1, 1))])
        self.assertEqual(shortfall, Decimal('3'))

    def test_split_with_zero_eligible_stock(self):
        ship = date(2026, 10, 1)
        rows = [Stock('A', ship, Decimal('5'))]  # expires on ship date
        picks, shortfall = reserve_plan(rows, 5, ship, 30, partial="split")
        self.assertEqual(picks, [])
        self.assertEqual(shortfall, Decimal('5'))

    def test_invalid_partial_mode_rejected(self):
        with self.assertRaises(ValueError):
            reserve([], 1, date(2026, 10, 1), 30, partial="maybe")


class AtomicReservationTests(unittest.TestCase):
    SHIP = date(2026, 10, 1)

    def test_successful_reservation_moves_free_to_reserved(self):
        rows = [
            Stock('A', date(2027, 1, 1), Decimal('5')),
            Stock('B', date(2027, 2, 1), Decimal('10')),
        ]
        ledger = seeded_ledger(rows)
        result = place_reservation(rows, 8, self.SHIP, 30, ledger, 'ORD-1')
        self.assertTrue(result.complete)
        self.assertEqual(result.shortfall, Decimal('0'))
        self.assertEqual(result.reserved, Decimal('8'))
        self.assertEqual([p.lot for p in result.picks], ['A', 'B'])
        self.assertEqual(ledger.balance(free_lot_account('A')), Decimal('0'))
        self.assertEqual(ledger.balance(free_lot_account('B')), Decimal('7'))
        self.assertEqual(ledger.balance(reserved_lot_account('A')), Decimal('5'))
        self.assertEqual(ledger.balance(reserved_lot_account('B')), Decimal('3'))

    def test_eligibility_and_occupation_decided_together_skips_expiring_lot(self):
        rows = [
            Stock('A', date(2026, 10, 20), Decimal('5')),  # only 19 days
            Stock('B', date(2027, 2, 1), Decimal('10')),
        ]
        ledger = seeded_ledger(rows)
        result = place_reservation(rows, 5, self.SHIP, 30, ledger, 'ORD-1')
        self.assertEqual([p.lot for p in result.picks], ['B'])
        # soonest lot must never have been touched, even transiently
        self.assertEqual(ledger.balance(free_lot_account('A')), Decimal('5'))
        self.assertEqual(ledger.balance(reserved_lot_account('A')), Decimal('0'))
        self.assertEqual(ledger.balance(free_lot_account('B')), Decimal('5'))
        self.assertEqual(ledger.balance(reserved_lot_account('B')), Decimal('5'))

    def test_reject_failure_posts_nothing(self):
        rows = [
            Stock('A', date(2026, 10, 20), Decimal('5')),  # fails shelf life
            Stock('B', date(2027, 2, 1), Decimal('2')),     # qualified but short
        ]
        ledger = seeded_ledger(rows)
        before = ledger.entries()
        with self.assertRaises(ShortageError):
            place_reservation(rows, 8, self.SHIP, 30, ledger, 'ORD-1')
        self.assertEqual(ledger.entries(), before, "failed reject left ledger rows")
        self.assertEqual(ledger.balance(free_lot_account('A')), Decimal('5'))
        self.assertEqual(ledger.balance(free_lot_account('B')), Decimal('2'))
        self.assertEqual(ledger.balance(reserved_lot_account('B')), Decimal('0'))

    def test_split_occupies_what_is_qualified_and_reports_shortfall(self):
        rows = [
            Stock('A', date(2026, 10, 20), Decimal('5')),  # fails shelf life
            Stock('B', date(2027, 2, 1), Decimal('3')),
        ]
        ledger = seeded_ledger(rows)
        result = place_reservation(rows, 8, self.SHIP, 30, ledger, 'ORD-1', partial="split")
        self.assertFalse(result.complete)
        self.assertEqual(result.reserved, Decimal('3'))
        self.assertEqual(result.shortfall, Decimal('5'))
        self.assertEqual([p.lot for p in result.picks], ['B'])
        self.assertEqual(ledger.balance(free_lot_account('B')), Decimal('0'))
        self.assertEqual(ledger.balance(reserved_lot_account('B')), Decimal('3'))
        self.assertEqual(ledger.balance(free_lot_account('A')), Decimal('5'))

    def test_ledger_balance_below_stock_quantity_is_the_real_occupation_limit(self):
        # Stock master says 10, but free account only holds 6: the ledger wins,
        # and reject fails without touching anything.
        rows = [Stock('A', date(2027, 1, 1), Decimal('10'))]
        ledger = InventoryLedger()
        ledger.post(free_lot_account('A'), 'seed:A', Decimal('6'), 'on-hand')
        with self.assertRaises(ShortageError):
            place_reservation(rows, 8, self.SHIP, 30, ledger, 'ORD-1')
        self.assertEqual(ledger.balance(free_lot_account('A')), Decimal('6'))
        self.assertEqual(ledger.balance(reserved_lot_account('A')), Decimal('0'))

    def test_split_clips_to_actual_free_balance(self):
        rows = [Stock('A', date(2027, 1, 1), Decimal('10'))]
        ledger = InventoryLedger()
        ledger.post(free_lot_account('A'), 'seed:A', Decimal('6'), 'on-hand')
        result = place_reservation(rows, 8, self.SHIP, 30, ledger, 'ORD-1', partial="split")
        self.assertEqual(result.reserved, Decimal('6'))
        self.assertEqual(result.shortfall, Decimal('2'))
        self.assertEqual(result.picks[0].quantity, Decimal('6'))

    def test_duplicate_reference_is_rejected(self):
        rows = [Stock('A', date(2027, 1, 1), Decimal('10'))]
        ledger = seeded_ledger(rows)
        place_reservation(rows, 2, self.SHIP, 30, ledger, 'ORD-1')
        with self.assertRaises(ValueError):
            place_reservation(rows, 2, self.SHIP, 30, ledger, 'ORD-1')
        # the rejected duplicate changed nothing
        self.assertEqual(ledger.balance(free_lot_account('A')), Decimal('8'))

    def test_ledger_audit_still_passes_after_reservation(self):
        rows = [
            Stock('A', date(2027, 1, 1), Decimal('5')),
            Stock('B', date(2027, 2, 1), Decimal('10')),
        ]
        ledger = seeded_ledger(rows)
        place_reservation(rows, 8, self.SHIP, 30, ledger, 'ORD-1')
        ledger.audit()

    def test_service_exposes_atomic_reservation(self):
        rows = [Stock('A', date(2027, 1, 1), Decimal('5'))]
        ledger = seeded_ledger(rows)
        result = ColdLedgerService().place_reservation(
            rows, 3, self.SHIP, 30, ledger, 'ORD-9'
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.picks[0].lot, 'A')


if __name__ == '__main__':
    unittest.main()
