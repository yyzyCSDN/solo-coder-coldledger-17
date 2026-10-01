# ColdLedger Pharmaceutical Cold-Chain Operations Lab

Dependency-free Python reference implementation for temperature-sensitive pharmaceutical distribution. It models inventory expiry, chain-of-custody, telemetry correction, packaging qualification, dry-ice depletion, freezer allocation, customs holds, serialization, recalls, release decisions, vehicle compartment compatibility, returns, SLA calendars and cross-dock state.

Runtime third-party dependencies: none.

## FEFO reservation contract

`coldledger.inventory.fefo.reserve()` decides shelf-life eligibility (remaining
days at ship date ≥ the required minimum, released, not quarantined), lot
selection (earliest expiry first) and the optional ledger posting in one atomic
step — a failed decision reserves nothing and writes no ledger entries.

Shortfall policy is explicit via `allow_partial`:

- `False` (default, conservative reject): the request is covered in full or
  `InsufficientEligibleStock` is raised and nothing is reserved.
- `True` (split fulfilment): eligible stock is picked in FEFO order as far as
  it goes; the unmet remainder is reported as `Reservation.shortfall`.

When a `ledger` and `reference` are supplied, every pick is posted as a
negative entry to account `"{account}:{lot}"` via
`InventoryLedger.post_many()`, which validates the whole batch before writing
so all entries succeed or none is written. Release a hold by reversing each
entry in `Reservation.entries`.
