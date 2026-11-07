"""Ядро LiVPN: создание подписки, обновление узлов, планировщик."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import config
import db
import xray
from ai import rename_nodes
from providers import get_provider
from providers.base import ProviderError, dedupe_nodes

log = logging.getLogger("livpn.service")

_refreshing: set[int] = set()
_refresh_guard = threading.Lock()

_scheduler_started = False
_scheduler_stop: threading.Event | None = None
_scheduler_guard = threading.Lock()


# ── Время ────────────────────────────────────────────────────────────────────

def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def needs_refresh(sub, hours: float | None = None) -> bool:
    if hours is None:
        hours = config.UPDATE_INTERVAL_HOURS
    last = _parse_ts(sub["last_refresh"] if "last_refresh" in sub.keys() else "")
    if last is None:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=hours)


def last_refresh_text(sub) -> str:
    last = _parse_ts(sub["last_refresh"] if "last_refresh" in sub.keys() else "")
    if last is None:
        return "ещё не обновлялась"
    delta = datetime.now(timezone.utc) - last
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def _recent_attempt(sub, seconds: int = 300) -> bool:
    attempt = _parse_ts(sub["last_attempt"] if "last_attempt" in sub.keys() else "")
    if attempt is None:
        return False
    return (datetime.now(timezone.utc) - attempt).total_seconds() < seconds


def recently_attempted(sub, seconds: int = 30) -> bool:
    return _recent_attempt(sub, seconds)


# ── Подписка ─────────────────────────────────────────────────────────────────

def ensure_sub(tg_id: int, username: str | None = None, first_name: str | None = None):
    db.touch_user(tg_id, username, first_name)
    row = db.get_sub_by_user(tg_id)
    if row:
        # При смене PROVIDER существующие пользователи тоже переходят
        # на выбранный источник, без пересоздания своей LiVPN-ссылки.
        if row["provider"] != config.PROVIDER:
            db.set_provider(int(row["id"]), config.PROVIDER)
            row = db.get_sub(int(row["id"])) or row
        return row
    return db.create_sub(
        tg_id,
        name=config.SUB_NAME,
        provider=config.PROVIDER,
        token_len=config.TOKEN_LEN,
    )


def link_for(sub) -> str:
    return config.sub_url(sub["token"])


def stored_nodes(sub_id: int) -> list:
    """Узлы из базы, отбрасывая мусор (например, JSON вместо ссылки)."""
    return [row for row in db.get_nodes(sub_id) if xray.is_share_uri(row["uri"])]


def traffic_text(sub) -> str:
    used = int(sub["upload"] or 0) + int(sub["download"] or 0)
    total = int(sub["total"] or 0)
    if total > 0:
        return f"{xray.human_bytes(used)} / {xray.human_bytes(total)}"
    return f"{xray.human_bytes(used)} / ∞"


# ── Обновление ───────────────────────────────────────────────────────────────

def _disambiguate_names(names: list[str]) -> list[str]:
    """Разные узлы не должны называться одинаково.

    Суффикс « | LiVPN» остаётся в конце, номер вставляется перед ним.
    """
    counts: dict[str, int] = {}
    result: list[str] = []
    suffix = config.SUB_SUFFIX
    for name in names:
        counts[name] = counts.get(name, 0) + 1
        index = counts[name]
        if index == 1:
            result.append(name)
            continue
        head = name[: -len(suffix)].rstrip() if suffix and name.endswith(suffix) else name
        result.append(f"{head} {index}{suffix}")
    return result


def refresh_sub(sub_id: int):
    """Синхронно обновить подписку. Бросает исключение при ошибке."""
    sub = db.get_sub(sub_id)
    if not sub:
        raise ProviderError("Подписка не найдена")

    try:
        provider = get_provider(sub["provider"])
        nodes, traffic = provider.fetch(sub)
        nodes = [
            node
            for node in dedupe_nodes(list(nodes), limit=config.MAX_NODES)
            if xray.is_share_uri(node.uri)
        ]
        if not nodes:
            raise ProviderError("Источник не вернул ни одного рабочего узла")

        raw_names = [
            (node.name or f"Сервер {index + 1}").strip()
            for index, node in enumerate(nodes)
        ]
        names = rename_nodes(raw_names)
        names = _disambiguate_names(names)

        pairs: list[tuple[str, str]] = []
        for node, name in zip(nodes, names):
            pairs.append((xray.rename_uri(node.uri, name), name))

        db.replace_nodes(sub_id, pairs)
        db.set_traffic(sub_id, traffic.upload, traffic.download, traffic.total)
        db.mark_refresh_ok(sub_id)
        log.info(
            "Подписка #%s обновлена: %s узлов, трафик %s / %s",
            sub_id,
            len(pairs),
            xray.human_bytes(traffic.used),
            xray.human_bytes(traffic.total) if traffic.total else "∞",
        )
        return db.get_sub(sub_id)
    except Exception as exc:
        db.mark_refresh_error(sub_id, str(exc))
        log.warning("Подписка #%s: ошибка обновления: %s", sub_id, exc)
        raise


def refresh_sub_safe(sub_id: int) -> tuple[bool, str]:
    try:
        refresh_sub(sub_id)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def refresh_sub_claimed(sub_id: int):
    """Обновить подписку, если её ещё не обновляет другой поток.

    Возвращает обновлённую строку sub или None, если обновление уже идёт.
    """
    if not _claim(sub_id):
        return None
    try:
        return refresh_sub(sub_id)
    finally:
        _release(sub_id)


def _claim(sub_id: int) -> bool:
    with _refresh_guard:
        if sub_id in _refreshing:
            return False
        _refreshing.add(sub_id)
        return True


def _release(sub_id: int) -> None:
    with _refresh_guard:
        _refreshing.discard(sub_id)


def maybe_refresh_async(sub, force: bool = False) -> bool:
    """Запустить фоновое обновление, если подписка устарела.

    force=True — обновить даже если last_refresh свежий (например, когда
    в базе нет валидных узлов).
    """
    sub_id = int(sub["id"])
    if not force:
        if not needs_refresh(sub):
            return False
        if _recent_attempt(sub, 300) and int(sub["refresh_failures"] or 0) > 0:
            return False
    if not _claim(sub_id):
        return False

    def worker() -> None:
        try:
            refresh_sub_safe(sub_id)
        finally:
            _release(sub_id)

    threading.Thread(
        target=worker,
        name=f"livpn-refresh-{sub_id}",
        daemon=True,
    ).start()
    return True


def refresh_due(limit: int | None = None, workers: int | None = None) -> dict[str, int]:
    rows = db.list_subs()
    due = [row for row in rows if needs_refresh(row)]
    if limit is not None:
        due = due[:limit]

    stats = {"total": 0, "ok": 0, "failed": 0, "skipped": 0}
    if not due:
        return stats

    max_workers = max(1, workers or config.REFRESH_WORKERS)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for row in due:
            sub_id = int(row["id"])
            if not _claim(sub_id):
                stats["skipped"] += 1
                continue
            stats["total"] += 1
            future = executor.submit(refresh_sub_safe, sub_id)
            futures[future] = sub_id

        for future in as_completed(futures):
            sub_id = futures[future]
            _release(sub_id)
            try:
                ok, error = future.result()
            except Exception as exc:  # на всякий случай
                ok, error = False, str(exc)
            if ok:
                stats["ok"] += 1
            else:
                stats["failed"] += 1
                log.warning("Фоновое обновление #%s не удалось: %s", sub_id, error)

    log.info(
        "Плановое обновление: %s, успешно %s, ошибок %s, пропущено %s",
        stats["total"],
        stats["ok"],
        stats["failed"],
        stats["skipped"],
    )
    return stats


# ── Планировщик ──────────────────────────────────────────────────────────────

def _scheduler_loop(stop: threading.Event) -> None:
    # Даём веб-серверу и боту спокойно стартовать.
    if stop.wait(8):
        return
    while not stop.is_set():
        try:
            refresh_due()
        except Exception:
            log.exception("Планировщик: непредвиденная ошибка")
        stop.wait(max(60, config.REFRESH_CHECK_SECONDS))


def start_scheduler() -> threading.Event:
    """Идемпотентно запустить фоновый планировщик."""
    global _scheduler_started, _scheduler_stop
    with _scheduler_guard:
        if _scheduler_started and _scheduler_stop is not None:
            return _scheduler_stop
        stop = threading.Event()
        thread = threading.Thread(
            target=_scheduler_loop,
            args=(stop,),
            name="livpn-scheduler",
            daemon=True,
        )
        thread.start()
        _scheduler_started = True
        _scheduler_stop = stop
        log.info("Планировщик запущен (проверка каждые %s сек)", config.REFRESH_CHECK_SECONDS)
        return stop


def stop_scheduler() -> None:
    global _scheduler_started
    with _scheduler_guard:
        if _scheduler_stop is not None:
            _scheduler_stop.set()
        _scheduler_started = False
