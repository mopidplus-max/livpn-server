"""Конфигурация LiVPN. Всё берётся из переменных окружения / файла .env."""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    """Минимальный загрузчик .env, чтобы не тащить python-dotenv."""
    if not path.exists():
        return
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off", "нет")


def _env_text(name: str, default: str) -> str:
    """Многострочные значения: в .env можно писать \\n."""
    return _env(name, default).replace("\\n", "\n")


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN = _env("BOT_TOKEN")
BOT_USERNAME = _env("BOT_USERNAME")  # без @, нужен только для ссылок в вебе
CHANNEL = _env("CHANNEL", "@LiVPN_official")
CHANNEL_HANDLE = _env("CHANNEL_HANDLE", CHANNEL if CHANNEL.startswith("@") else "@LiVPN_official")
CHANNEL_URL = _env(
    "CHANNEL_URL",
    "https://t.me/" + CHANNEL_HANDLE.lstrip("@"),
)
CHANNEL_ID = _env("CHANNEL_ID")  # числовой id, если канал приватный
SUPPORT_URL = _env("SUPPORT_URL", "https://t.me/livpn_official")
ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", _env("ADMIN_IDS", ""))
    if x.strip().isdigit()
}

# ── Прокси (Россия: Telegram/Groq часто недоступны напрямую) ──────────────────
# http://host:port или socks5://user:pass@host:port (для SOCKS нужен PySocks).
PROXY_URL = _env("PROXY_URL")
TELEGRAM_PROXY = _env("TELEGRAM_PROXY", PROXY_URL)
GROQ_PROXY = _env("GROQ_PROXY", PROXY_URL)
# Для локального Bot API server: http://127.0.0.1:8081/bot{0}/{1}
TELEGRAM_API_URL = _env("TELEGRAM_API_URL")

# ── Groq (переименование узлов) ───────────────────────────────────────────────
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = _env("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_TIMEOUT = _env_int("GROQ_TIMEOUT", 45)
AI_ENABLED = bool(GROQ_API_KEY) and _env_bool("AI_ENABLED", True)
AI_BATCH_SIZE = _env_int("AI_BATCH_SIZE", 40)
AI_MAX_TOKENS = _env_int("AI_MAX_TOKENS", 2048)

# ── Подписка ──────────────────────────────────────────────────────────────────
# Публичный адрес: https://livpn.example.com
# Если пусто — сервис попробует определить домен/адрес сам.
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
AUTO_DOMAIN = _env_bool("AUTO_DOMAIN", True)
# Проверять, что домен из Host действительно указывает на этот сервер.
# Защищает от подмены ссылок через поддельный заголовок Host.
AUTO_DOMAIN_VERIFY = _env_bool("AUTO_DOMAIN_VERIFY", True)
BASE_URL_FILE = Path(_env("BASE_URL_FILE", str(BASE_DIR / "base_url.txt")))
ALLOWED_HOSTS = {
    item.strip().lower()
    for item in re.split(r"[,\s]+", _env("ALLOWED_HOSTS", ""))
    if item.strip()
}
SUB_NAME = _env("SUB_NAME", "LiVPN 🌐")
# Приводим к виду " | LiVPN", даже если в .env потерялся пробел.
_SUFFIX_BODY = _env("SUB_SUFFIX", "| LiVPN").lstrip("|").strip() or "LiVPN"
SUB_SUFFIX = f" | {_SUFFIX_BODY}"
SUB_DESCRIPTION = _env_text(
    "SUB_DESCRIPTION",
    "Наш телеграм канал: https://t.me/livpn_official\n"
    "Обновляйте подписку каждые 24 часа.\n"
    "Даты истекания нету.",
)
UPDATE_INTERVAL_HOURS = _env_int("UPDATE_INTERVAL_HOURS", 24)
TOKEN_LEN = max(8, _env_int("TOKEN_LEN", 24))
MAX_NODES = max(1, _env_int("MAX_NODES", 500))
USER_REFRESH_COOLDOWN_MINUTES = _env_int("USER_REFRESH_COOLDOWN_MINUTES", 10)

# ── Источник конфигов ─────────────────────────────────────────────────────────
# static   — файл nodes.json (свои серверы)
# xui      — своя панель 3x-ui (бот сам создаёт клиента)
# upstream — своя официальная подписка (ссылка на вашу покупку)
# remnawave — официальная subscription-ссылка Remnawave (UPSTREAM_URL)
# vlesshub — аккаунт VLESSHub (автонастройка через mail.tm при первом запуске)
PROVIDER = _env("PROVIDER", "static").lower()
NODES_FILE = Path(_env("NODES_FILE", str(BASE_DIR / "nodes.json")))
UPSTREAM_URL = _env("UPSTREAM_URL")
UPSTREAM_USER_AGENT = _env("UPSTREAM_USER_AGENT", "Happ/3.0 (Android)")
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 30)

# vlesshub — аккаунты VLESSHub. У каждой подписки LiVPN свой аккаунт,
# иначе трафик у всех пользователей был бы одинаковым (один upstream-аккаунт).
VLESSHUB_API_URL = _env("VLESSHUB_API_URL", "https://vlesshub.fun/api").rstrip("/")
VLESSHUB_ACCOUNT_FILE = Path(
    _env("VLESSHUB_ACCOUNT_FILE", str(BASE_DIR / "account.json"))
)
# Каталог персональных аккаунтов: <токен подписки>.json
VLESSHUB_ACCOUNTS_DIR = Path(
    _env(
        "VLESSHUB_ACCOUNTS_DIR",
        str(VLESSHUB_ACCOUNT_FILE.parent / "accounts"),
    )
)

XUI_BASE_URL = _env("XUI_BASE_URL").rstrip("/")
XUI_USERNAME = _env("XUI_USERNAME")
XUI_PASSWORD = _env("XUI_PASSWORD")
XUI_INBOUND_ID = _env_int("XUI_INBOUND_ID", 1)
XUI_INBOUND_REMARK = _env("XUI_INBOUND_REMARK")  # если задан — ищем inbound по remark
XUI_SERVER_ADDRESS = _env("XUI_SERVER_ADDRESS")  # адрес, который попадёт в ссылку
XUI_VERIFY_TLS = _env_bool("XUI_VERIFY_TLS", True)
XUI_TOTAL_GB = _env_float("XUI_TOTAL_GB", 0)  # 0 — безлимит
XUI_FLOW = _env("XUI_FLOW")  # обычно xtls-rprx-vision; пусто — взять из inbound

# ── Прочее ────────────────────────────────────────────────────────────────────
DB_PATH = Path(_env("DB_PATH", str(BASE_DIR / "livpn.db")))
HOST = _env("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8080)
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
REFRESH_CHECK_SECONDS = _env_int("REFRESH_CHECK_SECONDS", 900)
REFRESH_WORKERS = _env_int("REFRESH_WORKERS", 3)
RATE_LIMIT_PER_MINUTE = _env_int("RATE_LIMIT_PER_MINUTE", 240)
TRUST_PROXY = _env_bool("TRUST_PROXY", False)
WEB_TITLE = _env("WEB_TITLE", "LiVPN")
# Имя бота может определиться автоматически при старте (getMe) и сохраниться
# в этот файл — тогда веб-страницы показывают актуальную ссылку.
BOT_USERNAME_FILE = Path(
    _env("BOT_USERNAME_FILE", str(BASE_DIR / "bot_username.txt"))
)


def _port_suffix(port: int) -> str:
    return "" if port in (80, 443) else f":{port}"


_detect_lock = threading.Lock()
_detected: dict[str, float | str] = {"value": "", "ts": 0.0, "ttl": 3600.0}
_file_cache: dict[str, float | str] = {"value": "", "mtime": 0.0}

_ip_lock = threading.Lock()
_ip_cache: dict[str, float | str] = {"value": "", "ts": 0.0}

_dns_lock = threading.Lock()
_dns_cache: dict[str, tuple[float, bool]] = {}

# Уникальный маркер процесса: по нему проверяем, что домен ведёт именно
# на этот сервер (работает и через CDN/туннель, где DNS-проверка бесполезна).
INSTANCE_ID = secrets.token_urlsafe(12)
_self_check_lock = threading.Lock()
_self_check_cache: dict[str, tuple[float, bool | None]] = {}

_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}"
    r"(?:\.[a-z0-9-]{1,63})*(?::\d{1,5})?$",
    re.IGNORECASE,
)


def _host_name(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if not host or host.startswith("["):
        return ""
    return host


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_private_or_local(host: str) -> bool:
    name = _host_name(host).split(":")[0]
    if not name:
        return True
    if name in ("localhost", "localhost.localdomain") or name.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_allowed(host: str) -> bool:
    """Проверка Host/домена. ALLOWED_HOSTS — необязательный белый список."""
    host = _host_name(host)
    if not host or not _HOST_RE.fullmatch(host):
        return False
    if _is_private_or_local(host):
        return False
    if not ALLOWED_HOSTS:
        return True

    name = host.split(":")[0]
    allowed = set()
    for item in ALLOWED_HOSTS:
        item = item.removeprefix("https://").removeprefix("http://").rstrip("/")
        if item:
            allowed.add(item.lower())
    return host in allowed or name in allowed


def _proxies() -> dict[str, str] | None:
    proxy_url = PROXY_URL or TELEGRAM_PROXY
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _host_points_to_this_server(host: str) -> bool | None:
    """Домен из Host резолвится в публичный IP этого сервера?

    Так поддельный `Host: evil.example` не может навсегда подменить адрес
    в ссылках подписки. IP-адреса пропускаем как есть.

    None — проверить не удалось (DNS или api.ipify.org недоступны).
    """
    name = _host_name(host).split(":")[0]
    if not name:
        return False
    if _is_ip(name):
        return True

    now = time.time()
    with _dns_lock:
        cached = _dns_cache.get(name)
    if cached and now - cached[0] < 3600:
        return cached[1]

    result: bool | None
    try:
        infos = socket.getaddrinfo(name, None)
        addresses = {str(info[4][0]) for info in infos if info[4]}
    except OSError:
        # Домен не резолвится — точно не наш.
        result = False
    else:
        server_ip = _public_ip(_proxies())
        if not server_ip:
            # Не знаем свой IP (нет сети/заблокирован ipify). Не блокируем
            # сервис: пусть домен запомнится, но результат не кешируем.
            return None
        result = server_ip in addresses

    with _dns_lock:
        if len(_dns_cache) > 1000:
            _dns_cache.clear()
        _dns_cache[name] = (now, result)
    return result


def _serves_this_instance(scheme: str, host: str) -> bool | None:
    """Проверить домен запросом к его же /healthz.

    DNS-проверка ломается за CDN/туннелем (домен резолвится в чужие IP).
    Здесь мы просим сам домен назвать INSTANCE_ID процесса: совпал —
    домен точно наш. None — проверить не удалось (нет сети/таймаут).
    """
    key = f"{scheme}://{host}"
    now = time.time()
    with _self_check_lock:
        cached = _self_check_cache.get(key)
    if cached and now - cached[0] < 3600:
        return cached[1]

    result: bool | None
    try:
        import requests

        def get(path: str):
            return requests.get(
                f"{key}{path}",
                timeout=6,
                proxies=_proxies(),
                headers={"User-Agent": "LiVPN domain-check"},
                allow_redirects=True,
            )

        response = get("/healthz")
    except Exception:
        result = None
    else:
        payload = {}
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.ok and isinstance(payload, dict) and "instance" in payload:
            result = payload.get("instance") == INSTANCE_ID
        else:
            # Прокси мог не пропустить /healthz. Проверяем заголовок
            # на главной странице.
            try:
                home = get("/")
            except Exception:
                result = None
            else:
                marker = home.headers.get("X-LiVPN-Instance")
                if marker:
                    result = marker == INSTANCE_ID
                else:
                    result = False

    with _self_check_lock:
        if len(_self_check_cache) > 1000:
            _self_check_cache.clear()
        _self_check_cache[key] = (now, result)
    return result


def _read_base_url_file() -> str:
    try:
        stat = BASE_URL_FILE.stat()
    except OSError:
        return ""
    with _detect_lock:
        if stat.st_mtime_ns == _file_cache.get("mtime"):
            return str(_file_cache.get("value") or "")
        try:
            value = BASE_URL_FILE.read_text("utf-8").strip().rstrip("/")
        except OSError:
            return ""
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not host_allowed(parts.netloc):
            value = ""
        _file_cache["value"] = value
        _file_cache["mtime"] = stat.st_mtime_ns
        return value


def remember_base_url(url: str, force: bool = False) -> str:
    """Запомнить домен, с которого пришёл запрос.

    Защита от подмены Host: приватные адреса и localhost отклоняются,
    уже сохранённый домен не перезаписывается другим доменом без force.
    IP можно заменить на домен автоматически.
    """
    if not AUTO_DOMAIN and not force:
        return ""

    value = (url or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("нужен http:// или https://")
    if parts.path not in ("", "/"):
        raise ValueError("в адресе не должно быть пути")
    host = _host_name(parts.netloc)
    if not host_allowed(host):
        raise ValueError("недопустимый или приватный домен")
    if AUTO_DOMAIN_VERIFY and not force:
        verified = _host_points_to_this_server(host)
        if verified is False:
            # За CDN/туннелем DNS указывает на чужие IP. Спросим домен
            # напрямую: если /healthz вернул наш INSTANCE_ID — всё в порядке.
            probe = _serves_this_instance(parts.scheme, host)
            if probe is True:
                verified = True
            elif probe is None:
                verified = None
        if verified is False:
            raise ValueError(
                "домен не указывает на этот сервер; задайте PUBLIC_BASE_URL "
                "или вызовите /setdomain"
            )
        if verified is None:
            # Свой IP узнать не удалось (например, ipify недоступен).
            # Лучше запомнить реальный домен, чем отдать клиенту 127.0.0.1.
            import logging

            logging.getLogger("livpn.config").warning(
                "Не удалось проверить домен %s (нет доступа к api.ipify.org)", host
            )
    if parts.scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    elif parts.scheme == "https" and host.endswith(":443"):
        host = host[:-4]

    value = f"{parts.scheme}://{host}"
    current = _read_base_url_file()
    if current == value:
        return value
    if current and not force:
        old = urlsplit(current)
        old_host = old.hostname or ""
        new_host = parts.hostname or ""
        same_domain = old_host and old_host == new_host
        # Разрешаем замену IP → домен и апгрейд http → https для того же
        # домена. Остальное не перезаписываем: Host-заголовок не должен
        # уводить ссылки на чужой домен.
        upgrade_to_https = same_domain and old.scheme == "http" and parts.scheme == "https"
        ip_to_domain = _is_ip(old_host) and not _is_ip(new_host)
        if not (upgrade_to_https or ip_to_domain):
            return current

    try:
        BASE_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BASE_URL_FILE.with_name(BASE_URL_FILE.name + ".tmp")
        tmp.write_text(value + "\n", "utf-8")
        os.replace(tmp, BASE_URL_FILE)
    except OSError:
        BASE_URL_FILE.write_text(value + "\n", "utf-8")

    try:
        stat = BASE_URL_FILE.stat()
        with _detect_lock:
            _file_cache["value"] = value
            _file_cache["mtime"] = stat.st_mtime_ns
    except OSError:
        pass
    return value


def base_url_source() -> str:
    if PUBLIC_BASE_URL:
        return "PUBLIC_BASE_URL"
    if _read_base_url_file():
        return "авто (base_url.txt)"
    return "авто (внешний IP)"


def _public_ip(proxies: dict[str, str] | None = None) -> str:
    now = time.time()
    with _ip_lock:
        cached = str(_ip_cache.get("value") or "")
        if cached and now - float(_ip_cache.get("ts") or 0.0) < 3600:
            return cached
    try:
        import requests

        response = requests.get(
            "https://api.ipify.org",
            params={"format": "json"},
            timeout=6,
            proxies=proxies,
        )
        response.raise_for_status()
        payload = response.json()
        ip = str(payload.get("ip") or "").strip()
    except Exception:
        return ""
    if ip:
        with _ip_lock:
            _ip_cache["value"] = ip
            _ip_cache["ts"] = now
    return ip


def base_url() -> str:
    """Публичный адрес сервиса.

    Приоритет:
      1. PUBLIC_BASE_URL;
      2. домен, запомненный из реального запроса (base_url.txt);
      3. внешний IP через api.ipify.org;
      4. 127.0.0.1.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL

    saved = _read_base_url_file()
    if saved:
        return saved

    now = time.time()
    with _detect_lock:
        value = str(_detected.get("value") or "")
        ts = float(_detected.get("ts") or 0.0)
        ttl = float(_detected.get("ttl") or 3600.0)
        if value and now - ts < ttl:
            return value

    ip = _public_ip(_proxies())
    if ip:
        value = f"http://{ip}{_port_suffix(PORT)}"
        ttl = 3600.0
    else:
        # Не кешируем неудачу на час: как только сеть появится, адрес
        # определится заново.
        value = f"http://127.0.0.1{_port_suffix(PORT)}"
        ttl = 60.0
    with _detect_lock:
        _detected["value"] = value
        _detected["ts"] = now
        _detected["ttl"] = ttl
    return value


def sub_url(token: str) -> str:
    return f"{base_url()}/sub/{token}"


_ACCOUNT_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]+")


def vlesshub_account_path(token: str) -> Path:
    """Файл персонального аккаунта VLESSHub для подписки с этим токеном."""
    safe = _ACCOUNT_TOKEN_RE.sub("", token or "")[:64] or "shared"
    return VLESSHUB_ACCOUNTS_DIR / f"{safe}.json"


# ── Ссылки для веба ──────────────────────────────────────────────────────────

_BOT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")


def bot_username() -> str:
    """@username бота без собаки: из .env или из файла, сохранённого ботом."""
    if BOT_USERNAME:
        return BOT_USERNAME.lstrip("@")
    try:
        name = BOT_USERNAME_FILE.read_text("utf-8").strip().lstrip("@")
    except OSError:
        return ""
    return name if _BOT_NAME_RE.fullmatch(name) else ""


def set_bot_username(name: str) -> str:
    """Запомнить реальное имя бота (после getMe), чтобы веб давал ссылку."""
    global BOT_USERNAME
    value = (name or "").strip().lstrip("@")
    if not _BOT_NAME_RE.fullmatch(value):
        return bot_username()
    BOT_USERNAME = value
    try:
        BOT_USERNAME_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOT_USERNAME_FILE.write_text(value + "\n", "utf-8")
    except OSError:
        pass
    return value


def bot_url() -> str:
    name = bot_username()
    return f"https://t.me/{name}" if name else ""


def dashboard_url(token: str) -> str:
    return f"{base_url()}/s/{token}"
