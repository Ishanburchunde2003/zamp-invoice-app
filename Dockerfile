# Dockerfile — Invoice Processing Automation
# Installs Python deps + the system-level OCR binaries (tesseract, poppler)
# that pytesseract/pdf2image need, then serves the Flask app with gunicorn.

FROM python:3.11-slim

# System dependencies for OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render (and most PaaS) inject a PORT env var at runtime.
ENV PORT=10000
EXPOSE 10000

# NOTE: --workers must stay at 1. JOBS and SEEN_INVOICES in app.py are
# plain in-memory Python dicts/sets. With more than one gunicorn worker,
# each worker process gets its own separate copy — a request handled by
# worker A can create a job that worker B (handling the next poll request)
# has never seen, producing a false "Job not found" error. A single
# worker keeps all state in one process, which is fine for this demo's
# traffic level.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
