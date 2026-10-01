# Inventory


## Overview

The inventory workspace is the item and stock master. It combines a searchable,
filterable product list with batch-level stock, valuation, and lifecycle actions.

## Features

- **Item master** — article ID, name, generic name, manufacturer, category, pack size,
  unit, GST rate, MRP and barcode.
- **Batches** — per-batch expiry, purchase rate, selling rate, MRP and on-hand quantity.
- **Search & filters** — full-text search plus category, manufacturer, supplier,
  purchase-rate range, MRP range, expiry window and stock level; quick views for
  all / in / low / out / expiring / expired.
- **Valuation** — whole-shelf value at purchase cost and at MRP, shown in the header
  cards and the table.
- **Bulk actions** — enable, disable, delete (administrator password required) and
  restore, plus CSV / Excel export of a selection.


## Recycle bin

Items are soft-deleted to a recycle bin and can be restored. Receiving new stock for a
deleted product creates a **new** active item rather than resurrecting the deleted one.

## Expiry and adjustments

Expiry alerts lists approaching and expired batches and can snooze or settle them;
settling debits stock. Loose and damaged stock is written off through audited stock
adjustments.


## Product details


## Technical notes

- Search uses a SQLite FTS5 virtual table kept in sync by triggers.
- All quantity changes pass through one service module so every movement is audited.
- Valuation and counts exclude soft-deleted items.
