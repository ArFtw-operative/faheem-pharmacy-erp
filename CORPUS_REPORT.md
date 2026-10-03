# Purchase corpus report

Synthetic, anonymised corpus (`tests/purchase_corpus.py`): 10 invoices from 5 suppliers, 289 lines. OCR engine available: yes.

## Straight-through on recurring invoices

| Route | Target | Known-product lines | Straight-through (known) | All lines incl. traps | Wrong auto-accepts |
|---|---|---|---|---|---|
| Known supplier, CSV / XLSX | 98.0% | 134 | 100.0% | 97.1% | 0 |
| Known supplier, born-digital PDF | 95.0% | 4 | 100.0% | 100.0% | 0 |
| Known supplier, clear scan / photo | 90.0% | 4 | 100.0% | 100.0% | 0 |

**Wrong auto-accepts on the whole corpus: 0.**

## Per invoice

| Invoice | Route | First / recurring | Lines | Auto | Warning | Review | Blocked | Packs resolved | Product right | ms |
|---|---|---|---|---|---|---|---|---|---|---|
| NR03895 | STRUCTURED | first | 9 | 4 | 0 | 5 | 0 | 4 | 9 | 103 |
| NR03897 | STRUCTURED | recurring | 14 | 10 | 0 | 4 | 0 | 12 | 14 | 121 |
| CS/26/0411 | STRUCTURED | first | 5 | 1 | 0 | 4 | 0 | 2 | 2 | 52 |
| CS/26/0502 | STRUCTURED | recurring | 5 | 5 | 0 | 0 | 0 | 5 | 5 | 31 |
| DPD-7731 | PDF_TEXT | first | 4 | 4 | 0 | 0 | 0 | 4 | 4 | 138 |
| DPD-7790 | PDF_TEXT | recurring | 4 | 4 | 0 | 0 | 0 | 4 | 4 | 46 |
| GH-0041 | OCR | first | 4 | 0 | 3 | 1 | 0 | 3 | 3 | 195 |
| GH-0042 | OCR | recurring | 4 | 0 | 4 | 0 | 0 | 4 | 4 | 185 |
| EV/1001 | STRUCTURED | first | 120 | 101 | 0 | 19 | 0 | 101 | 120 | 921 |
| EV/1088 | STRUCTURED | recurring | 120 | 120 | 0 | 0 | 0 | 120 | 120 | 728 |

Lines left for a person on recurring invoices are the deliberate traps: a strength the catalogue does not carry (5 mg against 10 mg), a missing release marker (plain against SR), an unreadable pack (`10ML57`) and a product the pharmacy has never stocked. First invoices from a supplier go to review until a person confirms products and packs once; that is what makes the recurring ones pass.

## Resource use

CPU time 4.27 s (including the OCR engine), wall time 3.87 s for the whole corpus, peak memory 150.3 MB (Python process, including the app). OCR runs only for scans; nothing runs while idle. No GPU, no network.
