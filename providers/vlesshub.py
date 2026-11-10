"""VLESSHub provider: у каждой подписки LiVPN свой аккаунт VLESSHub.

Общий account.json давал всем пользователям один и тот же трафик, поэтому
аккаунт создаётся отдельно для каждой подписки: /data/accounts/<токен>.json.
Когда пробный тариф заканчивается, аккаунт пересоздаётся автоматически.
"""
from __future__ import annotations

import logging
import time
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


def _account_for(sub):
    """Файл аккаунта именно этой подписки."""
    token = ""
    try:
        token = str(sub["token"] or "")
    except (KeyError, IndexError, TypeError):
        token = ""
    if not token:
        raise ProviderError("У подписки нет токена — некуда сохранить аккаунт")
    return config.vlesshub_account_path(token)


def _download(url: str) -> requests.Response:
    last: Exception | None = None
    for attempt in (1, 2, 3):
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
            last = exc
            log.warning(
                "VLESSHub: попытка %s из 3 не удалась (%s)", attempt, _safe_url(url)
            )
            time.sleep(1.5 * attempt)
            continue
        return response
    raise ProviderError(f"VLESSHub subscription недоступна: {last}")


class VlessHubProvider:
    key = "vlesshub"

    def fetch(self, sub) -> tuple[list[Node], Traffic]:
        path = _account_for(sub)
        try:
            account = ensure_account(path)
        except Exception as exc:
            raise ProviderError(f"Не удалось подготовить VLESSHub: {exc}") from exc

        url = (account.get("subscription_url") or "").strip()
        if not url:
            raise ProviderError(f"В {path.name} отсутствует subscription_url")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ProviderError("VLESSHub вернул некорректную ссылку подписки")

        response = _download(url)
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
