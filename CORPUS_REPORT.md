# Purchase corpus report

Synthetic, anonymised corpus (`tests/purchase_corpus.py`): 10 invoices from 5 suppliers, 289 lines. OCR engine available: yes.

## Straight-through on recurring invoices

| Route | Target | Known-product lines | Straight-through (known) | All lines incl. traps | Wrong auto-accepts |
|---|---|---|---|---|---|
| Known supplier, CSV / XLSX | 98.0% | 134 | 100.0% | 97.1% | 0 |
| Known supplier, born-digital PDF | 95.0% | 4 | 100.0% | 100.0% | 0 |
| Known supplier, clear scan / photo | 90.0% | 4 | 100.0% | 100.0% | 0 |

**Wrong auto-accepts on the whole corpus: 0.**

**Lines arriving counted: 100.0%** of 289; 32 counted by a proposal (a person posts them), of which 0 wrong.

## Per invoice

| Invoice | Route | First / recurring | Lines | Auto | Warning | Proposed | Review | Blocked | Counted | Product right | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| NR03895 | STRUCTURED | first | 9 | 4 | 0 | 5 | 0 | 0 | 9 | 9 | 154 |
| NR03897 | STRUCTURED | recurring | 14 | 5 | 5 | 4 | 0 | 0 | 14 | 14 | 154 |
| CS/26/0411 | STRUCTURED | first | 5 | 2 | 0 | 3 | 0 | 0 | 5 | 5 | 54 |
| CS/26/0502 | STRUCTURED | recurring | 5 | 4 | 1 | 0 | 0 | 0 | 5 | 5 | 43 |
| DPD-7731 | PDF_TEXT | first | 4 | 4 | 0 | 0 | 0 | 0 | 4 | 4 | 155 |
| DPD-7790 | PDF_TEXT | recurring | 4 | 4 | 0 | 0 | 0 | 0 | 4 | 4 | 55 |
| GH-0041 | OCR | first | 4 | 0 | 3 | 1 | 0 | 0 | 4 | 4 | 219 |
| GH-0042 | OCR | recurring | 4 | 0 | 4 | 0 | 0 | 0 | 4 | 4 | 230 |
| EV/1001 | STRUCTURED | first | 120 | 101 | 0 | 19 | 0 | 0 | 120 | 120 | 1232 |
| EV/1088 | STRUCTURED | recurring | 120 | 101 | 19 | 0 | 0 | 0 | 120 | 120 | 903 |

Proposed lines on recurring invoices are the deliberate traps — a strength the catalogue does not carry (5 mg against 10 mg), a missing release marker (plain against SR), an unreadable pack (`10ML57`), a product never stocked: each arrives counted as a proposed new product for a person to check, never auto-accepted. On first invoices, what a person posts teaches the supplier, which is what makes the recurring ones pass.

## Resource use

CPU time 4.98 s (including the OCR engine), wall time 4.54 s for the whole corpus, peak memory 151.9 MB (Python process, including the app). OCR runs only for scans; nothing runs while idle. No GPU, no network.
