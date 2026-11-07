#!/usr/bin/env python3
"""Единая точка запуска LiVPN: Flask + Telegram-бот + планировщик."""
from __future__ import annotations

import argparse
import logging
import threading
import time

import config
import db
import service

log = logging.getLogger("livpn")


def main() -> None:
    parser = argparse.ArgumentParser(description="LiVPN")
    parser.add_argument("--web-only", action="store_true", help="только Flask")
    parser.add_argument("--bot-only", action="store_true", help="только бот")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.web_only and not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан. Заполните .env или запустите с --web-only."
        )

    # Синхронизируем существующие подписки с глобальным PROVIDER до старта
    # планировщика, чтобы он не успел обновить их через старый источник.
    db.init_db()
    changed = 0
    for row in db.list_subs():
        if row["provider"] != config.PROVIDER:
            db.set_provider(int(row["id"]), config.PROVIDER)
            changed += 1
    if changed:
        log.info("Источник подписок обновлён для %d записей: %s", changed, config.PROVIDER)

    service.start_scheduler()
    server = None

    if not args.bot_only:
        from app import app
        from werkzeug.serving import make_server

        server = make_server(config.HOST, config.PORT, app, threaded=True)
        thread = threading.Thread(
            target=server.serve_forever,
            name="livpn-web",
            daemon=True,
        )
        thread.start()
        log.info(
            "Flask слушает http://%s:%s (публичный адрес: %s)",
            config.HOST,
            config.PORT,
            config.base_url(),
        )

    try:
        if args.web_only:
            log.info("Запущен режим только веб-сервера")
            while True:
                time.sleep(3600)
        else:
            from bot import run_bot

            run_bot()
    except KeyboardInterrupt:
        log.info("Остановка…")
    finally:
        service.stop_scheduler()
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
