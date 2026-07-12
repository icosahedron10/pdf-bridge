FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDF_BRIDGE_STORAGE_ROOT=/var/lib/pdf-bridge

WORKDIR /app

RUN addgroup --system --gid 10001 pdfbridge \
    && adduser --system --uid 10001 --ingroup pdfbridge --home /nonexistent pdfbridge \
    && install -d -o pdfbridge -g pdfbridge -m 0700 /var/lib/pdf-bridge

COPY pyproject.toml README.md ./
COPY pdf_bridge ./pdf_bridge
RUN python -m pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/pdf-bridge-entrypoint
RUN chmod 0755 /usr/local/bin/pdf-bridge-entrypoint

USER pdfbridge:pdfbridge
EXPOSE 8000
VOLUME ["/var/lib/pdf-bridge"]

ENTRYPOINT ["pdf-bridge-entrypoint"]
CMD ["uvicorn", "pdf_bridge.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
