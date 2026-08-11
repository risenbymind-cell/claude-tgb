FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kbot ./kbot

# The database lives on a volume so trade history and access keys survive a
# redeploy. Risk caps are enforced from this ledger — losing it resets them.
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/kbot.sqlite3

CMD ["python", "-m", "kbot"]
