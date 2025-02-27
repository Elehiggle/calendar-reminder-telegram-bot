FROM python:3.13-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .

RUN mkdir -p /app/data

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python /app/healthcheck.py

CMD ["python", "main.py"]
