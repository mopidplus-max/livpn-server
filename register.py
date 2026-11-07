#!/usr/bin/env python3
"""VLESSHub bootstrap.

Создаёт аккаунт только если account.json ещё нет. Существующий аккаунт
переиспользуется; активная подписка ищется до попытки активации тарифа.
Повторная активация после неудачной/истёкшей попытки автоматически не делается.
"""
from __future__ import annotations

import json
import os
import secrets
import string
import threading
from pathlib import Path
from typing import Any

import requests

try:
    import config
    VH = config.VLESSHUB_API_URL
    ACCOUNT = config.VLESSHUB_ACCOUNT_FILE
    TIMEOUT = config.HTTP_TIMEOUT
except Exception:
    VH = os.getenv("VLESSHUB_API_URL", "https://vlesshub.fun/api").rstrip("/")
    ACCOUNT = Path(os.getenv("VLESSHUB_ACCOUNT_FILE", "account.json"))
    TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

MT = "https://api.mail.tm"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 16) Chrome/140.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
_LOCK = threading.RLock()


def password(length: int = 24) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(chars) for _ in range(length))


def _short(data: Any, limit: int = 200) -> str:
    """Короткое и безопасное описание ответа API (без токенов)."""
    if isinstance(data, dict):
        for key in ("detail", "message", "error", "msg", "detail_message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
        return "ключи ответа: " + ", ".join(sorted(map(str, data.keys())))[:limit]
    return str(data)[:limit]


def call(session, base, method, path, payload=None):
    try:
        response = session.request(
            method, base.rstrip("/") + path, json=payload, timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Ошибка соединения с {base}: {exc}") from exc
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {"raw": response.text[:300]}
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {_short(data)}")
    return data


def save(data: dict) -> None:
    ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNT.with_name(ACCOUNT.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ACCOUNT)
    try:
        ACCOUNT.chmod(0o600)
    except OSError:
        pass


def _items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "subscriptions", "plans", "results", "hydra:member"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _find_url(value: Any) -> str:
    """Достаёт subscription URL из разных вариантов ответа API."""
    preferred = ("subscription_url", "subscriptionUrl", "url", "link", "uri")
    if isinstance(value, dict):
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
        for child in value.values():
            found = _find_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_url(child)
            if found:
                return found
    elif isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return ""


def create_mail() -> dict:
    print("[VLESSHub] Создаю почту mail.tm…")
    session = requests.Session()
    session.headers.update(HEADERS)
    domains_data = call(session, MT, "GET", "/domains")
    domains = domains_data.get("hydra:member", []) if isinstance(domains_data, dict) else domains_data
    available = [
        item["domain"] for item in domains
        if isinstance(item, dict) and item.get("domain") and item.get("isActive", True)
    ]
    if not available:
        raise RuntimeError("mail.tm не вернул доступных доменов")
    email = "vh" + secrets.token_hex(5) + "@" + secrets.choice(available)
    mail_password = password()
    call(session, MT, "POST", "/accounts", {"address": email, "password": mail_password})
    auth = call(session, MT, "POST", "/token", {"address": email, "password": mail_password})
    if not auth.get("token"):
        raise RuntimeError("mail.tm не вернул токен")
    print(f"[VLESSHub] Почта создана: {email}")
    return {"email": email, "password": mail_password, "token": auth["token"]}


def register_vless(mail: dict) -> dict:
    print("[VLESSHub] Регистрирую аккаунт…")
    session = requests.Session()
    session.headers.update(HEADERS)
    vh_password = password()
    result = call(session, VH, "POST", "/auth/register", {
        "email": mail["email"],
        "password": vh_password,
        "os": "Android",
        "browser": "Chrome",
        "device": "Mobile",
    })
    token = result.get("access_token")
    if not token:
        raise RuntimeError(
            "VLESSHub не вернул access_token (" + _short(result) + ")"
        )
    account = {
        "email": mail["email"],
        "password": vh_password,
        "access_token": token,
        "user": result.get("user"),
        "mailtm": mail,
    }
    save(account)
    print("[VLESSHub] Аккаунт создан.")
    return account


def get_session(account: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Authorization"] = "Bearer " + account["access_token"]
    return session


def _active_subscription(session, account: dict) -> dict | None:
    data = call(session, VH, "GET", "/subscriptions/")
    subscriptions = _items(data)
    # Сначала предпочитаем активную подписку, затем сохранённую с URL.
    for item in subscriptions:
        if item.get("is_active") is True or item.get("status") in ("active", "ACTIVE"):
            if item.get("subscription_url") or item.get("id") is not None:
                return item
    saved = account.get("subscription")
    if isinstance(saved, dict) and (
        saved.get("is_active") is True or saved.get("subscription_url")
    ):
        return saved
    return None


def _resolve_subscription_url(session, subscription: dict) -> str:
    url = _find_url(subscription)
    if url:
        return url
    sub_id = subscription.get("id")
    if sub_id is None:
        return ""
    # API возвращает данные подключения отдельным endpoint'ом.
    data = call(session, VH, "GET", f"/subscriptions/{sub_id}/connect")
    return _find_url(data)


def activate_trial(account: dict, session) -> dict:
    print("[VLESSHub] Проверяю доступный пробный тариф…")
    plans_data = call(session, VH, "GET", "/subscriptions/plans")
    plans = _items(plans_data)
    trials = [
        plan for plan in plans
        if plan.get("is_trial") is True and plan.get("is_active", True) is not False
    ]
    if not trials:
        raise RuntimeError("VLESSHub не вернул доступный пробный тариф")
    trial = trials[0]
    plan_id = trial.get("id")
    tariff_type = trial.get("tariff_type")
    if plan_id is None or not tariff_type:
        raise RuntimeError("В пробном тарифе отсутствует id или tariff_type")

    # Сохраняем флаг ДО POST: если сеть оборвётся после обработки запроса,
    # повторный запуск не отправит вторую активацию вслепую.
    account["trial_activation_attempted"] = True
    save(account)
    print(f"[VLESSHub] Активирую тариф: {trial.get('name') or plan_id}")
    try:
        result = call(session, VH, "POST", "/subscriptions/", {
            "plan_id": plan_id,
            "device_count": 1,
            "tariff_type": tariff_type,
            "auto_renew": False,
        })
    except RuntimeError as exc:
        # Явный отказ панели (4xx) — повторять безопасно, снимаем флаг.
        # Сетевые обрывы оставляем «занятыми»: тариф мог активироваться.
        if str(exc).startswith("HTTP 4"):
            account["trial_activation_attempted"] = False
            save(account)
        raise
    account["subscription"] = result
    save(account)
    return result


def ensure_account() -> dict:
    """Вернуть аккаунт с активной подпиской, создав его только при необходимости."""
    with _LOCK:
        account = None
        if ACCOUNT.exists():
            try:
                loaded = json.loads(ACCOUNT.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("access_token"):
                    account = loaded
            except (OSError, ValueError):
                raise RuntimeError(f"Не удалось прочитать {ACCOUNT.name}; проверьте JSON.")
        if account is None:
            account = register_vless(create_mail())

        session = get_session(account)
        # Проверка токена и профиля заодно подтверждает, что сессия рабочая.
        try:
            profile = call(session, VH, "GET", "/users/me")
            account["user"] = profile
        except Exception as exc:
            raise RuntimeError(f"Не удалось проверить VLESSHub-аккаунт: {exc}") from exc

        subscription = _active_subscription(session, account)
        if subscription is None:
            if account.get("trial_activation_attempted"):
                raise RuntimeError(
                    "У сохранённого аккаунта нет активной подписки, а попытка "
                    "активации триала уже была. Повторная активация автоматически "
                    "отключена; проверьте аккаунт в VLESSHub."
                )
            subscription = activate_trial(account, session)

        url = _resolve_subscription_url(session, subscription)
        if not url:
            raise RuntimeError(
                "VLESSHub подтвердил подписку, но не вернул subscription URL "
                "ни в ответе, ни через /subscriptions/{id}/connect."
            )
        account["subscription"] = subscription
        account["subscription_url"] = url
        save(account)
        return account


def main() -> None:
    print("=" * 48)
    print("        VLESSHUB AUTO SETUP")
    print("=" * 48)
    try:
        account = ensure_account()
        print("\n[OK] VLESSHub готов.")
        print("[OK] Email:", account.get("email", "(не указан)"))
        print("[OK] Ссылка подписки:", account.get("subscription_url"))
        print("[OK] Данные сохранены в", ACCOUNT)
        print("\nНе публикуйте account.json: в нём находятся токены и пароль.")
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except Exception as exc:
        print("\n[ОШИБКА]", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
