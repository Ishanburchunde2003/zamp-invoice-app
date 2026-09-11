# Invoice Processing Automation — PS-1 Case Study

An automated pipeline that takes a vendor invoice (PDF, digital or scanned),
extracts the key fields, matches it against a purchase order database, and
produces a reasoned decision: **approved / flagged / rejected**.

## Architecture

```
Invoice input (PDF) → Extraction (text or OCR) → Validation →
PO matching → Decision → Dashboard (live run view + history)
```

- `extraction.py` — pulls structured fields out of a PDF. Tries direct text
  extraction first (pdfplumber); falls back to OCR (pytesseract + pdf2image)
  if the PDF is a scanned image.
- `matching.py` — matches the extracted invoice against an in-memory PO
  database, checks vendor approval, amount tolerance (including split-PO
  balances), and duplicate submissions. Returns a decision plus a
  plain-English reasoning trail.
- `app.py` — Flask web app: upload page, animated live-run pipeline view,
  results page, and a history dashboard (persisted to a local JSON file).

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
pip install flask

# System dependencies for OCR:
#   macOS:   brew install tesseract poppler
#   Ubuntu:  sudo apt install tesseract-ocr poppler-utils

python app.py
# Open http://127.0.0.1:5000
```

## Test invoices (`test_invoices/`)

| File | Scenario | Expected decision |
|---|---|---|
| `happy_path_invoice.pdf` | Clean invoice, exact PO match | **Approved** |
| `edge1_scanned_invoice.pdf` | Scanned (image-only) PDF — forces OCR | **Flagged** (OCR results always get a human-review flag) |
| `edge2_unapproved_vendor.pdf` | Vendor not on the approved list | **Rejected** |
| `edge3_amount_variance.pdf` | Invoice amount is 26.7% over the PO's remaining balance | **Flagged** |
| `edge4_duplicate_original.pdf` | Upload it once, then upload it again | 1st: **Approved**, 2nd: **Rejected** (duplicate) |

## A bug we found and fixed while testing

While running the edge cases through the actual app (not just unit-testing
the parser functions), we noticed invoice totals were coming back truncated
— `$4050.00` was being read as `405.00`. The regex used to pull monetary
values capped the integer part at 3 digits with no fallback for larger
numbers that don't use a thousands separator (a very common real-world
invoice format). The same capped pattern was silently dropping whole line
items whenever an amount was ≥ 1000. We rewrote the money-matching regex to
capture the full digit blob regardless of length, and added support for
3-letter currency codes (e.g. `EUR 9,500.00`) appearing before the amount.

We also found the extractor's routine note about defaulting `$` to USD was
being treated as a "low confidence" signal by the matching engine, which
meant every single USD invoice — even a perfect one — was being flagged for
human review. We narrowed the matching engine's low-confidence keyword list
so it only reacts to genuine extraction problems (missing/failed/ambiguous
fields), not this common, low-risk default.

Both fixes are a good example of why the happy path alone isn't enough to
trust a build — the edge cases surfaced real defects that a clean demo
invoice never would have.


TRY - https://invoice-processor-qric.onrender.com/
TO TEST FILE - GO AND DOWNLODE FILE FROM test_invoices FOLDER

