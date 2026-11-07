# LiVPN

Раздатчик подписок Xray (VLESS / Trojan) с Telegram-ботом.
Пользователь получает одну постоянную ссылку `/sub/<токен>`, бот сам
создаёт подписку, обновляет узлы и показывает трафик.

Работает с Happ, Incy, v2rayNG, Hiddify, Streisand и другими клиентами.

## Быстрый старт

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # заполните BOT_TOKEN, ADMIN_IDS, PROVIDER
python register.py        # только для PROVIDER=vlesshub
python run.py
```

`run.py` поднимает Flask, бота и планировщик сразу.
Отдельные режимы: `python run.py --web-only`, `python run.py --bot-only`.

## Веб-интерфейс

- `GET /` — лендинг с живой статистикой, возможностями, инструкцией и FAQ.
- `GET /s/<token>` — личный кабинет: трафик, серверы с поиском и
  фильтрами (TCP / gRPC / XHTTP / Авто), QR-коды, кнопки Happ и Hiddify,
  время следующего автообновления.
- `GET /sub/<token>` — машинная подписка (base64 + заголовки), её читают
  клиенты. Она не изменилась.
- `GET /qr/<token>.svg` — QR-код подписки; `/qr/node/<token>/<idx>.svg` —
  QR-код отдельного сервера. Генерируются на сервере, без внешних API.
- `GET /api/sub/<token>` — JSON для кабинета; `POST /api/refresh/<token>` —
  ручное обновление с кулдауном.
- `/manifest.webmanifest` и `/sw.js` — установка на главный экран (PWA)
  и офлайн-оболочка лендинга.

Кабинет закрыт от индексации (`noindex`, `robots.txt`), ссылка нигде не
публикуется. Тема — тёмная/светлая с запоминанием, интерфейс адаптирован
под телефон. В боте появилась кнопка «👤 Кабинет» (WebApp) и команда
`/cabinet`; имя бота для ссылок определяется через `getMe` и сохраняется
в `bot_username.txt` (или задаётся `BOT_USERNAME`).

## Источники узлов (`PROVIDER`)

| Значение | Что делает |
|---|---|
| `static` | узлы из `nodes.json` (свои серверы) |
| `xui` | своя панель 3x-ui: на каждого пользователя создаётся клиент |
| `upstream` | репроксирование вашей официальной подписки (`UPSTREAM_URL`) |
| `remnawave` | подписка Remnawave (`UPSTREAM_URL`) |
| `vlesshub` | аккаунт VLESSHub из `account.json` |

Поддерживаются все распространённые форматы ответа источника:

- обычный текст со ссылками `vless://`, `trojan://`, `vmess://` …;
- base64 c теми же ссылками;
- JSON-конфиг Xray (`{"outbounds": [...]}`) и массив таких конфигов
  (так, например, отвечает VLESSHub) — из них собираются VLESS/Trojan-ссылки.

Комментарии, ссылки на сайты и HTML в подписку больше не попадают.

Панель часто отдаёт отдельный «Авто»-конфиг, внутри которого перечислены
все серверы сразу. LiVPN не размножает его в десятки одинаковых
«Автоподборов»: дубли по `host:port` схлопываются, остаются нормальные
названия стран, а уникальный авто-сервер остаётся один. Транспорт
(`TCP`, `gRPC`, `XHTTP`) сохраняется в названии: «Испания gRPC 🇪🇸 | LiVPN».
Служебные подписи вроде «0 дней осталось» вырезаются.

## Публичный адрес

Приоритет:

1. `PUBLIC_BASE_URL` — самый надёжный вариант;
2. домен, запомненный из реального запроса (`base_url.txt`);
3. внешний IP через `api.ipify.org`;
4. `127.0.0.1`.

Автоопределение домена из заголовка `Host` проверяется двумя способами
(`AUTO_DOMAIN_VERIFY=1`): домен должен либо резолвиться в публичный IP
сервера, либо отвечать на `/healthz` маркером именно этого процесса
(`instance` / заголовок `X-LiVPN-Instance`). Второй способ нужен за
CDN/туннелем (Cloudflare и т.п.), где DNS указывает на чужие IP.
Если проверить не удалось из-за сети, домен всё равно запоминается,
чтобы не отдавать клиентам `127.0.0.1`; в лог пишется предупреждение.
Атака с поддельным `Host:` на чужой домен отклоняется. Админ может
задать домен вручную командой `/setdomain`.

Если используете reverse-proxy, пропускайте наружу `/healthz` и `/`
(либо просто задайте `PUBLIC_BASE_URL` — тогда проверки не нужны).

## Команды бота

- `/start` — меню / карточка подписки
- `/sub` — ссылка и статистика
- `/cabinet` — кабинет с трафиком, серверами и QR
- `/refresh` — обновить (кулдаун `USER_REFRESH_COOLDOWN_MINUTES`)
- `/status` — трафик и число серверов
- `/help` — инструкция для клиентов
- `/myid` — ваш Telegram ID
- для админов: `/stats`, `/domain`, `/setdomain`, `/clearcache`

## HTTP

- `GET /` — лендинг со статистикой
- `GET /s/<token>` — кабинет подписки (HTML)
- `GET /sub/<token>` — подписка (base64 + заголовки `Subscription-Userinfo`)
- `GET /api/sub/<token>` — JSON кабинета
- `POST /api/refresh/<token>` — обновление с кулдауном
- `GET /qr/<token>.svg`, `GET /qr/node/<token>/<idx>.svg` — QR-коды
- `GET /manifest.webmanifest`, `GET /sw.js` — PWA
- `GET /healthz` — состояние и текущий base URL
- `GET /robots.txt`

## Docker и деплой

```bash
docker build -t livpn .
docker run -d --name livpn -p 8080:8080 --env-file .env \
  -e DB_PATH=/data/livpn.db \
  -e VLESSHUB_ACCOUNT_FILE=/data/account.json \
  -e BASE_URL_FILE=/data/base_url.txt \
  -e BOT_USERNAME_FILE=/data/bot_username.txt \
  -e PUBLIC_BASE_URL=https://app.example.com \
  -e TRUST_PROXY=1 -e AUTO_DOMAIN=0 \
  -v livpn-data:/data livpn
```

- Образ собирается GitHub Actions и публикуется в GHCR
  (`.github/workflows/docker-publish.yml`, тег `latest`).
- Секреты и база не попадают в образ: см. `.dockerignore`.
- Для `PROVIDER=vlesshub` положите `account.json` в том `/data`
  (или выполните `docker exec -it livpn python register.py`).
- Контейнер должен работать постоянно: в нём и Flask, и Telegram-бот,
  и планировщик. На free-плане blitz.cloud включите `keepAwake`.
- Минимальные переменные для хостинга: `BOT_TOKEN`, `PUBLIC_BASE_URL`,
  `PROVIDER`, `DB_PATH`, `VLESSHUB_ACCOUNT_FILE`, `TRUST_PROXY=1`,
  `AUTO_DOMAIN=0`.

## Безопасность

- `.env`, `account.json`, `livpn.db`, `base_url.txt` не коммитятся и не
  должны попадать в архивы/логи.
- В Telegram пользователю никогда не отправляются токены и пароли —
  ошибки провайдеров очищаются перед показом.
- Рекомендуется держать секреты вне общей памяти телефона/хоста
  (в каталоге приложения, а не в `/storage/emulated/0`).
- `PROVIDER=vlesshub` использует один общий аккаунт на всех: учитывайте
  лимиты устройств и правила сервиса.

## Файлы

```
run.py          запуск всего
app.py          Flask-раздатчик
bot.py          Telegram-бот
service.py      создание/обновление подписок, планировщик
providers/      static, xui, upstream, remnawave, vlesshub
xray.py         share-ссылки, подписка, трафик
xrjson.py       разбор JSON-конфигов Xray
ai.py           переименование узлов (Groq) + локальный fallback
config.py       .env и публичный адрес
db.py           SQLite
register.py     bootstrap аккаунта VLESSHub
templates/      Jinja-шаблоны лендинга, кабинета и ошибок
static/         CSS, JS, логотип, PWA-манифест и service worker
Dockerfile      образ для хостинга
.github/        сборка образа в GHCR
```
