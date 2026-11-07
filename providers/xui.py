"""Провайдер xui: собственная панель 3x-ui.

На каждого подписчика создаётся отдельный клиент в inbound, поэтому
трафик считается панелью, а не выдумывается. Ссылка собирается из
streamSettings inbound'а (Reality / TLS / WS / gRPC / xhttp).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid as uuidlib
from urllib.parse import quote, urlsplit

import requests

import config
import db
from providers.base import Node, ProviderError
from xray import Traffic, build_vless_uri

log = logging.getLogger("livpn.provider.xui")


def _json_field(value, default):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


class XuiProvider:
    key = "xui"

    def __init__(self) -> None:
        self._session: requests.Session | None = None
        self._lock = threading.RLock()
        self._inbound_cache: dict | None = None
        self._inbound_ts = 0.0

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _login_locked(self) -> None:
        if not config.XUI_BASE_URL:
            raise ProviderError("Не задан XUI_BASE_URL")
        if not config.XUI_USERNAME or not config.XUI_PASSWORD:
            raise ProviderError("Не заданы XUI_USERNAME / XUI_PASSWORD")

        session = requests.Session()
        try:
            response = session.post(
                config.XUI_BASE_URL + "/login",
                json={
                    "username": config.XUI_USERNAME,
                    "password": config.XUI_PASSWORD,
                },
                timeout=config.HTTP_TIMEOUT,
                verify=config.XUI_VERIFY_TLS,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"3x-ui недоступен: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"3x-ui login вернул не-JSON (HTTP {response.status_code})"
            ) from exc

        if not response.ok or not data.get("success"):
            raise ProviderError(
                "3x-ui: не удалось войти. Проверьте XUI_BASE_URL, "
                "XUI_USERNAME и XUI_PASSWORD."
            )

        self._session = session
        log.info("3x-ui: вход выполнен")

    def _request_locked(self, method: str, path: str, **kwargs):
        if self._session is None:
            self._login_locked()

        assert self._session is not None
        response = self._session.request(
            method,
            config.XUI_BASE_URL + path,
            timeout=config.HTTP_TIMEOUT,
            verify=config.XUI_VERIFY_TLS,
            **kwargs,
        )

        # Сессия могла истечь: панель отдаёт login-страницу или 401/403.
        content_type = response.headers.get("Content-Type", "")
        response_path = urlsplit(response.url).path.rstrip("/")
        looks_like_login = (
            response.status_code in (401, 403)
            or "text/html" in content_type
            or response_path.endswith("/login")
        )
        if looks_like_login:
            self._login_locked()
            assert self._session is not None
            response = self._session.request(
                method,
                config.XUI_BASE_URL + path,
                timeout=config.HTTP_TIMEOUT,
                verify=config.XUI_VERIFY_TLS,
                **kwargs,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"3x-ui вернул не-JSON (HTTP {response.status_code})"
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"3x-ui HTTP {response.status_code}: "
                f"{str(data.get('msg') or data)[:200]}"
            )
        if data.get("success") is False:
            raise ProviderError(
                "3x-ui: "
                + str(data.get("msg") or data.get("message") or "неизвестная ошибка")[:250]
            )
        return data

    def _api(self, method: str, path: str, **kwargs):
        with self._lock:
            return self._request_locked(method, path, **kwargs)

    # ── Inbound ──────────────────────────────────────────────────────────────

    def _inbound(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._inbound_cache and now - self._inbound_ts < 120:
            return self._inbound_cache

        if config.XUI_INBOUND_REMARK:
            data = self._api("GET", "/panel/api/inbounds/list")
            inbound = None
            for item in data.get("obj") or []:
                if str(item.get("remark", "")).strip() == config.XUI_INBOUND_REMARK:
                    inbound = item
                    break
            if inbound is None:
                raise ProviderError(
                    f"3x-ui: inbound с remark={config.XUI_INBOUND_REMARK!r} не найден"
                )
        else:
            data = self._api("GET", f"/panel/api/inbounds/get/{config.XUI_INBOUND_ID}")
            inbound = data.get("obj")
            if not inbound:
                raise ProviderError(
                    f"3x-ui: inbound id={config.XUI_INBOUND_ID} не найден"
                )

        self._inbound_cache = inbound
        self._inbound_ts = now
        return inbound

    # ── Клиент ───────────────────────────────────────────────────────────────

    @staticmethod
    def _client_email(sub) -> str:
        try:
            saved = (sub["client_email"] or "").strip()
        except (KeyError, IndexError):
            saved = ""
        if saved:
            return saved
        token = str(sub["token"] or "")
        return f"livpn-{int(sub['id'])}-{token[:8]}"

    def _ensure_client(self, sub, inbound: dict) -> tuple[str, str, str]:
        settings = _json_field(inbound.get("settings"), {})
        clients = settings.get("clients") if isinstance(settings, dict) else []
        clients = clients or []

        email = self._client_email(sub)
        try:
            uuid = (sub["client_uuid"] or "").strip()
        except (KeyError, IndexError):
            uuid = ""
        uuid = uuid or str(uuidlib.uuid4())

        existing = next(
            (c for c in clients if str(c.get("email", "")).strip() == email),
            None,
        )
        if existing:
            uuid = str(existing.get("id") or uuid)
            flow = str(existing.get("flow") or "")
            try:
                if sub["client_uuid"] != uuid or sub["client_email"] != email:
                    db.set_client(int(sub["id"]), uuid, email)
            except (KeyError, IndexError):
                pass
            return uuid, email, flow

        # Берём flow, который уже используется в inbound (обычно xtls-rprx-vision).
        flow = config.XUI_FLOW
        if not flow:
            for client in clients:
                if client.get("flow"):
                    flow = str(client["flow"])
                    break

        total_bytes = int(max(0.0, config.XUI_TOTAL_GB) * 1024**3)
        client = {
            "id": uuid,
            "flow": flow,
            "email": email,
            "limitIp": 0,
            "totalGB": total_bytes,
            "expiryTime": 0,
            "enable": True,
            "tgId": str(sub["tg_id"]),
            "subId": str(sub["token"])[:16],
            "comment": "LiVPN",
            "reset": 0,
        }

        try:
            self._api(
                "POST",
                "/panel/api/inbounds/addClient",
                json={
                    "id": int(inbound["id"]),
                    "settings": json.dumps({"clients": [client]}, ensure_ascii=False),
                },
            )
        except ProviderError as exc:
            # Гонка: клиент уже создан параллельным запросом.
            error_text = str(exc).lower()
            if (
                "already" not in error_text
                and "duplicate" not in error_text
                and "существ" not in error_text
            ):
                raise
            fresh = self._inbound(force=True)
            fresh_settings = _json_field(fresh.get("settings"), {})
            found = next(
                (
                    c
                    for c in (fresh_settings.get("clients") or [])
                    if str(c.get("email", "")).strip() == email
                ),
                None,
            )
            if found:
                uuid = str(found.get("id") or uuid)
                flow = str(found.get("flow") or flow)
            else:
                raise

        self._inbound_ts = 0.0
        db.set_client(int(sub["id"]), uuid, email)
        return uuid, email, flow

    # ── Ссылка ───────────────────────────────────────────────────────────────

    def _build_uri(self, inbound: dict, uuid: str, email: str, flow: str) -> str:
        if str(inbound.get("protocol", "vless")).lower() != "vless":
            raise ProviderError(
                "Провайдер xui поддерживает только inbound с протоколом VLESS"
            )

        base_host = urlsplit(config.XUI_BASE_URL).hostname or ""
        address = config.XUI_SERVER_ADDRESS or base_host
        if not address:
            raise ProviderError("Не удалось определить адрес сервера 3x-ui")

        port = int(inbound.get("port") or 443)
        stream = _json_field(inbound.get("streamSettings"), {})
        network = str(stream.get("network") or "tcp")
        security = str(stream.get("security") or "none").lower()
        if security == "xtls":
            security = "tls"

        sni = ""
        fp = ""
        pbk = ""
        sid = ""
        spx = ""
        path = ""
        host_header = ""
        service_name = ""
        alpn = ""

        if security == "reality":
            reality = _json_field(stream.get("realitySettings"), {})
            inner = _json_field(reality.get("settings"), {})
            names = reality.get("serverNames") or inner.get("serverNames") or []
            if isinstance(names, str):
                names = [names]
            sni = str(inner.get("serverName") or (names[0] if names else ""))
            pbk = str(
                inner.get("publicKey")
                or reality.get("publicKey")
                or ""
            )
            short_ids = reality.get("shortIds") or []
            if isinstance(short_ids, str):
                short_ids = [short_ids]
            sid = str(inner.get("shortId") or (short_ids[0] if short_ids else ""))
            spx = str(inner.get("spiderX") or reality.get("spiderX") or "/")
            fp = str(inner.get("fingerprint") or reality.get("fingerprint") or "chrome")
        elif security == "tls":
            tls = _json_field(stream.get("tlsSettings"), {})
            sni = str(tls.get("serverName") or "")
            fp = str(tls.get("fingerprint") or "chrome")
            alpn_list = tls.get("alpn") or []
            if isinstance(alpn_list, list):
                alpn = ",".join(str(x) for x in alpn_list if x)
            elif isinstance(alpn_list, str):
                alpn = alpn_list

        if network in ("ws", "httpupgrade", "xhttp"):
            node_settings = _json_field(
                stream.get(
                    {
                        "ws": "wsSettings",
                        "httpupgrade": "httpupgradeSettings",
                        "xhttp": "xhttpSettings",
                    }[network]
                ),
                {},
            )
            path = str(node_settings.get("path") or "")
            headers = node_settings.get("headers") or {}
            if isinstance(headers, dict):
                host_header = str(headers.get("Host") or headers.get("host") or "")
            if network == "xhttp" and not host_header:
                host_header = str(node_settings.get("host") or "")
        elif network == "grpc":
            grpc = _json_field(stream.get("grpcSettings"), {})
            service_name = str(grpc.get("serviceName") or "")
        elif network == "tcp":
            tcp = _json_field(stream.get("tcpSettings"), {})
            header = tcp.get("header") or {}
            if isinstance(header, dict) and str(header.get("type", "")).lower() == "http":
                request = header.get("request") or {}
                path = str(request.get("path") or "/")
                headers = request.get("headers") or {}
                if isinstance(headers, dict):
                    hosts = headers.get("Host") or headers.get("host") or []
                    if isinstance(hosts, list) and hosts:
                        host_header = str(hosts[0])
                    elif isinstance(hosts, str):
                        host_header = hosts

        name = (
            config.XUI_INBOUND_REMARK
            or str(inbound.get("remark") or "").strip()
            or "LiVPN"
        )

        return build_vless_uri(
            uuid,
            address,
            port,
            network=network,
            security=security,
            sni=sni,
            fp=fp or "chrome",
            pbk=pbk,
            sid=sid,
            spx=spx,
            flow=flow,
            path=path,
            host_header=host_header,
            service_name=service_name,
            alpn=alpn,
            name=name,
        )

    # ── Трафик ───────────────────────────────────────────────────────────────

    def _traffic(self, email: str, sub) -> Traffic:
        def fallback() -> Traffic:
            # Не обнуляем показанный трафик из-за разового сбоя панели.
            try:
                return Traffic(sub["upload"], sub["download"], sub["total"])
            except (KeyError, IndexError):
                return Traffic()

        path = "/panel/api/inbounds/getClientTraffics/" + quote(email, safe="")
        try:
            data = self._api("GET", path)
        except ProviderError:
            # Некоторые сборки панели не имеют этого endpoint'а.
            log.warning("3x-ui: не удалось получить трафик для %s", email)
            return fallback()
        obj = data.get("obj") or {}
        if not isinstance(obj, dict) or not obj:
            return fallback()
        return Traffic(
            obj.get("up", 0),
            obj.get("down", 0),
            obj.get("total", 0),
        )

    # ── Провайдер ────────────────────────────────────────────────────────────

    def fetch(self, sub) -> tuple[list[Node], Traffic]:
        inbound = self._inbound()
        uuid, email, flow = self._ensure_client(sub, inbound)
        uri = self._build_uri(inbound, uuid, email, flow)
        traffic = self._traffic(email, sub)
        name = (
            config.XUI_INBOUND_REMARK
            or str(inbound.get("remark") or "").strip()
            or "LiVPN"
        )
        return [Node(uri, name)], traffic
