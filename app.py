"""
app.py — Flask web application for the invoice processing automation demo.

Routes:
    GET  /                  → Upload page
    POST /upload            → Receives PDF, saves it, redirects to processing
    GET  /processing/<id>   → Shows animated pipeline stages, polls for completion
    GET  /status/<id>       → JSON endpoint: returns {"done": bool} for polling
    GET  /results/<id>      → Final results page (extraction + matching)
    GET  /history           → Dashboard of all processed invoices
    GET  /history/clear     → Clear the history (convenience for demo)
    GET  /history/export    → Download history as JSON

The app stores processed results in a local JSON file so they survive restarts.
Uploaded PDFs are saved to an ./uploads/ directory.
"""

import json
import os
import time
import uuid
import traceback
from datetime import datetime
from threading import Thread
from typing import Any, Dict

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

# ---------------------------------------------------------------------------
# Import the extraction and matching modules (must be in the same directory)
# ---------------------------------------------------------------------------
from extraction import extract_invoice
from matching import load_sample_po_database, match_invoice_to_po

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "invoice-demo-secret-key-change-me")

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_history.json")
ALLOWED_EXTENSIONS = {"pdf"}
MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# PO database — loaded once at startup
PO_DATABASE = load_sample_po_database()

# Tracks invoices seen in this process lifetime (for duplicate detection)
SEEN_INVOICES: set = set()

# Tracks background processing jobs: job_id → status dict
# Status dict:  {"done": bool, "stage": str, "result": dict|None, "error": str|None}
JOBS: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# History persistence helpers
# ---------------------------------------------------------------------------


def _load_history() -> list:
    """Load processing history from the JSON file."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_history(history: list) -> None:
    """Persist history to the JSON file."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False, default=str)


def _append_to_history(entry: dict) -> None:
    """Add one entry to the persisted history."""
    history = _load_history()
    history.insert(0, entry)  # newest first
    _save_history(history)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Background processing worker
# ---------------------------------------------------------------------------


def _process_invoice(job_id: str, pdf_path: str, original_filename: str) -> None:
    """Run extraction → matching in a background thread, updating JOBS state.

    Each stage update is written to JOBS[job_id]["stage"] so the polling
    endpoint can report progress to the frontend.
    """
    job = JOBS[job_id]
    try:
        # Stage 1 — Extracting text from PDF
        job["stage"] = "extracting"
        time.sleep(0.6)  # Small delay so the UI animation is visible
        invoice_data = extract_invoice(pdf_path)

        # Stage 2 — Parsing fields
        job["stage"] = "parsing"
        time.sleep(0.5)

        # Stage 3 — Matching to PO
        job["stage"] = "matching"
        time.sleep(0.5)
        match_result = match_invoice_to_po(
            invoice_data,
            PO_DATABASE,
            tolerance_pct=2.0,
            seen_invoices=SEEN_INVOICES,
        )

        # Stage 4 — Producing decision
        job["stage"] = "deciding"
        time.sleep(0.4)

        # Build the combined result
        result = {
            "invoice": invoice_data,
            "match": match_result,
            "filename": original_filename,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
        job["result"] = result

        # Persist to history
        history_entry = {
            "id": job_id,
            "filename": original_filename,
            "vendor_name": invoice_data.get("vendor_name"),
            "invoice_number": invoice_data.get("invoice_number"),
            "invoice_date": invoice_data.get("invoice_date"),
            "total_amount": invoice_data.get("total_amount"),
            "currency": invoice_data.get("currency", "USD"),
            "decision": match_result.get("decision"),
            "extraction_method": invoice_data.get("extraction_method"),
            "processed_at": result["processed_at"],
        }
        _append_to_history(history_entry)

    except Exception as exc:
        job["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        job["stage"] = "done"
        job["done"] = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def upload_page():
    """Render the upload form."""
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    """Handle the PDF upload, kick off background processing."""

    # --- Validate the upload ------------------------------------------------
    if "invoice_file" not in request.files:
        flash("No file was submitted. Please choose a PDF to upload.", "error")
        return redirect(url_for("upload_page"))

    file = request.files["invoice_file"]
    if file.filename == "" or file.filename is None:
        flash("No file selected. Please choose a PDF to upload.", "error")
        return redirect(url_for("upload_page"))

    if not _allowed_file(file.filename):
        flash(
            f"Invalid file type '{file.filename.rsplit('.', 1)[-1]}'. "
            "Only PDF files are accepted.",
            "error",
        )
        return redirect(url_for("upload_page"))

    # --- Save the file ------------------------------------------------------
    original_filename = file.filename
    job_id = uuid.uuid4().hex[:12]
    safe_name = f"{job_id}.pdf"
    pdf_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)

    try:
        file.save(pdf_path)
    except Exception as exc:
        flash(f"Failed to save uploaded file: {exc}", "error")
        return redirect(url_for("upload_page"))

    # Quick sanity check: is it actually a PDF?
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            os.remove(pdf_path)
            flash(
                "The uploaded file does not appear to be a valid PDF. "
                "Please check the file and try again.",
                "error",
            )
            return redirect(url_for("upload_page"))
    except Exception:
        pass  # If we can't read the header, let extraction.py handle the error

    # --- Start background processing ----------------------------------------
    JOBS[job_id] = {
        "done": False,
        "stage": "queued",
        "result": None,
        "error": None,
    }
    thread = Thread(
        target=_process_invoice,
        args=(job_id, pdf_path, original_filename),
        daemon=True,
    )
    thread.start()

    return redirect(url_for("processing_page", job_id=job_id))


@app.route("/processing/<job_id>")
def processing_page(job_id: str):
    """Show the pipeline progress tracker (polls /status/<job_id> via JS)."""
    if job_id not in JOBS:
        flash("Processing job not found. It may have expired.", "error")
        return redirect(url_for("upload_page"))
    return render_template("processing.html", job_id=job_id)


@app.route("/status/<job_id>")
def job_status(job_id: str):
    """JSON endpoint polled by the processing page."""
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"done": True, "stage": "unknown", "error": "Job not found"}), 404
    return jsonify({
        "done": job["done"],
        "stage": job["stage"],
        "error": job.get("error"),
    })


@app.route("/results/<job_id>")
def results_page(job_id: str):
    """Display the final extraction + matching results."""
    job = JOBS.get(job_id)
    if job is None:
        flash("Results not found. The job may have expired.", "error")
        return redirect(url_for("upload_page"))

    if not job["done"]:
        return redirect(url_for("processing_page", job_id=job_id))

    if job.get("error"):
        return render_template(
            "results.html",
            job_id=job_id,
            error=job["error"],
            result=None,
        )

    return render_template(
        "results.html",
        job_id=job_id,
        error=None,
        result=job["result"],
    )


@app.route("/history")
def history_page():
    """Dashboard showing all processed invoices."""
    history = _load_history()
    # Summary stats
    total = len(history)
    approved = sum(1 for h in history if h.get("decision") == "approved")
    flagged = sum(1 for h in history if h.get("decision") == "flagged")
    rejected = sum(1 for h in history if h.get("decision") == "rejected")
    stats = {
        "total": total,
        "approved": approved,
        "flagged": flagged,
        "rejected": rejected,
    }
    return render_template("history.html", history=history, stats=stats)


@app.route("/history/clear")
def clear_history():
    """Clear all history (convenience for demo)."""
    _save_history([])
    SEEN_INVOICES.clear()
    flash("Processing history has been cleared.", "success")
    return redirect(url_for("history_page"))


@app.route("/history/export")
def export_history():
    """Download history as JSON."""
    if not os.path.exists(HISTORY_FILE):
        flash("No history to export.", "error")
        return redirect(url_for("history_page"))
    return send_file(
        HISTORY_FILE,
        mimetype="application/json",
        as_attachment=True,
        download_name="invoice_history.json",
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def file_too_large(e):
    flash("File is too large. Maximum upload size is 25 MB.", "error")
    return redirect(url_for("upload_page"))


@app.errorhandler(404)
def page_not_found(e):
    flash("Page not found.", "error")
    return redirect(url_for("upload_page"))


@app.errorhandler(500)
def internal_error(e):
    flash("An internal error occurred. Please try again.", "error")
    return redirect(url_for("upload_page"))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Invoice Processing Automation — Demo")
    print("  Open http://127.0.0.1:5000 in your browser")
    print("=" * 60)
    app.run(debug=True, port=5000)
