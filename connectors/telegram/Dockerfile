# Telegram connector — Bot API long-poller.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connector.py .

EXPOSE 8081
VOLUME ["/data"]
CMD ["python3", "connector.py"]
