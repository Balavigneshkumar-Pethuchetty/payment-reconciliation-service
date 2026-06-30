FROM python:3.12-slim
WORKDIR /app
# Tesseract OCR engine + English data
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx python-multipart pypdf asyncpg "PyJWT[crypto]" cryptography Pillow pytesseract anthropic
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8082"]
