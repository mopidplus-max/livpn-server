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
import time
from datetime import datetime, timezone
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


def call(session, base, method, path, payload=None, retries_429: int = 0):
    """Запрос к API. При 429 (лимит запросов) можно подождать и повторить."""
    attempt = 0
    while True:
        try:
            response = session.request(
                method, base.rstrip("/") + path, json=payload, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Ошибка соединения с {base}: {exc}") from exc

        if response.status_code == 429 and attempt < retries_429:
            attempt += 1
            wait = 20.0
            header = (response.headers.get("Retry-After") or "").strip()
            if header.isdigit():
                wait = min(120.0, max(5.0, float(header)))
            print(
                f"[VLESSHub] Лимит запросов ({base}); жду {int(wait)} с "
                f"и повторяю ({attempt}/{retries_429})…"
            )
            time.sleep(wait)
            continue

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"raw": response.text[:300]}
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status_code}: {_short(data)}")
        return data


def save(data: dict, path: Path | None = None) -> None:
    target = Path(path) if path else ACCOUNT
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    try:
        target.chmod(0o600)
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
    domains_data = call(session, MT, "GET", "/domains", retries_429=3)
    domains = domains_data.get("hydra:member", []) if isinstance(domains_data, dict) else domains_data
    available = [
        item["domain"] for item in domains
        if isinstance(item, dict) and item.get("domain") and item.get("isActive", True)
    ]
    if not available:
        raise RuntimeError("mail.tm не вернул доступных доменов")
    email = "vh" + secrets.token_hex(5) + "@" + secrets.choice(available)
    mail_password = password()
    # mail.tm ограничивает создание ящиков с одного IP — ждём и пробуем снова.
    call(
        session,
        MT,
        "POST",
        "/accounts",
        {"address": email, "password": mail_password},
        retries_429=6,
    )
    auth = call(session, MT, "POST", "/token", {"address": email, "password": mail_password})
    if not auth.get("token"):
        raise RuntimeError("mail.tm не вернул токен")
    print(f"[VLESSHub] Почта создана: {email}")
    return {"email": email, "password": mail_password, "token": auth["token"]}


def register_vless(mail: dict, path: Path | None = None) -> dict:
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
    }, retries_429=3)
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
    save(account, path)
    print("[VLESSHub] Аккаунт создан.")
    return account


def get_session(account: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Authorization"] = "Bearer " + account["access_token"]
    return session


def _expires_at(value) -> datetime | None:
    raw = ""
    if isinstance(value, dict):
        raw = str(value.get("expires_at") or value.get("expiresAt") or "")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def is_expired(account: dict) -> bool:
    """Подписка аккаунта уже истекла (панель может держать status=active)."""
    return _already_expired(account.get("subscription"))


def _already_expired(subscription) -> bool:
    moment = _expires_at(subscription)
    return moment is not None and moment <= datetime.now(timezone.utc)


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


def activate_trial(account: dict, session, path: Path | None = None) -> dict:
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
    save(account, path)
    print(f"[VLESSHub] Активирую тариф: {trial.get('name') or plan_id}")
    try:
        result = call(session, VH, "POST", "/subscriptions/", {
            "plan_id": plan_id,
            "device_count": 1,
            "tariff_type": tariff_type,
            "auto_renew": False,
        }, retries_429=2)
    except RuntimeError as exc:
        # Явный отказ панели (4xx) — повторять безопасно, снимаем флаг.
        # Сетевые обрывы оставляем «занятыми»: тариф мог активироваться.
        if str(exc).startswith("HTTP 4"):
            account["trial_activation_attempted"] = False
            save(account, path)
        raise
    account["subscription"] = result
    save(account, path)
    return result


class _DeadAccount(RuntimeError):
    """Аккаунт нельзя использовать; его нужно заменить новым."""


_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(target: Path) -> threading.RLock:
    with _LOCK:
        lock = _PATH_LOCKS.get(str(target))
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[str(target)] = lock
        return lock


def _archive(target: Path) -> str:
    """Убрать негодный файл аккаунта, сохранив его для истории."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archived = target.with_name(f"{target.stem}.expired-{stamp}{target.suffix}")
    try:
        os.replace(target, archived)
        return archived.name
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        return "(удалён)"


def _load(target: Path) -> dict | None:
    if not target.exists():
        return None
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError(f"Не удалось прочитать {target.name}; проверьте JSON.")
    if isinstance(loaded, dict) and loaded.get("access_token"):
        return loaded
    return None


def _prepare(account: dict, target: Path) -> dict:
    """Довести аккаунт до рабочего состояния со ссылкой на подписку."""
    session = get_session(account)
    # Проверка токена и профиля заодно подтверждает, что сессия рабочая.
    try:
        profile = call(session, VH, "GET", "/users/me")
        account["user"] = profile
    except Exception as exc:
        raise RuntimeError(f"Не удалось проверить VLESSHub-аккаунт: {exc}") from exc

    subscription = _active_subscription(session, account)
    if subscription is not None and _already_expired(subscription):
        raise _DeadAccount("Подписка аккаунта истекла.")
    if subscription is None:
        if account.get("trial_activation_attempted"):
            raise _DeadAccount(
                "У аккаунта нет активной подписки, а попытка активации "
                "триала уже была."
            )
        subscription = activate_trial(account, session, target)

    url = _resolve_subscription_url(session, subscription)
    if not url:
        raise RuntimeError(
            "VLESSHub подтвердил подписку, но не вернул subscription URL "
            "ни в ответе, ни через /subscriptions/{id}/connect."
        )
    account["subscription"] = subscription
    account["subscription_url"] = url
    save(account, target)
    return account


def ensure_account(path: Path | None = None) -> dict:
    """Вернуть аккаунт с активной подпиской, создав его только при необходимости.

    path — файл аккаунта конкретной подписки LiVPN. У каждого пользователя
    свой аккаунт VLESSHub, поэтому и трафик у всех разный.
    """
    target = Path(path) if path else ACCOUNT
    with _lock_for(target):
        for attempt in (1, 2):
            account = _load(target)
            if account is not None and is_expired(account):
                print(
                    f"[VLESSHub] Подписка аккаунта {target.name} истекла — "
                    "создаю новый аккаунт."
                )
                _archive(target)
                account = None
            if account is None:
                account = register_vless(create_mail(), target)
            try:
                return _prepare(account, target)
            except _DeadAccount as exc:
                removed = _archive(target)
                print(f"[VLESSHub] {exc} Старый файл: {removed}. Создаю новый.")
                if attempt == 2:
                    raise RuntimeError(str(exc)) from None
        raise RuntimeError("Не удалось подготовить аккаунт VLESSHub")


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
