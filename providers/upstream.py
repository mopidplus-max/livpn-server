"""Провайдер upstream: репроксирование ВАШЕЙ официальной подписки.

Это не обход чужих триалов: URL — ваша купленная/выданная подписка.
Сервис забирает её, переименовывает узлы и отдаёт пользователям LiVPN
в формате Xray, сохраняя трафик из заголовка Subscription-Userinfo.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

import requests

import config
from providers.base import Node, ProviderError
from xray import Traffic, parse_userinfo
from xrjson import nodes_from_text

log = logging.getLogger("livpn.provider.upstream")


def _safe_url(url: str) -> str:
    """Убрать логин/пароль из URL перед логом."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, "", ""))
    except ValueError:
        return "<url>"


class UpstreamProvider:
    key = "upstream"

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
                "Не задан UPSTREAM_URL. Укажите ссылку на вашу официальную "
                "подписку в .env или в таблице subs.upstream_url."
            )

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ProviderError("UPSTREAM_URL должен начинаться с http:// или https://")

        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": config.UPSTREAM_USER_AGENT,
                    "Accept": "*/*",
                },
                timeout=config.HTTP_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Источник подписки недоступен: {exc}") from exc

        if not response.ok:
            raise ProviderError(
                f"Источник вернул HTTP {response.status_code} ({_safe_url(url)})"
            )

        uris = nodes_from_text(response.text)
        if not uris:
            raise ProviderError(
                "В ответе источника нет ссылок. Проверьте, что URL — это "
                "subscription-ссылка, а не страница входа."
            )

        traffic = parse_userinfo(response.headers.get("Subscription-Userinfo"))
        if traffic is None:
            # Не сбрасываем уже известный трафик, если источник его не прислал.
            try:
                traffic = Traffic(sub["upload"], sub["download"], sub["total"])
            except (KeyError, IndexError):
                traffic = Traffic()

        return uris, traffic
