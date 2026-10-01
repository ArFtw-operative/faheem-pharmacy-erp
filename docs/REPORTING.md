# Reporting


## Overview

Reporting turns the day-to-day data into sales, profit, purchasing and operational
figures, with period selection and export.

## Capabilities

- **Sales summary and trends** — totals, bill counts and daily series.
- **Profit & loss** — revenue, cost of goods sold (from the cost captured at sale
  time), gross profit and margin.
- **Breakdowns** — sales by category, brand and item; **Walk-In vs Home Delivery**
  customer mix; purchases by agency; returns and adjustments summaries.
- **Periods** — today, week, month or year, computed in the pharmacy's business
  timezone (so the business day is correct regardless of the server clock).
- **Export** — CSV / Excel / PDF where applicable (the customer-mix breakdown exports
  as `channel.csv`).

## Counter sales report

The counter module produces a daily sales report (category breakdown, payment mix and

## ERP document format

Every generated report is also rendered as an ERP text document (centred
pharmacy name and title, period, dashed rules, SNo, right-aligned figures,
TOTAL line). It is what the Document view (F6), printing, the Text download
and the PDF show. Cost and profit columns appear only when a user with
financial rights selects them; an unknown cost prints as "—".

## Technical notes

- Profit uses `SaleItem.cost_rate`, captured when the sale is made, so re-pricing a
  batch later never rewrites history.
- Returns are valued at MRP for the profit widget; restocked returns recover cost and
  non-restocked returns plus loose/damage adjustments are treated as write-offs.
