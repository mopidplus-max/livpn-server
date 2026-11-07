"""Remnawave subscription provider.

Поддерживаются все форматы ответа, которые понимает LiVPN:
  * XRAY_JSON — JSON-конфиг (или массив конфигов) с proxy-outbound'ами;
  * XRAY_BASE64 — base64 со списком share-ссылок;
  * обычный текст со ссылками vless:// / trojan:// / vmess://.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import requests

import config
from providers.base import Node, ProviderError
from xray import Traffic, parse_userinfo
from xrjson import nodes_from_text


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        return urlunsplit(
            (parts.scheme, parts.netloc.split("@")[-1], parts.path, "", "")
        )
    except ValueError:
        return "<url>"


class RemnawaveProvider:
    key = "remnawave"

    def _url_for(self, sub) -> str:
        try:
            value = (sub["upstream_url"] or "").strip()
        except (KeyError, IndexError):
            value = ""
        return value or config.UPSTREAM_URL.strip()

    def fetch(self, sub) -> tuple[list[Node], Traffic]:
        url = self._url_for(sub)
        if not url:
            raise ProviderError(
                "Не задана ссылка Remnawave. Укажите UPSTREAM_URL в .env"
            )
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ProviderError(
                "Ссылка Remnawave должна начинаться с http:// или https://"
            )

        try:
            response = requests.get(
                url,
                headers={
                    # Remnawave selects the subscription body using User-Agent.
                    "User-Agent": config.UPSTREAM_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=config.HTTP_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Remnawave недоступен: {exc}") from exc

        if not response.ok:
            raise ProviderError(
                f"Remnawave вернул HTTP {response.status_code} "
                f"({_safe_url(url)})"
            )

        body = (response.text or "").strip()
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type or body[:100].lower().startswith(
            ("<!doctype html", "<html")
        ):
            raise ProviderError(
                "Remnawave вернул HTML вместо подписки. Проверь URL и "
                "Subscription Response Rules в панели."
            )

        nodes = nodes_from_text(body)
        if not nodes:
            raise ProviderError(
                "Не удалось найти поддерживаемые узлы в ответе Remnawave. "
                f"Content-Type: {content_type or 'не указан'}. "
                "Ожидался JSON-конфиг Xray, base64 или текст со ссылками."
            )

        traffic = parse_userinfo(response.headers.get("Subscription-Userinfo"))
        if traffic is None:
            # Не обнуляем известный трафик, если источник его не прислал.
            try:
                traffic = Traffic(sub["upload"], sub["download"], sub["total"])
            except (KeyError, IndexError):
                traffic = Traffic()
        return nodes, traffic
