# Official Playwright image — has Chromium + all system dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browsers are already in the base image; just link them for playwright
RUN playwright install chromium

COPY csm_monday_webhook_board3.py .

EXPOSE 8000

# IMPORTANT: shell form (sh -c) required so Railway's dynamic $PORT expands
# correctly. Exec form (["uvicorn", ...]) silently falls back to a hardcoded
# port and causes 502 errors on Railway.
CMD sh -c "uvicorn csm_monday_webhook_board3:app --host 0.0.0.0 --port ${PORT:-8000}"
