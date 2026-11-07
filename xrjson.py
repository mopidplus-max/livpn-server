"""Разбор ответов подписки: JSON-конфиги Xray, base64 и обычные share-ссылки.

VLESSHub (и некоторые другие панели) отдают не список vless://, а JSON-массив
полных конфигов Xray: [{ "remarks": "...", "outbounds": [...] }, ...].
Раньше такой ответ целиком сохранялся как один «узел». Этот модуль превращает
каждый proxy-outbound в нормальную share-ссылку.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from urllib.parse import quote, urlencode

import xray
from providers.base import Node

log = logging.getLogger("livpn.xrjson")

# «Авто»-конфиг панели — это агрегатор всех серверов, а не отдельные узлы.
# Если рядом есть конфиги с нормальными названиями стран, такие дубли
# отбрасываются: иначе в подписке появляются десятки «Автоподборов».
_AUTO_NAME_RE = re.compile(
    r"(?i)(?<![a-zа-яё])(?:авто|автоподбор|auto|automatic|best|"
    r"самый быстрый|оптимальн)"
)
_EXPIRY_RE = re.compile(
    r"(?i)(?:\b\d+\s*(?:день|дня|дней)\s+осталось|"
    r"\bосталось\s+\d+\s*(?:день|дня|дней)|"
    r"\b(?:expires?|expired|истёк|истек|trial)\b)"
)

# outbound'ы, которые не являются узлами
_NON_PROXY_PROTOCOLS = frozenset(
    {"freedom", "blackhole", "dns", "direct", "loopback", "socks", "http"}
)
_NON_PROXY_TAGS = frozenset(
    {"direct", "block", "dns", "freedom", "blackhole", "bypass", "proxy-block"}
)
_PROXY_PROTOCOLS = frozenset({"vless", "trojan", "vmess", "shadowsocks", "ss"})


def _dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value) -> str:
    return str(value or "").strip()


# ── streamSettings → параметры ссылки ────────────────────────────────────────

def stream_options(stream: dict) -> dict:
    stream = _dict(stream)
    network = _as_text(stream.get("network")).lower() or "tcp"
    if network == "raw":
        network = "tcp"
    security = _as_text(stream.get("security")).lower() or "none"
    if security == "xtls":
        security = "tls"

    result: dict = {"network": network, "security": security}

    if security == "reality":
        reality = _dict(stream.get("realitySettings"))
        inner = _dict(reality.get("settings"))
        names = reality.get("serverNames") or []
        if isinstance(names, str):
            names = [names]
        short_ids = reality.get("shortIds") or []
        if isinstance(short_ids, str):
            short_ids = [short_ids]
        result.update(
            sni=_as_text(
                inner.get("serverName")
                or reality.get("serverName")
                or (names[0] if names else "")
            ),
            fp=_as_text(
                inner.get("fingerprint")
                or reality.get("fingerprint")
                or "chrome"
            ),
            pbk=_as_text(
                inner.get("publicKey") or reality.get("publicKey") or ""
            ),
            sid=_as_text(
                inner.get("shortId")
                or reality.get("shortId")
                or (short_ids[0] if short_ids else "")
            ),
            spx=_as_text(
                inner.get("spiderX") or reality.get("spiderX") or "/"
            ),
        )
    elif security == "tls":
        tls = _dict(stream.get("tlsSettings"))
        alpn = tls.get("alpn") or []
        if isinstance(alpn, list):
            alpn = ",".join(_as_text(item) for item in alpn if _as_text(item))
        result.update(
            sni=_as_text(tls.get("serverName")),
            fp=_as_text(tls.get("fingerprint") or "chrome"),
            alpn=_as_text(alpn),
        )

    if network in ("ws", "httpupgrade", "xhttp", "http"):
        key = {
            "ws": "wsSettings",
            "httpupgrade": "httpupgradeSettings",
            "xhttp": "xhttpSettings",
            "http": "httpSettings",
        }[network]
        options = _dict(stream.get(key))
        headers = _dict(options.get("headers"))
        result.update(
            path=_as_text(options.get("path")),
            host=_as_text(
                headers.get("Host")
                or headers.get("host")
                or options.get("host")
            ),
        )
    elif network == "grpc":
        grpc = _dict(stream.get("grpcSettings"))
        result["service_name"] = _as_text(grpc.get("serviceName"))
    elif network == "tcp":
        tcp = _dict(stream.get("tcpSettings"))
        header = _dict(tcp.get("header"))
        if _as_text(header.get("type")).lower() == "http":
            request = _dict(header.get("request"))
            headers = _dict(request.get("headers"))
            hosts = headers.get("Host") or headers.get("host") or []
            if isinstance(hosts, list):
                hosts = hosts[0] if hosts else ""
            result.update(
                path=_as_text(request.get("path") or "/"),
                host=_as_text(hosts),
            )
    return result


# ── outbound → ссылка ───────────────────────────────────────────────────────

def _vless_uri(server: dict, user: dict, options: dict) -> str:
    host = _as_text(server.get("address") or server.get("host"))
    port = _int(server.get("port"))
    uuid = _as_text(user.get("id") or user.get("uuid"))
    if not host or not port or not uuid:
        return ""
    return xray.build_vless_uri(
        uuid,
        host,
        port,
        network=options.get("network", "tcp"),
        security=options.get("security", "none"),
        sni=options.get("sni", ""),
        fp=options.get("fp", "chrome"),
        pbk=options.get("pbk", ""),
        sid=options.get("sid", ""),
        spx=options.get("spx", "/"),
        flow=_as_text(user.get("flow")),
        path=options.get("path", ""),
        host_header=options.get("host", ""),
        service_name=options.get("service_name", ""),
        alpn=options.get("alpn", ""),
    )


def _trojan_uri(server: dict, options: dict, name: str) -> str:
    host = _as_text(server.get("address") or server.get("host"))
    port = _int(server.get("port"))
    password = _as_text(server.get("password"))
    if not host or not port or not password:
        return ""
    query: list[tuple[str, str]] = []
    for key in ("network", "security"):
        value = options.get(key)
        if value:
            query.append(("type" if key == "network" else "security", str(value)))
    for key, value in (
        ("sni", options.get("sni")),
        ("fp", options.get("fp")),
        ("pbk", options.get("pbk")),
        ("sid", options.get("sid")),
        ("spx", options.get("spx")),
        ("path", options.get("path")),
        ("host", options.get("host")),
        ("serviceName", options.get("service_name")),
        ("alpn", options.get("alpn")),
    ):
        if value:
            query.append((key, str(value)))
    uri = f"trojan://{quote(password, safe='')}@{host}:{port}"
    if query:
        uri += "?" + urlencode(query)
    if name:
        uri += "#" + quote(name, safe="")
    return uri


def outbound_uris(outbound: dict, name: str = "") -> list[str]:
    protocol = _as_text(outbound.get("protocol")).lower()
    tag = _as_text(outbound.get("tag"))
    if protocol in _NON_PROXY_PROTOCOLS or tag.lower() in _NON_PROXY_TAGS:
        return []
    if protocol not in _PROXY_PROTOCOLS:
        return []

    settings = _dict(outbound.get("settings"))
    options = stream_options(_dict(outbound.get("streamSettings")))
    uris: list[str] = []

    if protocol == "vless":
        servers = settings.get("vnext") or []
        for server in servers if isinstance(servers, list) else []:
            if not isinstance(server, dict):
                continue
            users = server.get("users") or []
            for user in users if isinstance(users, list) else []:
                if not isinstance(user, dict):
                    continue
                uri = _vless_uri(server, user, options)
                if uri:
                    uris.append(uri)
            if not users:
                uri = _vless_uri(server, server, options)
                if uri:
                    uris.append(uri)
    elif protocol == "trojan":
        servers = settings.get("servers") or []
        for server in servers if isinstance(servers, list) else []:
            if isinstance(server, dict):
                uri = _trojan_uri(server, options, tag or name)
                if uri:
                    uris.append(uri)

    return uris


# ── JSON-пейлоад → узлы ─────────────────────────────────────────────────────

def is_auto_name(name: str) -> bool:
    return bool(_AUTO_NAME_RE.search(name or ""))


def clean_remark(name: str) -> str:
    """Убрать из названия служебный текст вроде «0 дней осталось»."""
    value = _as_text(name)
    if not value:
        return ""
    value = _EXPIRY_RE.sub(" ", value)
    value = value.replace("📅", " ")
    value = re.sub(r"\s+", " ", value).strip(" ._-|—–")
    # Остались только флаги/эмодзи — это не название.
    plain = re.sub(r"[\U0001F1E6-\U0001F1FF]", "", value)
    plain = re.sub(r"[^\w]+", "", plain, flags=re.UNICODE)
    if not plain:
        return ""
    return value


def _endpoint_key(uri: str) -> str:
    return xray.uri_endpoint(uri) or uri


def _prefer_named(nodes: list[Node]) -> list[Node]:
    """Схлопнуть авто-агрегатор, оставив нормальные названия стран.

    * точные дубли URI: побеждает узел без «Авто» в названии;
    * узел из «Авто»-конфига отбрасывается, если такой же сервер
      (host:port) уже есть в конфиге с именем страны;
    * уникальные серверы из «Авто» сохраняются.
    """
    named_endpoints = {
        _endpoint_key(node.uri)
        for node in nodes
        if node.name and not is_auto_name(node.name)
    }

    by_uri: dict[str, Node] = {}
    order: list[str] = []
    for node in nodes:
        if is_auto_name(node.name) and _endpoint_key(node.uri) in named_endpoints:
            continue
        existing = by_uri.get(node.uri)
        if existing is None:
            by_uri[node.uri] = node
            order.append(node.uri)
        elif is_auto_name(existing.name) and not is_auto_name(node.name):
            by_uri[node.uri] = node
    return [by_uri[uri] for uri in order]


def _proxy_outbounds(config: dict) -> list[dict]:
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list):
        return []
    return [
        item
        for item in outbounds
        if isinstance(item, dict)
        and _as_text(item.get("protocol")).lower() not in _NON_PROXY_PROTOCOLS
        and _as_text(item.get("protocol")).lower() in _PROXY_PROTOCOLS
    ]


def nodes_from_payload(payload, default_name: str = "") -> list[Node]:
    nodes: list[Node] = []

    if isinstance(payload, list):
        collected: list[Node] = []
        for item in payload:
            collected.extend(nodes_from_payload(item, default_name))
        return _prefer_named(collected)

    if isinstance(payload, str):
        uri = payload.strip()
        return [Node(uri)] if xray.is_share_uri(uri) else []

    if not isinstance(payload, dict):
        return []

    # Обёртки вида {"items": [...]}, {"configs": [...]}, {"data": {...}}.
    for key in ("configs", "items", "nodes", "subscription", "data"):
        value = payload.get(key)
        if isinstance(value, (list, dict)):
            inner = nodes_from_payload(value, default_name)
            if inner:
                return inner

    if "outbounds" in payload:
        name = clean_remark(
            payload.get("remarks") or payload.get("name") or default_name
        )
        outbounds = _proxy_outbounds(payload)
        if outbounds:
            for outbound in outbounds:
                uris = outbound_uris(outbound, name)
                for uri in uris:
                    node_name = name
                    if not node_name and _endpoint_key(uri).startswith("auto."):
                        node_name = "Автоподбор"
                    nodes.append(Node(uri, node_name))
            return nodes
        # Конфиг без proxy-outbound'ов — не источник узлов.
        return []

    if _as_text(payload.get("protocol")).lower() in _PROXY_PROTOCOLS:
        name = clean_remark(
            payload.get("remarks") or payload.get("name") or default_name
        )
        for uri in outbound_uris(payload, name):
            node_name = name
            if not node_name and _endpoint_key(uri).startswith("auto."):
                node_name = "Автоподбор"
            nodes.append(Node(uri, node_name))
        return nodes

    uri = _as_text(payload.get("uri") or payload.get("link") or payload.get("url"))
    if xray.is_share_uri(uri):
        nodes.append(Node(uri, clean_remark(_as_text(payload.get("name")))))
    return nodes


# ── текст ответа → узлы ─────────────────────────────────────────────────────

def _decode_json_documents(text: str):
    """Разобрать один или несколько JSON-документов подряд, терпимо к хвосту."""
    decoder = json.JSONDecoder()
    values = []
    index = 0
    length = len(text)
    for _ in range(10):
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            break
        values.append(value)
        index = end
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _decode_base64(text: str) -> str:
    compact = "".join(text.split())
    if not compact or len(compact) % 4 == 1:
        return ""
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        probe = raw.lstrip()
        if "://" in raw or probe.startswith(("{", "[")):
            return raw
    return ""


def nodes_from_text(text: str) -> list[Node]:
    """Универсальный разбор тела подписки."""
    body = (text or "").strip()
    if not body:
        return []

    if body[0] in "{[":
        payload = _decode_json_documents(body)
        if payload is not None:
            nodes = nodes_from_payload(payload)
            if nodes:
                return nodes

    if "://" not in body:
        decoded = _decode_base64(body)
        if decoded and decoded != body:
            nodes = nodes_from_text(decoded)
            if nodes:
                return nodes

    return [Node(uri) for uri in xray.decode_subscription(body)]
