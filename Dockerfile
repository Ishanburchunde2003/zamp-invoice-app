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

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
