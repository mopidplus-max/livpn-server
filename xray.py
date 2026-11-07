"""Утилиты Xray: разбор/сборка vless-ссылок и subscription-пейлоада."""
from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

# Схемы, которые клиенты (Happ / Incy / v2rayNG / Streisand) понимают
# как настоящий узел. Всё остальное — комментарии, ссылки на сайты и мусор.
SHARE_SCHEMES = frozenset(
    {
        "vless",
        "vmess",
        "trojan",
        "ss",
        "ssr",
        "hysteria",
        "hysteria2",
        "hy2",
        "tuic",
        "wireguard",
        "socks",
    }
)


def is_share_uri(value: str) -> bool:
    """Похоже ли значение на реальную share-ссылку узла."""
    if not isinstance(value, str):
        return False
    uri = value.strip()
    if not uri or uri.startswith("#") or "://" not in uri:
        return False
    scheme = uri.split("://", 1)[0].strip().lower()
    if scheme not in SHARE_SCHEMES:
        return False
    rest = uri.split("://", 1)[1]
    return bool(rest) and not rest[0].isspace()


# ── Имена узлов ───────────────────────────────────────────────────────────────

def uri_name(uri: str) -> str:
    """Имя узла из фрагмента ссылки (#...)."""
    if "#" not in uri:
        return ""
    frag = uri.split("#", 1)[1]
    try:
        return unquote(frag)
    except Exception:
        return frag


def with_fragment(uri: str, name: str) -> str:
    """Подменить отображаемое имя узла, не трогая остальные параметры."""
    base = uri.split("#", 1)[0]
    return f"{base}#{quote(name, safe='')}"


def rename_uri(uri: str, name: str) -> str:
    """То же, что with_fragment, но работает с любой share-ссылкой.

    Не-ссылки (JSON, HTML, комментарии) не трогаем: именно из-за этого
    «переименования» весь JSON-конфиг VLESSHub превращался в один узел.
    """
    uri = (uri or "").strip()
    if not name or not is_share_uri(uri):
        return uri
    return with_fragment(uri, name)


# ── Сборка vless-ссылки ───────────────────────────────────────────────────────

def build_vless_uri(
    uuid: str,
    host: str,
    port: int,
    *,
    network: str = "tcp",
    security: str = "none",
    sni: str = "",
    fp: str = "chrome",
    pbk: str = "",
    sid: str = "",
    spx: str = "",
    flow: str = "",
    path: str = "",
    host_header: str = "",
    service_name: str = "",
    alpn: str = "",
    name: str = "",
) -> str:
    """Собрать vless://-ссылку, совместимую с Happ / Incy / v2rayNG."""
    params: list[tuple[str, str]] = [("type", network), ("security", security)]

    if security == "reality":
        params += [("pbk", pbk), ("fp", fp)]
        if sid:
            params.append(("sid", sid))
        if spx:
            params.append(("spx", spx))
    elif security == "tls":
        if fp:
            params.append(("fp", fp))
        if alpn:
            params.append(("alpn", alpn))

    if sni:
        params.append(("sni", sni))
    if flow:
        params.append(("flow", flow))
    if path:
        params.append(("path", path))
    if host_header:
        params.append(("host", host_header))
    if service_name:
        params.append(("serviceName", service_name))

    query = urlencode([(k, v) for k, v in params if v], safe="")
    base = f"vless://{uuid}@{host}:{port}"
    uri = f"{base}?{query}" if query else base
    return with_fragment(uri, name) if name else uri


def uri_endpoint(uri: str) -> str:
    """host:port или пустая строка — для дедупликации/диагностики."""
    try:
        parts = urlsplit(uri)
        return f"{parts.hostname}:{parts.port}" if parts.hostname else ""
    except ValueError:
        return ""


# ── Subscription ──────────────────────────────────────────────────────────────

def encode_subscription(uris: list[str]) -> str:
    """Классический формат подписки: base64 от ссылок, каждая с новой строки."""
    body = "\n".join(uri.strip() for uri in uris if uri and uri.strip())
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


def decode_subscription(body: str) -> list[str]:
    """Разобрать ответ сервера-источника. Понимает base64 и plain-текст.

    Возвращает только настоящие share-ссылки: строки-комментарии, ссылки
    на сайты и JSON/HTML отбрасываются.
    """
    text = (body or "").strip()
    if not text:
        return []

    if "://" not in text:
        padded = text + "=" * (-len(text) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                text = decoder(padded).decode("utf-8")
                break
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue

    result: list[str] = []
    for line in text.splitlines():
        uri = line.strip().strip("\"'").strip()
        if is_share_uri(uri):
            result.append(uri)
    return result


def b64_header(text: str) -> str:
    """Некоторые клиенты понимают не-ASCII заголовки только в base64."""
    return "base64:" + base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def subscription_headers(
    traffic: "Traffic",
    *,
    title: str,
    description: str = "",
    interval_hours: int = 24,
    web_url: str = "",
) -> dict[str, str]:
    """HTTP-заголовки подписки для Happ / Incy / v2rayNG / Streisand."""
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Profile-Title": b64_header(title),
        "Profile-Update-Interval": str(max(1, int(interval_hours))),
        "Subscription-Userinfo": (
            f"upload={traffic.upload}; download={traffic.download}; "
            f"total={traffic.total}"
        ),
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }
    if description:
        headers["Profile-Description"] = b64_header(description)
    if web_url:
        headers["Profile-Web-Page-Url"] = web_url
    return headers


# ── Трафик ────────────────────────────────────────────────────────────────────

class Traffic(dict):
    """upload/download/total в байтах. total == 0 → безлимит."""

    def __init__(self, upload: int = 0, download: int = 0, total: int = 0):
        super().__init__(
            upload=int(upload or 0),
            download=int(download or 0),
            total=int(total or 0),
        )

    @property
    def upload(self) -> int:
        return self["upload"]

    @property
    def download(self) -> int:
        return self["download"]

    @property
    def total(self) -> int:
        return self["total"]

    @property
    def used(self) -> int:
        return self["upload"] + self["download"]


def parse_userinfo(value: str | None) -> Traffic | None:
    """Разобрать заголовок Subscription-Userinfo."""
    if not value:
        return None
    data: dict[str, int] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, _, raw = part.partition("=")
        key = key.strip().lower()
        try:
            data[key] = int(float(raw.strip()))
        except ValueError:
            continue
    if not data:
        return None
    return Traffic(data.get("upload", 0), data.get("download", 0), data.get("total", 0))


def human_bytes(value: int) -> str:
    """1.2 GB, 512 MB и т.п."""
    value = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f}".rstrip("0").rstrip(".") + f" {unit}"
        size /= 1024
    return f"{value} B"


# ── Совместимость со старым кодом ─────────────────────────────────────────────

def file_name_from_url(url: str) -> str:
    """Имя файла подписки для Content-Disposition."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in url)
    return safe[:64]


def parse_query(uri: str) -> dict[str, str]:
    try:
        return dict(parse_qsl(urlsplit(uri).query, keep_blank_values=True))
    except ValueError:
        return {}
