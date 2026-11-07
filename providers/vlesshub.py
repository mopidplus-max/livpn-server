"""VLESSHub provider: fetches the subscription attached to account.json."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

import requests

import config
from providers.base import Node, ProviderError
from register import ensure_account
from xray import Traffic, parse_userinfo
from xrjson import nodes_from_text

log = logging.getLogger("livpn.provider.vlesshub")


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, "", ""))
    except ValueError:
        return "<url>"


class VlessHubProvider:
    key = "vlesshub"

    def fetch(self, sub) -> tuple[list[Node], Traffic]:
        try:
            account = ensure_account()
        except Exception as exc:
            raise ProviderError(f"Не удалось подготовить VLESSHub: {exc}") from exc

        url = (account.get("subscription_url") or "").strip()
        if not url:
            raise ProviderError("В account.json отсутствует subscription_url")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ProviderError("VLESSHub вернул некорректную ссылку подписки")

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
            raise ProviderError(f"VLESSHub subscription недоступна: {exc}") from exc

        if not response.ok:
            raise ProviderError(
                f"VLESSHub subscription вернула HTTP {response.status_code} "
                f"({_safe_url(url)})"
            )

        uris = nodes_from_text(response.text)
        if not uris:
            raise ProviderError(
                "В ответе VLESSHub нет узлов. Возможно, URL ведёт на страницу "
                "входа или подписка истекла."
            )

        traffic = parse_userinfo(response.headers.get("Subscription-Userinfo"))
        if traffic is None:
            # Не обнуляем трафик при сбое источника.
            try:
                traffic = Traffic(sub["upload"], sub["download"], sub["total"])
            except (KeyError, IndexError):
                traffic = Traffic()
        return uris, traffic
