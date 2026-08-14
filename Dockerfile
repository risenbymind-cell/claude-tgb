FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kbot ./kbot
COPY site ./site

# The database lives on a volume so trade history, access keys and invoices
# survive a redeploy. Risk caps are enforced from this ledger — losing it
# resets them.
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/kbot.sqlite3

# 8080: payment callbacks and /healthz for the bot process.
# 8787: the web desk, when this image is run as the desk (see DEPLOY.md).
EXPOSE 8080 8787

RUN useradd --create-home --uid 10001 kbot \
 && mkdir -p /app/data && chown -R kbot:kbot /app
USER kbot

# Baked into the image so `docker run` and orchestrators get it too, not just
# compose. Only meaningful when the webhook listener is enabled.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" \
      || exit 1

CMD ["python", "-m", "kbot"]
