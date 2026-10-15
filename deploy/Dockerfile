# One image, eight services: the fleet shares a codebase and differs only in the
# command it runs, which keeps dependency drift at zero.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Tesseract is the OCR engine — the one system dependency the platform needs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng libglib2.0-0 libgl1 poppler-utils curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN mkdir -p data outputs

# Non-root, and with a healthcheck so an orchestrator can restart a sick replica.
RUN useradd --create-home --uid 10001 apuser && chown -R apuser:apuser /app
USER apuser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${AP_PORT:-8080}/health || exit 1

CMD ["python3", "scripts/run_services.py", "--services", "gateway"]
