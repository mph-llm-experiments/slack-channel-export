FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY slack_channel_export_selfservice_1.py .

# Cloud Run injects PORT (defaults to 8080). One worker keeps the
# in-memory rejoin session store coherent; threads handle
# concurrent requests.
CMD exec gunicorn \
    --bind :${PORT:-8080} \
    --workers 1 \
    --threads 8 \
    --timeout 0 \
    slack_channel_export_selfservice_1:app
