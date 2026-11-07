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


def _run_bot_forever() -> None:
    """Держим бота живым: 409 при пересечении рестартов не должен ронять веб."""
    from telebot.apihelper import ApiTelegramException

    from bot import run_bot

    delay = 5
    while True:
        started = time.monotonic()
        try:
            run_bot()
            log.warning("Telegram-бот остановился, перезапуск через %d сек", delay)
        except KeyboardInterrupt:
            raise
        except ApiTelegramException as exc:
            if getattr(exc, "error_code", 0) == 409:
                # Старый контейнер ещё опрашивает Telegram во время рестарта.
                log.warning(
                    "Telegram: getUpdates занят другим экземпляром (409). "
                    "Повтор через %d сек",
                    delay,
                )
            else:
                log.exception("Ошибка Telegram API, повтор через %d сек", delay)
        except Exception:
            log.exception("Telegram-бот упал, повтор через %d сек", delay)
        if time.monotonic() - started > 60:
            delay = 5
        else:
            delay = min(delay * 2, 60)
        time.sleep(delay)


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
        elif args.bot_only:
            log.info("Запущен режим только Telegram-бота")
            _run_bot_forever()
            return
        else:
            # Бот работает в фоновом потоке: его сбой (например, 409 при
            # пересечении рестартов) больше не убивает веб-сервер.
            threading.Thread(
                target=_run_bot_forever,
                name="livpn-bot",
                daemon=True,
            ).start()
            log.info("Запущены веб-сервер и Telegram-бот")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Остановка…")
    finally:
        service.stop_scheduler()
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()
