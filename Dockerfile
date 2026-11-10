FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/livpn.db \
    BASE_URL_FILE=/data/base_url.txt \
    BOT_USERNAME_FILE=/data/bot_username.txt \
    VLESSHUB_ACCOUNT_FILE=/data/account.json \
    VLESSHUB_ACCOUNTS_DIR=/data/accounts \
    NODES_FILE=/data/nodes.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py bot.py config.py db.py service.py xray.py xrjson.py ai.py register.py run.py ./
COPY providers ./providers
COPY templates ./templates
COPY static ./static
COPY nodes.example.json README.md ./
COPY docker-entrypoint.sh /usr/local/bin/livpn-entrypoint
RUN chmod +x /usr/local/bin/livpn-entrypoint && mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('PORT','8080'), timeout=4)"

ENTRYPOINT ["livpn-entrypoint"]
CMD []
