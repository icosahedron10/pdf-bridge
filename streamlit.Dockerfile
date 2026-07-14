FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp/streamlit-home \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true

WORKDIR /app

RUN addgroup --system --gid 10002 pdfbridge-ui \
    && adduser --system --uid 10002 --ingroup pdfbridge-ui --home /nonexistent pdfbridge-ui

# Keep the canonical operator image a pure HTTP client. It deliberately does not
# contain the Bridge package, model runtimes, migrations, or storage tooling.
RUN python -m pip install --no-cache-dir \
    "httpx==0.28.1" \
    "streamlit==1.59.2"

COPY streamlit_app ./streamlit_app

USER pdfbridge-ui:pdfbridge-ui
EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.enableCORS=true", "--server.enableXsrfProtection=true", "--server.fileWatcherType=none", "--server.runOnSave=false"]
