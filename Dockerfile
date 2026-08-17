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
# compose.
#
# The port is read from the environment rather than hardcoded, because one
# image runs two services: the bot serves /healthz on 8080 and the desk on
# 8787. A fixed 8080 meant that running this image as the desk marked the
# container permanently unhealthy -- and an orchestrator responds to that by
# restarting a process that was working correctly. Found by building the
# image and running the desk in it.
ENV HEALTHCHECK_PORT=8080
# ProxyHandler({}) is not decoration: urllib honours HTTP_PROXY even for a
# 127.0.0.1 URL, so on any host that sets one -- which is most corporate and
# some cloud environments -- the check would route a loopback request through
# a proxy, fail, and mark a perfectly healthy container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:'+os.environ.get('HEALTHCHECK_PORT','8080')+'/healthz',timeout=4)" \
      || exit 1

CMD ["python", "-m", "kbot"]
