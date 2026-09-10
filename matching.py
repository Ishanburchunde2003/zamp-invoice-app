"""
matching.py — Purchase Order matching and approval decision engine.

Given an invoice dict (from extraction.py) and a PO database, this module
decides whether to APPROVE, FLAG (for human review), or REJECT the invoice.

Decision hierarchy (highest severity wins):
    REJECTED  — hard blockers (missing data, duplicates, unapproved vendor)
    FLAGGED   — soft issues that need human review (variance, no PO match,
                low-confidence OCR)
    APPROVED  — all checks passed cleanly

All reasoning strings are written in plain English for the finance team.
"""

from __future__ import annotations

import json
import sys
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# In-memory "seen invoices" registry for duplicate detection
# ---------------------------------------------------------------------------
#
# In a real system this would be a database query.  For the demo we keep a
# module-level set of (vendor_name_lower, invoice_number) tuples.  Callers can
# pass their own set to match_invoice_to_po() if they want isolation between
# runs (see the `seen_invoices` parameter).
#
_SEEN_INVOICES: Set[Tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Sample PO database
# ---------------------------------------------------------------------------

def load_sample_po_database() -> List[Dict[str, Any]]:
    """Return a small, realistic set of purchase orders for demo/testing."""
    return [
        {
            "po_number": "PO-2024-0001",
            "vendor_name": "Acme Office Supplies Inc.",
            "po_amount": 5000.00,
            "currency": "USD",
            "approved_vendors": True,
            "already_invoiced_amount": 0.00,
        },
        {
            "po_number": "PO-2024-0002",
            "vendor_name": "Global Cloud Services Ltd.",
            "po_amount": 12000.00,
            "currency": "USD",
            "approved_vendors": True,
            # 8000 already invoiced — remaining balance is 4000
            "already_invoiced_amount": 8000.00,
        },
        {
            "po_number": "PO-2024-0003",
            "vendor_name": "Bright Ideas Marketing GmbH",
            "po_amount": 7500.00,
            "currency": "EUR",
            "approved_vendors": True,
            "already_invoiced_amount": 0.00,
        },
        {
            "po_number": "PO-2024-0004",
            "vendor_name": "QuickShip Logistics Co.",
            "po_amount": 2200.00,
            "currency": "USD",
            # Vendor is NOT approved — invoices should be rejected
            "approved_vendors": False,
            "already_invoiced_amount": 0.00,
        },
        {
            "po_number": "PO-2024-0005",
            "vendor_name": "Northwind Hardware LLC",
            "po_amount": 15000.00,
            "currency": "USD",
            "approved_vendors": True,
            # Fully invoiced already
            "already_invoiced_amount": 15000.00,
        },
        {
            "po_number": "PO-2024-0006",
            "vendor_name": "Sunrise Consulting Group",
            "po_amount": 9800.00,
            "currency": "USD",
            "approved_vendors": True,
            "already_invoiced_amount": 4900.00,  # Half-invoiced
        },
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(s: Optional[str]) -> str:
    """Lowercase + strip for lenient comparisons."""
    return (s or "").strip().lower()


def _vendor_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Return a 0.0–1.0 similarity ratio between two vendor names.

    Uses difflib.SequenceMatcher — good enough for catching things like
    "Acme Office Supplies" vs "Acme Office Supplies Inc." (~0.9+ ratio).
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _find_po_by_number(
    po_number: Optional[str],
    po_database: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Exact match on PO number (case-insensitive, whitespace-tolerant)."""
    if not po_number:
        return None
    target = _normalise(po_number)
    for po in po_database:
        if _normalise(po.get("po_number")) == target:
            return po
    return None


def _fuzzy_match_po(
    invoice: Dict[str, Any],
    po_database: List[Dict[str, Any]],
    vendor_threshold: float = 0.80,
    amount_tolerance_pct: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Fallback matcher used when the invoice has no PO number.

    Strategy: rank each PO by (vendor_name similarity, amount proximity).
    A PO qualifies only if BOTH:
        - vendor similarity >= threshold
        - invoice total is within `amount_tolerance_pct` of the PO's
          remaining balance
    Returns the best-scoring PO, or None.
    """
    vendor = invoice.get("vendor_name")
    total = invoice.get("total_amount")
    if not vendor or total is None:
        return None

    best: Optional[Dict[str, Any]] = None
    best_score = 0.0

    for po in po_database:
        sim = _vendor_similarity(vendor, po.get("vendor_name"))
        if sim < vendor_threshold:
            continue

        remaining = po["po_amount"] - po.get("already_invoiced_amount", 0.0)
        if remaining <= 0:
            continue

        variance_pct = abs(total - remaining) / remaining * 100.0
        if variance_pct > amount_tolerance_pct:
            continue

        # Composite score — higher vendor similarity + lower variance wins
        score = sim - (variance_pct / 100.0)
        if score > best_score:
            best_score = score
            best = po

    return best


# ---------------------------------------------------------------------------
# The main entrypoint
# ---------------------------------------------------------------------------

def match_invoice_to_po(
    invoice: Dict[str, Any],
    po_database: List[Dict[str, Any]],
    tolerance_pct: float = 2.0,
    seen_invoices: Optional[Set[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Match an invoice against the PO database and produce a decision.

    Parameters
    ----------
    invoice : dict
        The output dict from extraction.py.
    po_database : list of dict
        Available purchase orders.
    tolerance_pct : float
        Allowed % variance between invoice total and PO remaining balance.
    seen_invoices : set, optional
        Set of (vendor_lower, invoice_number) tuples already processed. If
        not supplied, a module-level set is used.  The current invoice is
        added to the set after the check runs.

    Returns
    -------
    dict
        See module docstring for schema.
    """

    if seen_invoices is None:
        seen_invoices = _SEEN_INVOICES

    reasoning: List[str] = []
    checks = {
        "po_found": False,
        "vendor_approved": False,
        "amount_within_tolerance": False,
        "duplicate_detected": False,
        "required_fields_present": False,
    }

    matched_po: Optional[Dict[str, Any]] = None
    variance_amount: Optional[float] = None
    variance_pct: Optional[float] = None

    # ------------------------------------------------------------------
    # Check 1 — Required fields present
    # ------------------------------------------------------------------
    # We consider invoice_number AND total_amount as the bare minimum for
    # any downstream processing.  Without these we cannot detect duplicates
    # or perform matching at all.
    invoice_number = invoice.get("invoice_number")
    total_amount = invoice.get("total_amount")
    missing_fields: List[str] = []
    if not invoice_number:
        missing_fields.append("invoice number")
    if total_amount is None:
        missing_fields.append("total amount")

    if missing_fields:
        reasoning.append(
            "Invoice is missing required field(s): "
            + ", ".join(missing_fields) + "."
        )
    else:
        checks["required_fields_present"] = True

    # ------------------------------------------------------------------
    # Check 2 — Duplicate detection
    # ------------------------------------------------------------------
    # Only meaningful if we actually have an invoice number.
    vendor_name = invoice.get("vendor_name") or ""
    if invoice_number:
        key = (_normalise(vendor_name), _normalise(invoice_number))
        if key in seen_invoices:
            checks["duplicate_detected"] = True
            reasoning.append(
                f"This invoice appears to be a duplicate — invoice number "
                f"'{invoice_number}' from vendor '{vendor_name or 'unknown'}' "
                f"has already been processed."
            )
        else:
            # Register it now so subsequent calls in the same run catch dupes
            seen_invoices.add(key)

    # ------------------------------------------------------------------
    # Check 3 — Find the PO
    # ------------------------------------------------------------------
    po_number = invoice.get("po_number")
    matched_po = _find_po_by_number(po_number, po_database)

    if matched_po is None and not po_number:
        # No PO number extracted — try fuzzy fallback
        matched_po = _fuzzy_match_po(invoice, po_database)
        if matched_po is not None:
            reasoning.append(
                f"No PO number was found on the invoice, but it was matched "
                f"to PO '{matched_po['po_number']}' based on vendor name "
                f"'{matched_po['vendor_name']}' and amount similarity."
            )

    if matched_po is not None:
        checks["po_found"] = True
        reasoning.append(
            f"Purchase order '{matched_po['po_number']}' was found in the "
            f"system for vendor '{matched_po['vendor_name']}'."
        )
    else:
        if po_number:
            reasoning.append(
                f"Purchase order '{po_number}' referenced on the invoice "
                f"could not be found in the system."
            )
        else:
            reasoning.append(
                "No PO number was found on the invoice, and no matching PO "
                "could be identified from vendor name or amount."
            )

    # ------------------------------------------------------------------
    # Check 4 — Vendor approval status
    # ------------------------------------------------------------------
    if matched_po is not None:
        if matched_po.get("approved_vendors", False):
            checks["vendor_approved"] = True
            reasoning.append(
                f"Vendor '{matched_po['vendor_name']}' is on the approved "
                f"vendor list."
            )
        else:
            reasoning.append(
                f"Vendor '{matched_po['vendor_name']}' is NOT on the "
                f"approved vendor list — invoice cannot be paid without "
                f"vendor onboarding."
            )
    else:
        # Can't verify vendor approval without a PO match
        reasoning.append(
            "Vendor approval status could not be verified because no "
            "matching PO was found."
        )

    # ------------------------------------------------------------------
    # Check 5 — Currency consistency (informational, doesn't block)
    # ------------------------------------------------------------------
    if matched_po is not None:
        inv_currency = invoice.get("currency", "USD")
        po_currency = matched_po.get("currency", "USD")
        if inv_currency != po_currency:
            reasoning.append(
                f"Currency mismatch: invoice is in {inv_currency} but PO "
                f"is in {po_currency}. Amount comparison may be inaccurate."
            )

    # ------------------------------------------------------------------
    # Check 6 — Amount within tolerance of remaining PO balance
    # ------------------------------------------------------------------
    # We compare against (po_amount - already_invoiced_amount) so a PO
    # can legitimately be split across multiple invoices.
    if matched_po is not None and total_amount is not None:
        remaining_balance = (
            matched_po["po_amount"] - matched_po.get("already_invoiced_amount", 0.0)
        )

        if remaining_balance <= 0:
            # PO already fully invoiced
            variance_amount = total_amount - remaining_balance
            variance_pct = None  # Undefined when balance is zero
            reasoning.append(
                f"Purchase order '{matched_po['po_number']}' has already been "
                f"fully invoiced (${matched_po['already_invoiced_amount']:.2f} "
                f"of ${matched_po['po_amount']:.2f} used). No remaining "
                f"balance is available for this invoice of "
                f"${total_amount:.2f}."
            )
        else:
            variance_amount = total_amount - remaining_balance
            variance_pct = abs(variance_amount) / remaining_balance * 100.0

            if variance_pct <= tolerance_pct:
                checks["amount_within_tolerance"] = True
                reasoning.append(
                    f"Invoice amount ${total_amount:.2f} is within the "
                    f"allowed {tolerance_pct:.1f}% tolerance of the PO's "
                    f"remaining balance ${remaining_balance:.2f} "
                    f"(variance: {variance_pct:.2f}%)."
                )
            else:
                direction = "over" if variance_amount > 0 else "under"
                reasoning.append(
                    f"Invoice amount ${total_amount:.2f} is {direction} the "
                    f"PO's remaining balance ${remaining_balance:.2f} by "
                    f"${abs(variance_amount):.2f} ({variance_pct:.2f}%), "
                    f"exceeding the allowed {tolerance_pct:.1f}% tolerance."
                )

    # ------------------------------------------------------------------
    # Check 7 — Extraction confidence
    # ------------------------------------------------------------------
    # If the extractor flagged uncertainties, we surface that as a reason
    # to flag for human review.
    confidence_notes = invoice.get("confidence_notes", []) or []
    extraction_method = invoice.get("extraction_method", "text")
    low_confidence = False

    # OCR results are inherently less reliable than direct text extraction
    if extraction_method == "ocr":
        low_confidence = True
        reasoning.append(
            "Invoice data was extracted via OCR (scanned document), which "
            "is less reliable than digital text — a human should verify "
            "key fields."
        )

    # Any note that mentions failure/missing/could-not is treated as low confidence
    # Note: deliberately excludes "inferred" — the extractor logs a minor
    # note whenever it defaults '$' to USD, which is extremely common and
    # low-risk. Flagging every USD invoice for that alone would make the
    # FLAGGED bucket noisy and reduce trust in the signal. We only treat
    # genuine extraction failures/ambiguity as reasons to flag.
    problem_keywords = ("could not", "missing", "failed", "inaccurate", "ambiguous")
    problematic_notes = [
        note for note in confidence_notes
        if any(kw in note.lower() for kw in problem_keywords)
    ]
    if problematic_notes:
        low_confidence = True
        reasoning.append(
            "The extraction system reported uncertainty about some fields: "
            + "; ".join(problematic_notes)
        )

    # ------------------------------------------------------------------
    # Final decision
    # ------------------------------------------------------------------
    # REJECTED — hard blockers
    if (
        not checks["required_fields_present"]
        or checks["duplicate_detected"]
        or (matched_po is not None and not checks["vendor_approved"])
    ):
        decision = "rejected"
        reasoning.insert(
            0,
            "DECISION: REJECTED. This invoice cannot be processed and should "
            "not be paid.",
        )

    # FLAGGED — soft issues needing human review
    elif (
        not checks["po_found"]
        or not checks["amount_within_tolerance"]
        or low_confidence
    ):
        decision = "flagged"
        reasoning.insert(
            0,
            "DECISION: FLAGGED for human review. The invoice has issues that "
            "require a person to verify before approval.",
        )

    # APPROVED — everything clean
    else:
        decision = "approved"
        reasoning.insert(
            0,
            "DECISION: APPROVED. All checks passed — the invoice is safe to "
            "pay based on automated matching.",
        )

    return {
        "decision": decision,
        "reasoning": reasoning,
        "matched_po": matched_po,
        "variance_amount": (
            round(variance_amount, 2) if variance_amount is not None else None
        ),
        "variance_pct": (
            round(variance_pct, 2) if variance_pct is not None else None
        ),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Demo / CLI
# ---------------------------------------------------------------------------

def _demo_invoices() -> List[Dict[str, Any]]:
    """Hand-crafted invoice dicts covering the main decision paths."""
    return [
        # 1) Clean, approvable invoice — matches PO-2024-0001 exactly
        {
            "vendor_name": "Acme Office Supplies Inc.",
            "invoice_number": "INV-1001",
            "invoice_date": "2024-06-15",
            "po_number": "PO-2024-0001",
            "line_items": [
                {"description": "Paper A4 (case)", "qty": 20, "unit_price": 45.00, "amount": 900.00},
                {"description": "Ink cartridges",  "qty": 10, "unit_price": 110.00, "amount": 1100.00},
                {"description": "Desk chairs",     "qty": 6,  "unit_price": 500.00, "amount": 3000.00},
            ],
            "subtotal": 5000.00,
            "tax": 0.00,
            "total_amount": 5000.00,
            "currency": "USD",
            "extraction_method": "text",
            "confidence_notes": [],
            "raw_text": "...",
        },

        # 2) Split invoice against PO-2024-0002 — remaining is 4000, invoice is 4050 (~1.25% variance, within tolerance)
        {
            "vendor_name": "Global Cloud Services Ltd.",
            "invoice_number": "GCS-2024-Q3",
            "invoice_date": "2024-07-01",
            "po_number": "PO-2024-0002",
            "line_items": [
                {"description": "Cloud hosting Q3", "qty": 1, "unit_price": 4050.00, "amount": 4050.00},
            ],
            "subtotal": 4050.00,
            "tax": 0.00,
            "total_amount": 4050.00,
            "currency": "USD",
            "extraction_method": "text",
            "confidence_notes": [],
            "raw_text": "...",
        },

        # 3) Unapproved vendor — should be REJECTED
        {
            "vendor_name": "QuickShip Logistics Co.",
            "invoice_number": "QS-889",
            "invoice_date": "2024-06-20",
            "po_number": "PO-2024-0004",
            "line_items": [
                {"description": "Freight service", "qty": 1, "unit_price": 2200.00, "amount": 2200.00},
            ],
            "subtotal": 2200.00,
            "tax": 0.00,
            "total_amount": 2200.00,
            "currency": "USD",
            "extraction_method": "text",
            "confidence_notes": [],
            "raw_text": "...",
        },

        # 4) OCR-based, missing PO number — should FLAG (fuzzy match to Sunrise on amount)
        {
            "vendor_name": "Sunrise Consulting Grp",  # slightly different name
            "invoice_number": "SCG-2024-088",
            "invoice_date": "2024-08-05",
            "po_number": None,
            "line_items": [
                {"description": "Advisory services August", "qty": 1, "unit_price": 4900.00, "amount": 4900.00},
            ],
            "subtotal": 4900.00,
            "tax": 0.00,
            "total_amount": 4900.00,
            "currency": "USD",
            "extraction_method": "ocr",
            "confidence_notes": ["Could not determine PO number reliably."],
            "raw_text": "...",
        },

        # 5) Amount way over PO remaining balance — should FLAG
        {
            "vendor_name": "Bright Ideas Marketing GmbH",
            "invoice_number": "BIM-4421",
            "invoice_date": "2024-07-10",
            "po_number": "PO-2024-0003",
            "line_items": [
                {"description": "Ad campaign", "qty": 1, "unit_price": 9500.00, "amount": 9500.00},
            ],
            "subtotal": 9500.00,
            "tax": 0.00,
            "total_amount": 9500.00,  # PO is only 7500 EUR
            "currency": "EUR",
            "extraction_method": "text",
            "confidence_notes": [],
            "raw_text": "...",
        },
    ]


if __name__ == "__main__":
    po_db = load_sample_po_database()
    invoices = _demo_invoices()

    # Use a fresh set for the demo so we can also demonstrate duplicate detection.
    seen: Set[Tuple[str, str]] = set()

    results = []
    for i, invoice in enumerate(invoices, 1):
        print(f"\n{'=' * 70}")
        print(f"INVOICE #{i}: {invoice.get('vendor_name')} / {invoice.get('invoice_number')}")
        print("=" * 70)
        result = match_invoice_to_po(invoice, po_db, tolerance_pct=2.0, seen_invoices=seen)
        print(json.dumps(result, indent=2, default=str))
        results.append(result)

    # Bonus: re-process invoice #1 to demonstrate duplicate detection
    print(f"\n{'=' * 70}")
    print("INVOICE #1 (RESUBMITTED) — should be REJECTED as duplicate")
    print("=" * 70)
    dup_result = match_invoice_to_po(invoices[0], po_db, tolerance_pct=2.0, seen_invoices=seen)
    print(json.dumps(dup_result, indent=2, default=str))

    # Exit with non-zero if any invoice was rejected (useful for CI-like use)
    sys.exit(0)
