# Point of sale


## Overview

The POS is a single-screen billing workspace: a search-and-cart area on the left and a
checkout summary on the right, with parked/recent bills and a "new sale" action in the
header.

## Billing flow

1. **Search or scan** a product (name, generic, article ID or barcode). Keyboard
   shortcuts: `F2` focuses search, arrow keys navigate results, `Enter` adds.
2. **Add** to the cart. Each line shows the batch and expiry; quantities are stepped
   with `+`/`−` or typed; pack-based items offer a "+1 pack" shortcut.
3. **Discount** the bill by amount or percentage (subject to permission).
4. **Choose a payment method** — Cash, UPI or Card. For cash, enter the amount
   received to see the change due.
5. **Review payment** and **record** it. The sale posts to the counter's cash ledger,
   deducts stock (FEFO) and produces a printable/exportable invoice.


## Customer selection

The **Customer** button (`F3`) opens a dialog with two categories:

- **Walk-In** — an over-the-counter sale; a customer record is optional.
- **Home Delivery** — a sale dispatched to a customer's address.

The category is stored on the sale and shown on the invoice, sales history, exports and
the reports breakdown. Change it any time with the **Walk-In / Delivery** switch in the
customer strip — one click updates the current sale and persists the customer's category
without re-selecting anyone. Search by name, mobile
or CX ID to pick an existing customer; if nothing matches, the **add-new-customer** form
opens automatically (pre-filled from the search text) so the customer can be created
without leaving the POS. Entering a mobile that already exists selects the existing
customer instead of duplicating it.

## Manual bill (Alt+L)

For anything the stock bill cannot take: an item that is out of stock, not received yet, or not in
inventory at all. A manual bill never checks or changes stock and has its own number series (`MB-…`).

- Search works as usual and **any** product can be chosen, whatever its stock; its MRP becomes the rate.
  When nothing matches, the last row (or Enter) adds the typed name as an item not in inventory.
- Every column of a manual line is a field: click code, item name, batch, expiry (MM/YY), pack,
  quantity, rate, discount or amount and type (an amount sets the rate, after the discount). Tab moves to the next field, Enter returns to the item search; totals follow as
  you type.
- A saved manual bill is edited from Sales (F4) like any bill: it opens as a manual bill again.
- Complete the sale as usual (payment, invoice, WhatsApp). The bill is stored like any other: Sales
  history, bill register, item-wise sales (as a separate "· manual bill" row, since its quantity is per
  typed unit), returns. Cost is unknown, so it adds to revenue but not to profit.

## Park and resume

A cart can be **parked** and picked up later. Resuming is claim-once; the sale can be
released back to the pool or discarded.


## Receipt


## Invoice studio (at completion)

When a bill is recorded, the **invoice studio** opens with the saved invoice rendered by
the invoice kit v2 engine — all figures mapped from the database, never recalculated in
the browser. The studio offers the paper sizes **A4**, **A2**, A5, Letter, 80 mm and
58 mm receipts, toggles for the expiry column and a black-and-white (thermal) version,
and a **Print / Save PDF** action that opens an isolated print-ready document at the
selected physical size, with repeated table headers and `Page 1 of N` numbering on long
bills (A2 prints as 420 × 594 mm).

The bill follows the reference wholesale layout: a **logo-only** header, a three-panel
party strip (pharmacy | buyer | invoice details), a dense bordered item table, and a
single full-width **horizontal totals table** (subtotal, discount, net amount, received)
with the line-item count and amount in words below. There is no invoice title, terms,
payment, tax, round-off, balance or signature block. Line items show **article ID** and
**MRP** with HSN, pack and manufacturer columns appearing automatically only when the
catalogue carries them (no rate, batch or free columns).

Line items show **article ID** (no batch, no free), with HSN, pack and manufacturer
columns appearing automatically only when the catalogue carries them. There is **no GST
column, no tax table and no customer tax** — the pharmacy is not charging GST to the
customer, so GST is never posted against the bill; only the pharmacy's own **GSTIN /
Drug Licence** print in the header.

The header is driven entirely by **Settings → Pharmacy profile**: logo, full address,
phone, email, website and the GST / Drug License numbers (each shown only when its
checkbox is ticked). The footer carries no bank details, terms or payment line — just the
quantity/amount-words/totals boxes. The invoice-wide discount and voucher
are allocated across line items so each line's rate × quantity − saving equals its line
total, and subtotal, discount, round-off, net amount, received and balance always
balance. The mapping lives in `app/services/invoice_kit.py` and is exposed read-only at
`GET /api/sales/{id}/invoice-view`.

Invoices written after the studio upgrade are stamped `invoice_format = STUDIO` and open
in this same studio view from **Sales History**; invoices recorded before the upgrade
stay on their original layout. So new bills reflect the new design while historical
records are untouched.

## Refunds

A completed invoice can be refunded with per-line quantity, a reason and a disposition
(restock / damage / expired / no restock). Refunds beyond a configured value require

## Technical notes

- The active cart is persisted server-side as a draft per workstation, so a refresh or
  crash never loses an in-progress bill.
- Sale creation is idempotent via `client_request_id`.
- Line cost (`cost_rate`) is captured at sale time for accurate profit reporting.

## Limitations

- Split payments are shown as planned and are not implemented.
- Receipt printing uses the local OS printer backend; it is unavailable in the static
  browser demo.
