"""Flask-приложение LiVPN: лендинг, кабинет подписки и раздатчик ссылок."""
from __future__ import annotations

import io
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import qrcode
import qrcode.image.svg
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

import ai
import config
import db
import service
import xray
import xrjson

log = logging.getLogger("livpn.web")

TOKEN_RE = re.compile(r"^[a-z0-9]{8,64}$")
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()
_hits_count = 0


# ── Ограничение частоты запросов ─────────────────────────────────────────────

def _client_ip() -> str:
    if config.TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _allow(ip: str) -> bool:
    global _hits_count
    now = time.time()
    with _hits_lock:
        bucket = [ts for ts in _hits.get(ip, []) if now - ts < 60]
        if len(bucket) >= config.RATE_LIMIT_PER_MINUTE:
            _hits[ip] = bucket
            return False
        bucket.append(now)
        _hits[ip] = bucket
        _hits_count += 1
        if _hits_count % 5000 == 0:
            cutoff = now - 120
            for key in list(_hits):
                fresh = [ts for ts in _hits[key] if ts > cutoff]
                if fresh:
                    _hits[key] = fresh
                else:
                    _hits.pop(key, None)
        return True


def _error_response(
    message: str, status: int = 503, retry_after: int | None = None
) -> Response:
    response = Response(message + "\n", status=status, mimetype="text/plain")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Allow-Origin"] = "*"
    if retry_after:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def _request_origin() -> str:
    scheme = request.scheme
    host = request.host
    if config.TRUST_PROXY:
        proto = request.headers.get("X-Forwarded-Proto", "")
        proto = proto.split(",")[0].strip().lower()
        forwarded_host = request.headers.get("X-Forwarded-Host", "")
        forwarded_host = forwarded_host.split(",")[0].strip()
        if proto in ("http", "https"):
            scheme = proto
        if forwarded_host:
            host = forwarded_host
    return f"{scheme}://{host}"


def _is_api() -> bool:
    return request.path.startswith("/api/")


def _wants_html_error() -> bool:
    if request.method not in ("GET", "HEAD"):
        return False
    if request.path.startswith(("/sub/", "/api/", "/qr/", "/healthz")):
        return False
    return True


# ── Данные для страниц ───────────────────────────────────────────────────────

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


def _next_refresh_seconds(sub) -> int:
    last = _parse_ts(sub["last_refresh"] if "last_refresh" in sub.keys() else "")
    if last is None:
        return 0
    nxt = last + timedelta(hours=config.UPDATE_INTERVAL_HOURS)
    return max(0, int((nxt - datetime.now(timezone.utc)).total_seconds()))


def _cooldown_left(sub) -> int:
    """Сколько секунд ещё нельзя обновлять подписку вручную."""
    minutes = max(0, config.USER_REFRESH_COOLDOWN_MINUTES)
    if minutes == 0:
        return 0
    last = _parse_ts(sub["last_refresh"] if "last_refresh" in sub.keys() else "")
    if last is None:
        return 0
    passed = (datetime.now(timezone.utc) - last).total_seconds()
    return max(0, int(minutes * 60 - passed))


def _traffic_view(sub) -> dict:
    upload = int(sub["upload"] or 0)
    download = int(sub["download"] or 0)
    total = int(sub["total"] or 0)
    used = upload + download
    limited = total > 0
    percent = min(100.0, used * 100.0 / total) if limited else 100.0
    return {
        "upload": upload,
        "download": download,
        "total": total,
        "used": used,
        "used_text": xray.human_bytes(used),
        "upload_text": xray.human_bytes(upload),
        "download_text": xray.human_bytes(download),
        "total_text": xray.human_bytes(total) if limited else "∞",
        "left_text": xray.human_bytes(max(0, total - used)) if limited else "∞",
        "percent": round(percent, 2),
        "limited": limited,
    }


def _node_view(row) -> dict:
    name = row["name"] or ""
    uri = row["uri"] or ""
    suffix = config.SUB_SUFFIX
    short = name[: -len(suffix)].rstrip() if suffix and name.endswith(suffix) else name
    flags = ai.extract_flags(name)
    is_auto = xrjson.is_auto_name(name)
    transport = "" if is_auto else ai.transport_of(name)
    protocol = uri.split("://", 1)[0].lower() if "://" in uri else ""
    endpoint = xray.uri_endpoint(uri)
    host, _, port = endpoint.rpartition(":")
    flag = flags[0] if flags else ("🇪🇺" if is_auto else "🌐")
    filter_key = "auto" if is_auto else (transport.lower() if transport else "other")
    search = " ".join(
        part
        for part in (name, short, endpoint, protocol, transport, "авто" if is_auto else "")
        if part
    ).lower()
    return {
        "idx": int(row["idx"]),
        "uri": uri,
        "name": name,
        "short": short or name,
        "flag": flag,
        "transport": transport,
        "protocol": protocol,
        "endpoint": endpoint,
        "host": host,
        "port": port,
        "is_auto": is_auto,
        "filter": filter_key,
        "search": search,
    }


def _country_count(nodes: list[dict]) -> int:
    return len(
        {
            node["flag"]
            for node in nodes
            if not node["is_auto"] and node["flag"] not in ("🌐", "")
        }
    )


def _load_sub(token: str, prepare: bool = True):
    """Подписка + валидные узлы. При первом заходе готовит подписку."""
    sub = db.get_sub_by_token(token)
    if not sub:
        return None, []
    sub_id = int(sub["id"])
    nodes = service.stored_nodes(sub_id)
    if not nodes and prepare:
        if service.recently_attempted(sub, 30) and sub["last_error"]:
            return sub, []
        try:
            refreshed = service.refresh_sub_claimed(sub_id)
            sub = refreshed or db.get_sub(sub_id) or sub
        except Exception:
            log.exception("Не удалось подготовить подписку #%s", sub_id)
            sub = db.get_sub(sub_id) or sub
        nodes = service.stored_nodes(sub_id)
    elif nodes and service.needs_refresh(sub):
        service.maybe_refresh_async(sub)
    return sub, nodes


def _dashboard_payload(sub, nodes: list[dict]) -> dict:
    token = sub["token"]
    traffic = _traffic_view(sub)
    countries = _country_count(nodes)
    return {
        "name": sub["name"] or config.SUB_NAME,
        "token": token,
        "sub_url": config.sub_url(token),
        "dashboard_url": config.dashboard_url(token),
        "nodes": nodes,
        "node_count": len(nodes),
        "country_count": countries,
        "traffic": traffic,
        "last_refresh_text": service.last_refresh_text(sub),
        "next_refresh_seconds": _next_refresh_seconds(sub),
        "cooldown_left": _cooldown_left(sub),
        "interval_hours": config.UPDATE_INTERVAL_HOURS,
        "updated_at": sub["last_refresh"] or "",
    }


def _distinct_countries() -> int:
    try:
        with db.session() as conn:
            names = [
                row["name"]
                for row in conn.execute("SELECT DISTINCT name FROM nodes").fetchall()
            ]
    except Exception:
        return 0
    flags: set[str] = set()
    for name in names:
        for flag in ai.extract_flags(name):
            if flag != "🇪🇺":
                flags.add(flag)
    return len(flags)


def _distinct_nodes() -> int:
    """Сколько уникальных серверов, а не строк на каждого пользователя."""
    try:
        with db.session() as conn:
            return int(
                conn.execute("SELECT COUNT(DISTINCT uri) c FROM nodes").fetchone()["c"]
            )
    except Exception:
        return 0


# ── QR-коды ──────────────────────────────────────────────────────────────────

def _qr_svg(data: str) -> str:
    image = qrcode.make(
        data,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    # Встраиваем в адаптивную рамку: размер задаёт CSS, а не миллиметры.
    svg = re.sub(
        r'width="[^"]+"\s+height="[^"]+"',
        'width="100%" height="100%"',
        svg,
        count=1,
    )
    svg = svg.replace(
        "<svg ",
        '<svg role="img" shape-rendering="crispEdges" focusable="false" ',
        1,
    )
    return svg


# ── Приложение ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    db.init_db()
    app = Flask(__name__)
    app.json.ensure_ascii = False

    @app.before_request
    def _prepare_request():
        g.csp_nonce = secrets.token_urlsafe(16)
        g.started_at = time.time()

    @app.after_request
    def _security_headers(response):
        response.headers["X-LiVPN-Instance"] = config.INSTANCE_ID
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        nonce = getattr(g, "csp_nonce", "")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif request.path.startswith(("/s/", "/sub/", "/api/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        elif request.path in ("/sw.js", "/manifest.webmanifest"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.context_processor
    def _template_globals():
        return {
            "cfg": config,
            "csp_nonce": getattr(g, "csp_nonce", ""),
            "bot_username": config.bot_username(),
            "bot_url": config.bot_url(),
            "channel_url": config.CHANNEL_URL,
            "support_url": config.SUPPORT_URL,
            "update_interval": config.UPDATE_INTERVAL_HOURS,
            "year": datetime.now(timezone.utc).year,
        }

    @app.before_request
    def _detect_domain():
        # Домен запоминается из реального запроса (Host / X-Forwarded-Host).
        # Это позволяет не задавать PUBLIC_BASE_URL вручную.
        if not config.AUTO_DOMAIN or config.PUBLIC_BASE_URL:
            return
        if request.method not in ("GET", "HEAD"):
            return
        if request.path != "/" and not request.path.startswith(("/sub/", "/s/")):
            return
        try:
            config.remember_base_url(_request_origin())
        except ValueError:
            pass

    # ── Страницы ─────────────────────────────────────────────────────────────

    @app.get("/")
    def index():
        stats = db.stats()
        stats["nodes"] = _distinct_nodes() or stats["nodes"]
        stats["countries"] = _distinct_countries()
        return render_template("landing.html", stats=stats, nav="home")

    @app.get("/s/<token>")
    def dashboard(token: str):
        if not TOKEN_RE.fullmatch(token):
            return _render_error(404, "Кабинет не найден", "Проверьте ссылку подписки.")
        if not _allow(_client_ip()):
            return _render_error(
                429,
                "Слишком много запросов",
                "Подождите минуту и обновите страницу.",
            )
        sub, rows = _load_sub(token)
        if not sub:
            return _render_error(
                404,
                "Кабинет не найден",
                "Ссылка устарела или подписка ещё не создана. Получите новую ссылку в боте.",
            )
        nodes = [_node_view(row) for row in rows]
        payload = _dashboard_payload(sub, nodes)
        return render_template(
            "dashboard.html",
            **payload,
            nav="dashboard",
        )

    @app.get("/sub/<token>")
    def subscription(token: str):
        if not TOKEN_RE.fullmatch(token):
            return _error_response("Not found", 404)
        if not _allow(_client_ip()):
            return _error_response("Too many requests", 429)

        sub = db.get_sub_by_token(token)
        if not sub:
            return _error_response("Not found", 404)

        sub_id = int(sub["id"])
        nodes = service.stored_nodes(sub_id)

        if not nodes:
            # Первый запрос или в базе лежал мусор: обновляем сразу,
            # иначе клиент получит пустую подписку и закеширует ошибку.
            if service.recently_attempted(sub, 30) and sub["last_error"]:
                return _error_response(
                    "Subscription is being prepared, retry later",
                    503,
                    retry_after=30,
                )
            try:
                refreshed = service.refresh_sub_claimed(sub_id)
                sub = refreshed or db.get_sub(sub_id) or sub
            except Exception:
                log.exception("Не удалось подготовить подписку #%s", sub_id)
                sub = db.get_sub(sub_id) or sub
            nodes = service.stored_nodes(sub_id)
            if not nodes:
                return _error_response(
                    "Subscription is being prepared, retry later",
                    503,
                    retry_after=30,
                )
        elif service.needs_refresh(sub):
            # Кэш есть — отдаём сразу, устаревшее обновляем в фоне.
            service.maybe_refresh_async(sub)

        body = xray.encode_subscription([row["uri"] for row in nodes])
        traffic = xray.Traffic(
            sub["upload"] or 0,
            sub["download"] or 0,
            sub["total"] or 0,
        )
        headers = xray.subscription_headers(
            traffic,
            title=sub["name"] or config.SUB_NAME,
            description=config.SUB_DESCRIPTION,
            interval_hours=config.UPDATE_INTERVAL_HOURS,
            web_url=config.CHANNEL_URL,
        )
        headers["Content-Disposition"] = 'attachment; filename="LiVPN"'
        headers["X-LiVPN-Nodes"] = str(len(nodes))

        response = Response(body, status=200)
        response.headers.update(headers)
        return response

    # ── API кабинета ─────────────────────────────────────────────────────────

    @app.get("/api/sub/<token>")
    def api_sub(token: str):
        if not TOKEN_RE.fullmatch(token):
            return jsonify(ok=False, error="not_found"), 404
        if not _allow(_client_ip()):
            return jsonify(ok=False, error="rate_limited"), 429
        sub, rows = _load_sub(token)
        if not sub:
            return jsonify(ok=False, error="not_found"), 404
        nodes = [_node_view(row) for row in rows]
        payload = _dashboard_payload(sub, nodes)
        payload["ok"] = True
        payload["bot"] = {
            "username": config.bot_username(),
            "url": config.bot_url(),
        }
        payload["channel_url"] = config.CHANNEL_URL
        return jsonify(payload)

    @app.post("/api/refresh/<token>")
    def api_refresh(token: str):
        if not TOKEN_RE.fullmatch(token):
            return jsonify(ok=False, error="not_found"), 404
        if not _allow(_client_ip()):
            return jsonify(ok=False, error="rate_limited"), 429
        sub = db.get_sub_by_token(token)
        if not sub:
            return jsonify(ok=False, error="not_found"), 404
        sub_id = int(sub["id"])
        nodes = service.stored_nodes(sub_id)
        left = _cooldown_left(sub)
        if nodes and left > 0 and not service.needs_refresh(sub):
            return (
                jsonify(
                    ok=False,
                    error="cooldown",
                    retry_after=left,
                    message="Подписка уже обновлена. Повторите позже.",
                ),
                429,
            )
        try:
            refreshed = service.refresh_sub_claimed(sub_id)
        except Exception as exc:
            log.warning("Ручное обновление #%s не удалось: %s", sub_id, exc)
            return (
                jsonify(
                    ok=False,
                    error="refresh_failed",
                    message="Не удалось обновить сейчас. Попробуйте позже.",
                ),
                502,
            )
        if refreshed is None:
            return jsonify(
                ok=True,
                pending=True,
                message="Обновление уже выполняется.",
            ), 202
        fresh_nodes = service.stored_nodes(sub_id)
        return jsonify(
            ok=True,
            nodes=len(fresh_nodes),
            refreshed_at=refreshed["last_refresh"],
        )

    # ── QR и служебные файлы ─────────────────────────────────────────────────

    @app.get("/qr/<token>.svg")
    def qr_sub(token: str):
        if not TOKEN_RE.fullmatch(token):
            return _error_response("Not found", 404)
        if not db.get_sub_by_token(token):
            return _error_response("Not found", 404)
        response = Response(_qr_svg(config.sub_url(token)), mimetype="image/svg+xml")
        response.headers["Cache-Control"] = "private, max-age=600"
        return response

    @app.get("/qr/node/<token>/<int:idx>.svg")
    def qr_node(token: str, idx: int):
        if not TOKEN_RE.fullmatch(token):
            return _error_response("Not found", 404)
        sub = db.get_sub_by_token(token)
        if not sub:
            return _error_response("Not found", 404)
        row = next(
            (item for item in service.stored_nodes(int(sub["id"])) if int(item["idx"]) == idx),
            None,
        )
        if row is None:
            return _error_response("Not found", 404)
        response = Response(_qr_svg(row["uri"]), mimetype="image/svg+xml")
        response.headers["Cache-Control"] = "private, max-age=600"
        return response

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(
            app.static_folder,
            "manifest.webmanifest",
            mimetype="application/manifest+json",
        )

    @app.get("/sw.js")
    def service_worker():
        path = os.path.join(app.static_folder, "sw.js")
        with open(path, "r", encoding="utf-8") as handle:
            body = handle.read()
        response = Response(body, mimetype="application/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/favicon.ico")
    def favicon():
        return redirect(url_for("static", filename="logo.svg"), code=302)

    @app.get("/healthz")
    def healthz():
        return jsonify(
            {
                "ok": True,
                "app": "livpn",
                "instance": config.INSTANCE_ID,
                "base_url": config.base_url(),
                "base_url_source": config.base_url_source(),
                **db.stats(),
            }
        )

    @app.get("/robots.txt")
    def robots():
        return Response(
            "User-agent: *\nDisallow: /sub/\nDisallow: /s/\nDisallow: /api/\n",
            mimetype="text/plain",
        )

    # ── Ошибки ───────────────────────────────────────────────────────────────

    def _render_error(status: int, title: str, message: str):
        if not _wants_html_error():
            return _error_response(title, status)
        response = make_response(
            render_template(
                "error.html",
                code=status,
                title=title,
                message=message,
            ),
            status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return _render_error(
            404,
            "Страница не найдена",
            "Возможно, ссылка устарела или в адресе опечатка.",
        )

    @app.errorhandler(429)
    def too_many(_error):
        return _render_error(
            429,
            "Слишком много запросов",
            "Подождите минуту и попробуйте снова.",
        )

    @app.errorhandler(500)
    def server_error(error):
        log.exception("Веб-сервер: %s", error)
        return _render_error(
            500,
            "Внутренняя ошибка",
            "Мы уже знаем о проблеме. Попробуйте обновить страницу через минуту.",
        )

    return app


app = create_app()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service.start_scheduler()
    log.info(
        "Веб-сервер на http://%s:%s, публичный адрес: %s",
        config.HOST,
        config.PORT,
        config.base_url(),
    )
    app.run(
        host=config.HOST,
        port=config.PORT,
        threaded=True,
        use_reloader=False,
        debug=False,
    )


if __name__ == "__main__":
    main()
