"""Telegram-бот LiVPN: проверка канала, выдача и обновление подписки."""
from __future__ import annotations

import html
import logging
import re
import threading
import time

import telebot as telebot_api
from telebot import TeleBot, types

import ai
import config
import db
import service

log = logging.getLogger("livpn.bot")

if not config.BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Заполните .env")

if config.TELEGRAM_PROXY:
    telebot_api.apihelper.proxy = {
        "http": config.TELEGRAM_PROXY,
        "https": config.TELEGRAM_PROXY,
    }
    log.info("Telegram: используется прокси из TELEGRAM_PROXY/PROXY_URL")

if config.TELEGRAM_API_URL:
    telebot_api.apihelper.API_URL = config.TELEGRAM_API_URL
    log.info("Telegram: API_URL=%s", config.TELEGRAM_API_URL)

bot = TeleBot(config.BOT_TOKEN, parse_mode="HTML", threaded=True)

MEMBER_TTL = 60
_member_cache: dict[int, tuple[float, bool]] = {}
_cache_lock = threading.Lock()

_active_users: set[int] = set()
_active_lock = threading.Lock()

WAIT_TEXT = (
    "Ожидайте, обращаемся к серверам. Может занять некоторое время. "
    "Ссылка на подписку создаётся автоматически."
)

GATE_TEXT = (
    "Здравствуйте!\n"
    "Подпишитесь на канал чтобы продолжить:\n"
    f"{config.CHANNEL_HANDLE}"
)

WELCOME_TEXT = (
    "LiVPN — сервис, который абсолютно бесплатен.\n\n"
    "Нажмите на кнопку «Подключить», чтобы продолжить:"
)


# ── Канал ────────────────────────────────────────────────────────────────────

def _channel_ref():
    raw = (config.CHANNEL_ID or config.CHANNEL).strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def is_subscribed(user_id: int, use_cache: bool = True) -> bool:
    now = time.time()
    if use_cache:
        with _cache_lock:
            cached = _member_cache.get(user_id)
        if cached and now - cached[0] < MEMBER_TTL:
            return cached[1]

    try:
        member = bot.get_chat_member(_channel_ref(), user_id)
        status = str(getattr(member, "status", "") or "")
        ok = status in ("creator", "administrator", "member")
        if status == "restricted":
            ok = bool(getattr(member, "is_member", False))
    except Exception as exc:
        # Сетевую ошибку не кешируем: иначе человек на минуту застрянет
        # в «подпишитесь на канал» из-за разового сбоя Telegram.
        log.warning("Не удалось проверить подписку пользователя %s: %s", user_id, exc)
        return False

    with _cache_lock:
        _member_cache[user_id] = (now, ok)
    return ok


def _gate_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Подписаться", url=config.CHANNEL_URL),
    )
    keyboard.add(
        types.InlineKeyboardButton("Проверить ✅", callback_data="gate:check"),
    )
    return keyboard


def _welcome_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Подключить VPN", callback_data="connect"),
    )
    return keyboard


def _sub_keyboard(sub=None) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="refresh"),
        types.InlineKeyboardButton("📲 Как подключить", callback_data="help"),
    )
    if sub is not None:
        dashboard = config.dashboard_url(sub["token"])
        try:
            if dashboard.startswith("https://"):
                keyboard.add(
                    types.InlineKeyboardButton(
                        "👤 Кабинет", web_app=types.WebAppInfo(url=dashboard)
                    )
                )
            else:
                keyboard.add(types.InlineKeyboardButton("👤 Кабинет", url=dashboard))
        except Exception:
            keyboard.add(types.InlineKeyboardButton("👤 Кабинет", url=dashboard))
    keyboard.add(
        types.InlineKeyboardButton("Канал", url=config.CHANNEL_URL),
    )
    return keyboard


def send_gate(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        GATE_TEXT,
        reply_markup=_gate_keyboard(),
        disable_web_page_preview=True,
    )


def send_welcome(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        WELCOME_TEXT,
        reply_markup=_welcome_keyboard(),
        disable_web_page_preview=True,
    )


def _require_member(message) -> bool:
    if is_subscribed(message.from_user.id):
        return True
    send_gate(message.chat.id)
    return False


# ── Служебное ────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _claim_user(user_id: int) -> bool:
    with _active_lock:
        if user_id in _active_users:
            return False
        _active_users.add(user_id)
        return True


def _release_user(user_id: int) -> None:
    with _active_lock:
        _active_users.discard(user_id)


def _safe_error(exc: Exception) -> str:
    text = re.sub(r"(?:https?|vless|vmess|ss|trojan)://\S+", "<ссылка скрыта>", str(exc))
    text = re.sub(
        r"(?i)\b(access_token|refresh_token|token|password|secret|authorization)"
        r"\b\s*[:=]\s*\"?[^\s\",}]+",
        r"\1=<скрыто>",
        text,
    )
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
    return html.escape(text[:300])


def _answer(call, text: str = "", alert: bool = False) -> None:
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=alert)
    except Exception:
        pass


def _edit(
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: types.InlineKeyboardMarkup | None = None,
) -> None:
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        # Сообщение могли удалить, или текст не изменился.
        try:
            bot.send_message(
                chat_id,
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Не удалось отправить сообщение в %s", chat_id)


def _cooldown_hours() -> float:
    return max(0, config.USER_REFRESH_COOLDOWN_MINUTES) / 60


def _card_text(sub) -> str:
    nodes = service.stored_nodes(int(sub["id"]))
    url = service.link_for(sub)
    description = "\n".join(
        line for line in (config.SUB_DESCRIPTION or "").splitlines() if line.strip()
    )
    return (
        "✅ <b>Ваша подписка готова!</b>\n\n"
        "<b>Ваша подписка:</b>\n"
        f"<code>{html.escape(url)}</code>\n\n"
        "<b>Кабинет (трафик, серверы, QR):</b>\n"
        f"{html.escape(config.dashboard_url(sub['token']))}\n\n"
        f"<b>Название:</b> {html.escape(sub['name'] or config.SUB_NAME)}\n"
        f"<b>Серверов:</b> {len(nodes)}\n"
        f"<b>Трафик:</b> {html.escape(service.traffic_text(sub))}\n"
        f"<b>Обновление:</b> каждые {config.UPDATE_INTERVAL_HOURS} ч "
        f"({html.escape(service.last_refresh_text(sub))})\n\n"
        f"<i>{html.escape(description)}</i>"
    )


def send_card(chat_id: int, user_id: int, edit_id: int | None = None) -> None:
    sub = db.get_sub_by_user(user_id)
    if not sub:
        if edit_id:
            _edit(chat_id, edit_id, "Подписка не найдена. Нажмите «Подключить VPN».")
        else:
            send_welcome(chat_id)
        return

    text = _card_text(sub)
    if edit_id:
        _edit(chat_id, edit_id, text, _sub_keyboard(sub))
    else:
        bot.send_message(
            chat_id,
            text,
            reply_markup=_sub_keyboard(sub),
            disable_web_page_preview=True,
        )


def _job(chat_id: int, user, edit_id: int, do_refresh: bool = True) -> None:
    """Фоновая задача: обновить подписку и показать карточку."""
    user_id = user.id
    warning = ""
    try:
        sub = service.ensure_sub(user_id, user.username, user.first_name)
        nodes = service.stored_nodes(int(sub["id"]))

        if do_refresh:
            try:
                refreshed = service.refresh_sub_claimed(int(sub["id"]))
                sub = refreshed or db.get_sub(int(sub["id"])) or sub
                if refreshed is None and not nodes:
                    warning = (
                        "\n\n⏳ Обновление уже выполняется. "
                        "Откройте ссылку через минуту."
                    )
            except Exception as exc:
                log.warning("Обновление #%s не удалось: %s", sub["id"], exc)
                if not nodes:
                    raise
                warning = (
                    "\n\n⚠️ Не удалось обновить сейчас: "
                    + _safe_error(exc)
                    + "\nПоказаны последние сохранённые серверы."
                )
                sub = db.get_sub(int(sub["id"]))

        sub = db.get_sub(int(sub["id"])) or sub
        text = _card_text(sub) + warning
        _edit(chat_id, edit_id, text, _sub_keyboard(sub))
    except Exception as exc:
        log.exception("Не удалось создать/обновить подписку #%s", user_id)
        _edit(
            chat_id,
            edit_id,
            "❌ <b>Не удалось подключиться</b>\n\n"
            f"<code>{_safe_error(exc)}</code>\n\n"
            "Попробуйте ещё раз через минуту.",
            types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Повторить", callback_data="connect")
            ),
        )
    finally:
        _release_user(user_id)


def _start_job(chat_id: int, user, edit_id: int, do_refresh: bool = True) -> bool:
    if not _claim_user(user.id):
        return False
    threading.Thread(
        target=_job,
        args=(chat_id, user, edit_id, do_refresh),
        name=f"livpn-connect-{user.id}",
        daemon=True,
    ).start()
    return True


# ── Команды ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message) -> None:
    user = message.from_user
    db.touch_user(user.id, user.username, user.first_name)
    if not is_subscribed(user.id, use_cache=False):
        send_gate(message.chat.id)
        return
    sub = db.get_sub_by_user(user.id)
    if sub:
        # Уже подключались — сразу карточка, а не «нажмите Подключить».
        nodes = service.stored_nodes(int(sub["id"]))
        if not nodes:
            service.maybe_refresh_async(sub, force=True)
        elif service.needs_refresh(sub):
            service.maybe_refresh_async(sub)
        send_card(message.chat.id, user.id)
        return
    send_welcome(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "gate:check")
def cb_gate_check(call) -> None:
    if not is_subscribed(call.from_user.id, use_cache=False):
        _answer(call, "Вы ещё не подписались на канал", alert=True)
        return

    _answer(call, "Подписка подтверждена ✅")
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass
    send_welcome(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "connect")
def cb_connect(call) -> None:
    if not is_subscribed(call.from_user.id, use_cache=False):
        _answer(call, "Сначала подпишитесь на канал", alert=True)
        send_gate(call.message.chat.id)
        return

    _answer(call, "Создаём подписку…")
    wait = bot.send_message(call.message.chat.id, WAIT_TEXT)

    sub = service.ensure_sub(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
    )
    nodes = service.stored_nodes(int(sub["id"]))
    do_refresh = service.needs_refresh(sub, hours=0.05) or not nodes

    if not _start_job(
        call.message.chat.id,
        call.from_user,
        wait.message_id,
        do_refresh=do_refresh,
    ):
        _answer(call, "Уже подключаем, подождите…", alert=True)
        _edit(
            call.message.chat.id,
            wait.message_id,
            "⏳ Подключение уже выполняется. Подождите немного.",
        )


@bot.message_handler(commands=["sub", "link"])
def cmd_sub(message) -> None:
    if not _require_member(message):
        return

    user = message.from_user
    sub = service.ensure_sub(user.id, user.username, user.first_name)
    nodes = service.stored_nodes(int(sub["id"]))

    if not nodes:
        wait = bot.send_message(message.chat.id, WAIT_TEXT)
        if not _start_job(message.chat.id, user, wait.message_id, do_refresh=True):
            _edit(
                message.chat.id,
                wait.message_id,
                "Подключение уже выполняется. Подождите немного.",
            )
        return

    if service.needs_refresh(sub):
        service.maybe_refresh_async(sub)
    send_card(message.chat.id, user.id)


@bot.message_handler(commands=["cabinet", "web"])
def cmd_cabinet(message) -> None:
    if not _require_member(message):
        return

    user = message.from_user
    sub = service.ensure_sub(user.id, user.username, user.first_name)
    url = config.dashboard_url(sub["token"])
    keyboard = types.InlineKeyboardMarkup()
    try:
        if url.startswith("https://"):
            keyboard.add(
                types.InlineKeyboardButton(
                    "👤 Открыть кабинет", web_app=types.WebAppInfo(url=url)
                )
            )
        else:
            keyboard.add(types.InlineKeyboardButton("👤 Открыть кабинет", url=url))
    except Exception:
        keyboard.add(types.InlineKeyboardButton("👤 Открыть кабинет", url=url))

    bot.send_message(
        message.chat.id,
        "👤 <b>Кабинет LiVPN</b>\n\n"
        "Трафик, список серверов, QR-коды и время следующего обновления:\n"
        f"{html.escape(url)}",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["refresh", "update"])
def cmd_refresh(message) -> None:
    if not _require_member(message):
        return

    user = message.from_user
    sub = service.ensure_sub(user.id, user.username, user.first_name)

    if (
        not is_admin(user.id)
        and not service.needs_refresh(sub, hours=_cooldown_hours())
    ):
        bot.send_message(
            message.chat.id,
            "Подписка уже обновлена "
            f"{service.last_refresh_text(sub)}. "
            f"Повторно обновить можно через "
            f"{config.USER_REFRESH_COOLDOWN_MINUTES} мин.\n\n"
            "Ссылка та же, менять её не нужно.",
        )
        return

    wait = bot.send_message(message.chat.id, WAIT_TEXT)
    if not _start_job(message.chat.id, user, wait.message_id, do_refresh=True):
        _edit(
            message.chat.id,
            wait.message_id,
            "Обновление уже выполняется. Подождите немного.",
        )


@bot.message_handler(commands=["status", "traffic"])
def cmd_status(message) -> None:
    if not _require_member(message):
        return

    sub = db.get_sub_by_user(message.from_user.id)
    if not sub:
        send_welcome(message.chat.id)
        return

    nodes = service.stored_nodes(int(sub["id"]))
    bot.send_message(
        message.chat.id,
        "📊 <b>Статус подписки</b>\n\n"
        f"<b>Серверов:</b> {len(nodes)}\n"
        f"<b>Трафик:</b> {html.escape(service.traffic_text(sub))}\n"
        f"<b>Обновлена:</b> {html.escape(service.last_refresh_text(sub))}\n"
        f"<b>Интервал:</b> каждые {config.UPDATE_INTERVAL_HOURS} ч",
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["help", "howto"])
def cmd_help(message) -> None:
    if not _require_member(message):
        return
    _send_help(message.chat.id)


def _send_help(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "📲 <b>Как подключить</b>\n\n"
        "1. Нажмите «Подключить VPN» и скопируйте ссылку подписки.\n"
        "2. <b>Happ</b>: «Добавить подписку» → вставьте ссылку → «Импорт».\n"
        "3. <b>Incy</b>: «Подписки» → «+» → вставьте ссылку.\n"
        "4. <b>v2rayNG / Hiddify / Streisand</b>: «Подписки» → «+» → URL.\n\n"
        "👤 В <b>кабинете</b> видно трафик, все серверы и QR-коды: /cabinet.\n\n"
        f"Подписка обновляется каждые {config.UPDATE_INTERVAL_HOURS} ч, "
        "трафик при этом не сбрасывается. Ссылка не меняется.\n\n"
        f"Поддержка: {html.escape(config.SUPPORT_URL)}",
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "help")
def cb_help(call) -> None:
    if not is_subscribed(call.from_user.id, use_cache=False):
        _answer(call, "Сначала подпишитесь на канал", alert=True)
        send_gate(call.message.chat.id)
        return
    _answer(call)
    _send_help(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "refresh")
def cb_refresh(call) -> None:
    if not is_subscribed(call.from_user.id, use_cache=False):
        _answer(call, "Сначала подпишитесь на канал", alert=True)
        send_gate(call.message.chat.id)
        return

    user = call.from_user
    sub = service.ensure_sub(user.id, user.username, user.first_name)

    if (
        not is_admin(user.id)
        and not service.needs_refresh(sub, hours=_cooldown_hours())
    ):
        _answer(
            call,
            f"Уже обновлено {service.last_refresh_text(sub)}. "
            f"Повторить можно через {config.USER_REFRESH_COOLDOWN_MINUTES} мин.",
            alert=True,
        )
        send_card(call.message.chat.id, user.id)
        return

    _answer(call, "Обновляем…")
    wait = bot.send_message(call.message.chat.id, WAIT_TEXT)
    if not _start_job(call.message.chat.id, user, wait.message_id, do_refresh=True):
        _edit(
            call.message.chat.id,
            wait.message_id,
            "Обновление уже выполняется. Подождите немного.",
        )


@bot.message_handler(commands=["stats"])
def cmd_stats(message) -> None:
    if not is_admin(message.from_user.id):
        return
    stats = db.stats()
    bot.send_message(
        message.chat.id,
        "📈 <b>Статистика LiVPN</b>\n\n"
        f"Пользователей: {stats['users']}\n"
        f"Подписок: {stats['subs']}\n"
        f"Узлов: {stats['nodes']}\n"
        f"С ошибками обновления: {stats['failing']}\n"
        f"AI: {html.escape(ai.ai_status())}\n"
        f"Домен: {html.escape(config.base_url())}",
    )


@bot.message_handler(commands=["myid", "id"])
def cmd_myid(message) -> None:
    bot.send_message(
        message.chat.id,
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        f"Админ: {'да' if is_admin(message.from_user.id) else 'нет'}",
    )


@bot.message_handler(commands=["domain"])
def cmd_domain(message) -> None:
    if not is_admin(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        "<b>Адрес сервиса:</b>\n"
        f"<code>{html.escape(config.base_url())}</code>\n"
        f"Источник: {html.escape(config.base_url_source())}",
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["setdomain"])
def cmd_setdomain(message) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Использование:\n"
            "<code>/setdomain https://livpn.example.com</code>\n\n"
            "Обычно домен определяется автоматически после первого запроса "
            "к веб-серверу. Эта команда нужна, если бот и Flask на разных "
            "машинах или домен определился неправильно.",
        )
        return

    try:
        value = config.remember_base_url(parts[1].strip(), force=True)
    except ValueError as exc:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось сохранить домен: {html.escape(str(exc))}",
        )
        return

    bot.send_message(
        message.chat.id,
        f"✅ Домен сохранён:\n<code>{html.escape(value)}</code>",
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["clearcache"])
def cmd_clearcache(message) -> None:
    if not is_admin(message.from_user.id):
        return
    db.clear_name_cache()
    ai.reset_ai_block()
    bot.send_message(message.chat.id, "Кэш имён очищен, пауза AI сброшена.")


@bot.message_handler(content_types=["text"])
def fallback(message) -> None:
    if (message.text or "").startswith("/"):
        return
    cmd_start(message)


# ── Запуск ───────────────────────────────────────────────────────────────────

def _check_channel_access() -> None:
    try:
        chat = bot.get_chat(_channel_ref())
        title = getattr(chat, "title", "") or config.CHANNEL_HANDLE
        log.info("Канал найден: %s", title)
    except Exception as exc:
        log.error(
            "Не удалось открыть канал %s: %s. Добавьте бота в канал "
            "и выдайте права администратора.",
            config.CHANNEL_HANDLE,
            exc,
        )


def run_bot() -> None:
    db.init_db()
    try:
        me = bot.get_me()
        log.info("Бот @%s запущен", me.username)
        # Веб-страницы должны показывать реальное имя бота, даже если
        # BOT_USERNAME не задан в .env.
        if getattr(me, "username", ""):
            config.set_bot_username(me.username)
    except Exception as exc:
        log.error("Не удалось получить данные бота: %s", exc)

    _check_channel_access()
    try:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Главное меню"),
                types.BotCommand("sub", "Моя подписка"),
                types.BotCommand("cabinet", "Кабинет: трафик и QR"),
                types.BotCommand("refresh", "Обновить подписку"),
                types.BotCommand("status", "Статус и трафик"),
                types.BotCommand("help", "Как подключить"),
                types.BotCommand("domain", "Адрес сервиса"),
            ]
        )
    except Exception as exc:
        log.warning("Не удалось установить команды: %s", exc)

    service.start_scheduler()
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_bot()
