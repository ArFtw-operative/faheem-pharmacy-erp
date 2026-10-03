# Dependencies of the purchase processing engine

Policy: open source with permissive licences only (MIT, BSD, Apache-2.0, MPL-2.0); no GPL/AGPL;
everything local, offline, CPU only, Linux and Windows; fewer dependencies where the
standard library or the existing stack can do the job. Checked October 2026 from each
project's own package metadata (`pip show`, PyPI project page).

## Adopted

| Package | Version | Licence | Used for | Linux / Windows | Notes |
|---|---|---|---|---|---|
| Python `decimal`, `hashlib`, `re`, `csv`, `subprocess` | stdlib | PSF | money and tolerances, SHA-256 file/line hashes, parsing, CSV, running Tesseract | yes / yes | no floats for money |
| RapidFuzz | 3.14.6 (existing) | MIT | product name scoring for suggestions and the normalised-attribute matcher | wheels for both | already in the stack |
| pdfplumber / pdfminer.six | 0.11.10 / 20260107 (existing) | MIT | text-PDF tables (existing readers) | yes / yes | |
| pypdfium2 | 5.13.0 (existing, via pdfplumber) | Apache-2.0 / BSD-3 | renders scanned PDF pages to images for OCR | wheels for both | no new install |
| Pillow | 12.3.0 (existing) | MIT-CMU (HPND) | image clean-up before OCR: greyscale, autocontrast, upscale, threshold | yes / yes | used instead of OpenCV (see below) |
| Tesseract OCR (CLI) | 5.5.0 (bundled in `tools/tesseract`) | Apache-2.0 | OCR with word boxes (TSV output) for scanned PDFs and photos | Linux build bundled; Windows: the UB-Mannheim build of the same version | called through `subprocess`; loaded only for scans |
| openpyxl / xlrd | 3.1.5 / 2.0.1 (existing) | MIT / BSD | XLSX / XLS | yes / yes | |
| charset-normalizer | 3.5.1 (existing) | MIT | encoding of odd CSV files | yes / yes | |
| SQLite FTS5 | bundled with Python's sqlite3 | public domain | product shortlist (existing `items_fts`) | yes / yes | PostgreSQL uses indexed `LIKE` prefixes |
| Hypothesis | 6.168.3 (tests only) | MPL-2.0 | property tests of the packaging parser | yes / yes | dependency `sortedcontainers` 2.4.0, Apache-2.0 |

## Evaluated and not adopted

| Package | Licence | Why not |
|---|---|---|
| DuckDB (`read_csv`, `sniff_csv`) | MIT | The existing reader already detects delimiter, quoting, header row and value types and learns each supplier's layout; DuckDB would add ~40 MB for no measured gain on the corpus. |
| Camelot | MIT | Needs Ghostscript (AGPL) or OpenCV for lattice mode; the three existing PDF readers already pass the corpus. |
| OpenCV | Apache-2.0 | Large binary; Pillow plus Tesseract's own layout analysis reconstructs the receipt in the corpus. Can be added behind `ocr.clean_image` if skewed photos become common. |
| pytesseract | Apache-2.0 | A thin wrapper; calling the Tesseract CLI with TSV output directly needs no package. |
| PaddleOCR | Apache-2.0 | Heavy (deep-learning runtime); only to be evaluated if Tesseract fails a clear-scan corpus. |
| Docling | MIT | Heavy document converter; the text-PDF corpus passes without it. |
| Biip (GS1) | Apache-2.0 | Scanners hand the ERP plain barcode text; a small GS1 element parser (AI 01/10/17/21) in `barcode_service.py` covers what purchase lines need. |
| zxing-cpp | Apache-2.0 | Barcodes are not read from images in this workflow. |
| invoice2data | unclear in some listings | Reference only; `supplier_invoice_profiles` implements the per-supplier template idea. |

## Licence conflict to resolve (pre-existing)

| Package | Version | Licence | Where | Status |
|---|---|---|---|---|
| PyMuPDF (`pymupdf`) | 1.28.2 | AGPL-3.0 (or Artifex commercial) | `purchase_import._parse_pdf`, `pdf_invoice` (PDF invoice readers), `invoice_premium` (invoice PDF rendering) | In the repository before this work. Not used by any new code. Needs an owner decision: confirm a commercial licence, or port these modules to pdfplumber/pypdfium2 behind the corpus tests. See DECISIONS.md D5. |
