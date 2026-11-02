# syntax=docker/dockerfile:1.6
#
# VK Platform image. Build from the vk_digital_hub folder (the app imports the provider
# automation in railtel_debugger/vk_agent, which lives next to vk_platform):
#
#   cd vk_digital_hub
#   docker build -f vk_platform/Dockerfile -t vk-platform:latest .
#
# Data (SQLite DB, WhatsApp login, invoices, screenshots) lives in the /data volume.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng xvfb tzdata ca-certificates \
      fonts-noto-core fonts-noto-color-emoji \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

COPY vk_platform/requirements.txt vk_platform/docker/requirements-docker.txt /tmp/
RUN pip install -r /tmp/requirements.txt -r /tmp/requirements-docker.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/vk_digital_hub/vk_platform
COPY railtel_debugger/vk_agent/ /srv/vk_digital_hub/railtel_debugger/vk_agent/
COPY bix42_export/ /srv/vk_digital_hub/bix42_export/
COPY vk_platform/ /srv/vk_digital_hub/vk_platform/

# Windows checkouts give the entrypoint CRLF endings, which /bin/sh cannot run.
RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh \
 && rm -rf data && ln -s /data data

ENV VK_PLATFORM_HOST=0.0.0.0 \
    VK_PLATFORM_PORT=8800 \
    VK_BIX_HISTORY_DB=/data/vk_digital_history.db

VOLUME ["/data"]
EXPOSE 8800

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=8)"

ENTRYPOINT ["docker/entrypoint.sh"]
