# ──────────────────────────────────────────────────────────────────────────────
# signalbot — combined Flask dashboard + message poller
#
# Built on the official Playwright Python image so Chromium and all its system
# dependencies are already present (the poller uses Playwright for screenshots
# and HTML snapshots). Pin the Playwright image tag to a version compatible with
# whatever `playwright` resolves to in requirements.txt.
# ──────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg/ffprobe: image_caption.py samples video frames for AI captioning.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install chromium

# Application code.
COPY . .

# Flask dashboard.
EXPOSE 5581

# `app.py` honours CLI flags (--no-poller / --no-web / --debug / --port) and all
# the env vars in config.py; override the command in docker-compose if needed.
CMD ["python3", "app.py"]
