# syntax=docker/dockerfile:1.7
# Faheem Pharmacy ERP — one image for the web process, the worker and the migration job.
# Built and pushed by .github/workflows/release.yml on the prod branch:
#   ghcr.io/arftw-operative/faheem-pharmacy-erp:<version>  and  :sha-<commit>   (never :latest)
FROM python:3.14.7-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# pg_dump / pg_restore of the same major version as the database (snapshots, upgrade rehearsal),
# Tesseract for scanned supplier bills, tzdata for the shop's clock.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 tesseract-ocr tesseract-ocr-eng tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN grep -v '^pytest' requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt && rm /tmp/req.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY compose.yaml compose.prod.yaml ./
COPY deploy/appliance ./deploy/appliance
# the remote support package travels with each release but is installed / upgraded only on request
COPY deploy/support ./deploy/support

ARG APP_VERSION=dev
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV APP_VERSION=${APP_VERSION} GIT_COMMIT=${GIT_COMMIT} BUILD_DATE=${BUILD_DATE} \
    PHARMACY_HOST=0.0.0.0 PHARMACY_PORT=8000 TZ=Asia/Kolkata
LABEL org.opencontainers.image.title="Faheem Pharmacy ERP" \
      org.opencontainers.image.source="https://github.com/ArFtw-operative/faheem-pharmacy-erp" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

RUN groupadd --gid 10001 faheem && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin faheem
USER 10001:10001
EXPOSE 8000

# one process: in-memory report documents and locks are per process
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
