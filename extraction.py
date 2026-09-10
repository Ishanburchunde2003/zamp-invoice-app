"""
extraction.py — Invoice PDF data extraction module.

Strategy:
  1. Attempt direct text extraction via pdfplumber (fast, accurate for native PDFs).
  2. If text is empty / near-empty (scanned image PDF), fall back to OCR via
     pytesseract + pdf2image.
  3. Parse the raw text with regex heuristics to pull out structured invoice fields.
  4. Return a well-defined dict that always conforms to the output schema, even on
     errors.

Author: Assistant
"""

import re
import sys
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy / guarded imports so we can give clear errors instead of crashing
# ---------------------------------------------------------------------------

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore[assignment]

try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore[assignment]

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# If pdfplumber returns fewer than this many non-whitespace characters we
# consider the PDF to be image-based and fall back to OCR.
MIN_TEXT_LENGTH = 50

# Common currency symbols / codes we look for.  Ordered so that more specific
# patterns are tried first.
CURRENCY_PATTERNS: List[Tuple[str, str]] = [
    (r"\bUSD\b", "USD"),
    (r"\bEUR\b", "EUR"),
    (r"\bGBP\b", "GBP"),
    (r"\bCAD\b", "CAD"),
    (r"\bAUD\b", "AUD"),
    (r"\bJPY\b", "JPY"),
    (r"\bCHF\b", "CHF"),
    (r"\bINR\b", "INR"),
    (r"\bCNY\b", "CNY"),
    (r"\bRMB\b", "CNY"),
    (r"\$", "USD"),   # Default dollar sign → USD (ambiguous, noted)
    (r"€", "EUR"),
    (r"£", "GBP"),
    (r"¥", "JPY"),
    (r"₹", "INR"),
    (r"₣", "CHF"),
]

# ---------------------------------------------------------------------------
# Helpers — text extraction
# ---------------------------------------------------------------------------


def _extract_text_pdfplumber(pdf_path: str) -> str:
    """Return concatenated page text from *pdfplumber*.  May return ''."""
    if pdfplumber is None:
        raise ImportError("pdfplumber is not installed")
    text_parts: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def _extract_text_ocr(pdf_path: str, dpi: int = 300) -> str:
    """Convert each page to an image then OCR with *pytesseract*."""
    if pytesseract is None:
        raise ImportError("pytesseract is not installed")
    if convert_from_path is None:
        raise ImportError("pdf2image is not installed")

    images = convert_from_path(pdf_path, dpi=dpi)
    text_parts: List[str] = []
    for i, img in enumerate(images):
        page_text: str = pytesseract.image_to_string(img)
        text_parts.append(page_text)
    return "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Helpers — field parsing
# ---------------------------------------------------------------------------


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip and collapse whitespace; return None if empty."""
    if value is None:
        return None
    value = " ".join(value.split())
    return value if value else None


def _to_float(value: Optional[str]) -> Optional[float]:
    """Best-effort conversion of a money/number string to float.

    Handles:
      - comma as thousands separator  e.g. "1,234.56"
      - European comma decimal         e.g. "1.234,56"
      - leading currency symbols        e.g. "$1234.56"
    """
    if value is None:
        return None
    # Strip currency symbols / whitespace
    value = re.sub(r"[^\d,.\-]", "", value).strip()
    if not value:
        return None

    # Decide comma role: if pattern like "1.234,56" → European
    if re.search(r"\d\.\d{3},\d{1,2}$", value):
        value = value.replace(".", "").replace(",", ".")
    else:
        # Treat commas as thousands separators
        value = value.replace(",", "")

    try:
        return float(value)
    except ValueError:
        return None


def _normalise_date(raw: str) -> Optional[str]:
    """Try many common date formats and return YYYY-MM-DD or None.

    Heuristic: we attempt the most unambiguous formats first (those with month
    names), then numeric formats with descending likelihood.
    """
    raw = raw.strip().rstrip(".")

    # Formats to try — order matters (first match wins).
    formats = [
        # Month name variants
        "%B %d, %Y",       # January 15, 2024
        "%b %d, %Y",       # Jan 15, 2024
        "%d %B %Y",        # 15 January 2024
        "%d %b %Y",        # 15 Jan 2024
        "%B %d %Y",        # January 15 2024
        "%b %d %Y",        # Jan 15 2024
        # ISO
        "%Y-%m-%d",        # 2024-01-15
        # Common US
        "%m/%d/%Y",        # 01/15/2024
        "%m-%d-%Y",        # 01-15-2024
        # Common EU / international
        "%d/%m/%Y",        # 15/01/2024
        "%d-%m-%Y",        # 15-01-2024
        "%d.%m.%Y",        # 15.01.2024
        # Two-digit year
        "%m/%d/%y",        # 01/15/24
        "%d/%m/%y",        # 15/01/24
        "%Y/%m/%d",        # 2024/01/15
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            # Sanity: year should be between 1990 and 2099
            if 1990 <= dt.year <= 2099:
                return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ---- Vendor name -----------------------------------------------------------
# Heuristic: the vendor name is typically the very first non-empty line of the
# invoice, OR the line immediately following a "Bill From" / "From:" / "Vendor:"
# / "Supplier:" label.  We try the labelled approach first, then fall back to
# the first-line approach.

def _parse_vendor_name(text: str) -> Optional[str]:
    # Try labelled patterns (case-insensitive)
    patterns = [
        r"(?:bill\s*from|from|vendor|supplier|company)\s*[:]\s*(.+)",
        r"(?:sold\s*by)\s*[:]\s*(.+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            candidate = _clean(m.group(1))
            if candidate and len(candidate) > 1:
                return candidate

    # Fallback: first non-blank line that is NOT a common header keyword
    skip_words = {"invoice", "tax invoice", "credit note", "statement", "receipt",
                  "proforma", "quote", "estimate", "purchase order", "page"}
    for line in text.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        if line_clean.lower() in skip_words:
            continue
        # Skip lines that look like dates, numbers-only, or very short
        if re.fullmatch(r"[\d/\-.\s]+", line_clean):
            continue
        if len(line_clean) < 3:
            continue
        return _clean(line_clean)

    return None


# ---- Invoice number --------------------------------------------------------
# Heuristic: look for labels such as "Invoice #", "Invoice No", "Invoice Number",
# "Inv#", etc., followed by an alphanumeric identifier.

def _parse_invoice_number(text: str) -> Optional[str]:
    patterns = [
        r"(?:invoice|inv)\s*(?:#|no\.?|number|num)\s*[:\s]*([A-Za-z0-9\-/]+)",
        r"(?:invoice)\s*[:\s]+([A-Za-z0-9\-/]+)",
        r"(?:inv)\s*[#:]\s*([A-Za-z0-9\-/]+)",
        # Some invoices just have "No." or "Number:"
        r"\b(?:number|no\.?)\s*[:\s]*(\d[\dA-Za-z\-/]{2,})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _clean(m.group(1))
    return None


# ---- Invoice date ----------------------------------------------------------
# Heuristic: look for labels like "Invoice Date", "Date:", "Dated", then grab
# the date string that follows.  Also try standalone date strings.

def _parse_invoice_date(text: str) -> Optional[str]:
    # Labelled patterns first
    labelled = [
        r"(?:invoice\s*date|date\s*of\s*invoice|inv\.?\s*date|date)\s*[:\s]+([A-Za-z0-9,./\- ]+)",
        r"(?:dated)\s*[:\s]+([A-Za-z0-9,./\- ]+)",
    ]
    for pat in labelled:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            normalised = _normalise_date(m.group(1))
            if normalised:
                return normalised

    # Unlabelled: find any date-like string in the first 30 lines
    date_re = re.compile(
        r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})\b"
        r"|"
        r"\b(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})\b"
        r"|"
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{2,4})\b"
        r"|"
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4})\b",
        re.IGNORECASE,
    )
    for line in text.splitlines()[:30]:
        for match in date_re.finditer(line):
            raw = next(g for g in match.groups() if g is not None)
            normalised = _normalise_date(raw)
            if normalised:
                return normalised
    return None


# ---- PO number -------------------------------------------------------------
# Heuristic: labels such as "PO#", "PO Number", "Purchase Order", "P.O."

def _parse_po_number(text: str) -> Optional[str]:
    patterns = [
        r"(?:purchase\s*order|p\.?\s*o\.?)\s*(?:#|no\.?|number|num)?\s*[:\s]*([A-Za-z0-9\-/]+)",
        r"\bPO\s*[#:]\s*([A-Za-z0-9\-/]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _clean(m.group(1))
    return None


# ---- Currency --------------------------------------------------------------
# Heuristic: scan the text for known currency codes / symbols.  If $ is found
# without an explicit code, default to USD and note ambiguity.

def _parse_currency(text: str, notes: List[str]) -> str:
    for pat, code in CURRENCY_PATTERNS:
        if re.search(pat, text):
            if pat == r"\$":
                notes.append(
                    "Currency inferred as USD from '$' symbol — may be CAD/AUD/etc."
                )
            return code
    notes.append("No currency symbol or code detected — defaulting to USD.")
    return "USD"


# ---- Line items ------------------------------------------------------------
# Heuristic: we look for tabular rows that contain at least a quantity, a unit
# price and an amount (all numbers), separated by whitespace or tab.  The
# leading text is taken as the description.
#
# Typical row:  "Widget 10mm   2   15.00   30.00"
#
# We try two strategies:
#   A) A greedy regex that captures desc, qty, unit_price, amount from right.
#   B) pdfplumber table extraction (already done upstream if available).
#
# Because tables vary wildly we err on the side of capturing *something* and
# flag uncertainties in confidence_notes.

_LINE_ITEM_RE = re.compile(
    r"^"
    r"(?P<description>.+?)"          # Description: everything up to the numbers
    r"\s+"
    r"(?P<qty>\d+(?:[.,]\d+)?)"      # Quantity
    r"\s+"
    r"(?P<unit_price>\d+(?:[,.]\d+)*)"  # Unit price — digits, comma/dot groups of any length
    r"\s+"
    r"(?P<amount>\d+(?:[,.]\d+)*)"      # Line amount — same, so 4+ digit amounts aren't truncated
    r"\s*$"
)

# Alternate pattern where amount comes right after description (qty & up might be missing)
_LINE_ITEM_SIMPLE_RE = re.compile(
    r"^"
    r"(?P<description>.+?)"
    r"\s{2,}"                         # At least two spaces (crude column sep)
    r"(?P<amount>\$?\s*\d+(?:[,.]\d+)*)"
    r"\s*$"
)


def _parse_line_items(text: str, notes: List[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    # ---- Strategy A: full four-column rows ---------------------------------
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_ITEM_RE.match(line)
        if m:
            desc = _clean(m.group("description"))
            qty = _to_float(m.group("qty"))
            unit_price = _to_float(m.group("unit_price"))
            amount = _to_float(m.group("amount"))
            if desc and amount is not None:
                items.append({
                    "description": desc,
                    "qty": qty if qty is not None else 1.0,
                    "unit_price": unit_price if unit_price is not None else (
                        amount  # single item assumed
                    ),
                    "amount": amount,
                })

    if items:
        return items

    # ---- Strategy B: two-column (description + amount) ---------------------
    # Fall back if Strategy A found nothing.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_ITEM_SIMPLE_RE.match(line)
        if m:
            desc = _clean(m.group("description"))
            amount = _to_float(m.group("amount"))
            # Skip lines that are clearly totals/subtotals/tax
            if desc and amount is not None:
                lower_desc = desc.lower()
                if any(kw in lower_desc for kw in (
                    "total", "subtotal", "sub-total", "tax", "vat", "gst",
                    "discount", "shipping", "freight", "balance due",
                    "amount due", "payment", "deposit",
                )):
                    continue
                items.append({
                    "description": desc,
                    "qty": 1.0,
                    "unit_price": amount,
                    "amount": amount,
                })

    if not items:
        notes.append("Could not parse any line items from the invoice text.")
    else:
        notes.append(
            "Line items parsed with simple heuristic — qty/unit_price may be inaccurate."
        )

    return items


# ---- Monetary totals -------------------------------------------------------
# Heuristic: look for labels like "Subtotal", "Tax", "Total", "Amount Due",
# "Balance Due", etc., then grab the number immediately following.

_MONEY_RE = (
    r"(?:[\$€£¥₹]|\b(?:USD|EUR|GBP|INR|CAD|AUD|JPY|CHF|CNY)\b)?"
    r"\s*(\d+(?:[,.]\d+)*)"
)


def _parse_monetary(text: str, label_patterns: List[str]) -> Optional[float]:
    for pat in label_patterns:
        full = pat + r"\s*[:\s]*" + _MONEY_RE
        m = re.search(full, text, re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _parse_subtotal(text: str) -> Optional[float]:
    return _parse_monetary(text, [
        r"sub[\s\-]?total",
        r"subtotal",
    ])


def _parse_tax(text: str) -> Optional[float]:
    return _parse_monetary(text, [
        r"(?:sales\s*)?tax",
        r"vat",
        r"gst",
        r"hst",
    ])


def _parse_total(text: str) -> Optional[float]:
    """Parse the grand total / amount due.

    We look for "Total" last (after subtotal/tax) to avoid accidentally
    grabbing the subtotal.  We prefer "Amount Due" / "Balance Due" / "Grand
    Total" which are more explicit.
    """
    return _parse_monetary(text, [
        r"(?:total\s*)?amount\s*due",
        r"balance\s*due",
        r"grand\s*total",
        r"invoice\s*total",
        r"(?:total\s*amount)",
        r"\btotal\b",
    ])


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------


def _empty_result(**overrides: Any) -> Dict[str, Any]:
    """Return the canonical result dict with safe defaults."""
    result: Dict[str, Any] = {
        "vendor_name": None,
        "invoice_number": None,
        "invoice_date": None,
        "po_number": None,
        "line_items": [],
        "subtotal": None,
        "tax": None,
        "total_amount": None,
        "currency": "USD",
        "extraction_method": "text",
        "confidence_notes": [],
        "raw_text": "",
    }
    result.update(overrides)
    return result


def extract_invoice(pdf_path: str) -> Dict[str, Any]:
    """Extract structured invoice data from a PDF file.

    Parameters
    ----------
    pdf_path : str
        Filesystem path to the PDF invoice.

    Returns
    -------
    dict
        Always returns a dict matching the documented schema, even on errors.
    """

    notes: List[str] = []
    raw_text = ""
    method = "text"

    # ------------------------------------------------------------------
    # Step 1 — Try direct text extraction with pdfplumber
    # ------------------------------------------------------------------
    try:
        raw_text = _extract_text_pdfplumber(pdf_path)
    except ImportError:
        notes.append("pdfplumber not installed — skipping direct text extraction.")
        raw_text = ""
    except Exception as exc:  # noqa: BLE001
        notes.append(f"pdfplumber extraction failed: {exc}")
        raw_text = ""

    # ------------------------------------------------------------------
    # Step 2 — Fall back to OCR if text is empty / near-empty
    # ------------------------------------------------------------------
    stripped = re.sub(r"\s+", "", raw_text)
    if len(stripped) < MIN_TEXT_LENGTH:
        if raw_text:
            notes.append(
                f"Direct text extraction yielded only {len(stripped)} chars — "
                "falling back to OCR."
            )
        method = "ocr"
        try:
            raw_text = _extract_text_ocr(pdf_path)
        except ImportError as exc:
            notes.append(f"OCR dependency missing: {exc}")
            return _empty_result(
                extraction_method=method,
                confidence_notes=notes + [
                    "Could not extract any text from the PDF."
                ],
                raw_text=raw_text,
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"OCR extraction failed: {exc}")
            return _empty_result(
                extraction_method=method,
                confidence_notes=notes + [
                    "Could not extract any text from the PDF."
                ],
                raw_text=raw_text,
            )

    # Final check: if we still have nothing useful, bail out gracefully.
    stripped = re.sub(r"\s+", "", raw_text)
    if len(stripped) < MIN_TEXT_LENGTH:
        notes.append(
            f"Total extracted text is only {len(stripped)} chars — "
            "parsing will likely be incomplete."
        )

    # ------------------------------------------------------------------
    # Step 3 — Parse individual fields
    # ------------------------------------------------------------------

    vendor_name = _parse_vendor_name(raw_text)
    if vendor_name is None:
        notes.append("Could not determine vendor name.")

    invoice_number = _parse_invoice_number(raw_text)
    if invoice_number is None:
        notes.append("Could not determine invoice number.")

    invoice_date = _parse_invoice_date(raw_text)
    if invoice_date is None:
        notes.append("Could not determine invoice date.")

    po_number = _parse_po_number(raw_text)
    if po_number is None:
        notes.append("No PO number found (may not be present on invoice).")

    currency = _parse_currency(raw_text, notes)

    line_items = _parse_line_items(raw_text, notes)

    subtotal = _parse_subtotal(raw_text)
    tax = _parse_tax(raw_text)
    total_amount = _parse_total(raw_text)

    # Cross-check: if total is missing but subtotal+tax are present, compute it.
    if total_amount is None and subtotal is not None and tax is not None:
        total_amount = round(subtotal + tax, 2)
        notes.append(
            "total_amount was computed as subtotal + tax (not found explicitly)."
        )

    # Cross-check: if subtotal is missing but line items are present, sum them.
    if subtotal is None and line_items:
        subtotal = round(sum(item["amount"] for item in line_items), 2)
        notes.append(
            "subtotal was computed as sum of line item amounts."
        )

    return {
        "vendor_name": vendor_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "po_number": po_number,
        "line_items": line_items,
        "subtotal": subtotal,
        "tax": tax,
        "total_amount": total_amount,
        "currency": currency,
        "extraction_method": method,
        "confidence_notes": notes,
        "raw_text": raw_text,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extraction.py <path-to-invoice.pdf>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    result = extract_invoice(pdf_path)

    # Pretty-print as JSON (raw_text can be large; still included per spec)
    print(json.dumps(result, indent=2, ensure_ascii=False))
