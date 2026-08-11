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

# Serves payment callbacks and /healthz. Outbound-only if you don't take
# payments.
EXPOSE 8080

RUN useradd --create-home --uid 10001 kbot \
 && mkdir -p /app/data && chown -R kbot:kbot /app
USER kbot

CMD ["python", "-m", "kbot"]
