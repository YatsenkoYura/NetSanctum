# Mail OAuth: Gmail и Mail.ru (черновик спецификации)

## 1. Цель

Дать пользователю возможность подключить **свой существующий почтовый аккаунт** в модуле `mail` без хранения постоянного пароля (где это возможно), через OAuth 2.0:

- **Gmail / Google Workspace** — приоритет №1.
- **Mail.ru** — приоритет №2.

Текущий MVP модуля (`app/modules/mail/*`) уже поддерживает:
- per-user аккаунты (`mail_accounts`) с IMAP/SMTP хостами, логинами и паролями;
- ручную синхронизацию входящих через IMAP (`POST /api/mail/accounts/{id}/sync`);
- отправку через SMTP аккаунта.

OAuth должен встроиться в эту модель, а не заменить её полностью: пароль/app-password остаётся fallback-механизмом.

---

## 2. Область и принципы

### 2.1. Что делаем в первой итерации
- Подключение внешнего аккаунта по OAuth (authorization code flow).
- Получение и безопасное хранение `access_token` + `refresh_token` (если выдается).
- Использование токенов для:
  - **IMAP** (чтение/синхронизация входящих) через SASL XOAUTH2.
  - **SMTP** (отправка) через SASL XOAUTH2.
- Обновление access token по refresh token прозрачно при синке/отправке.
- UI/API для подключения, переподключения (reauth), отключения OAuth.

### 2.2. Чего не делаем сразу
- Полноценный Gmail API / Mail.ru REST API клиент вместо IMAP/SMTP (можно рассмотреть позже, но MVP проще на XOAUTH2 поверх существующего IMAP/SMTP контура).
- Push/sync в реальном времени (webhooks/push), фоновый постоянный IDLE для всех аккаунтов.
- OAuth для всех провайдеров сразу.
- Shared mailbox / delegated access.
- Полная миграция существующих password-аккаунтов.

### 2.3. Принципы реализации (под NetSanctum)
- Модульная изоляция: всё в `app/modules/mail/*` (модели, сервисы, роуты, шаблоны, миграции), без прямых зависимостей из других модулей.
- Секреты провайдера (client id/secret) — только через централизованный конфиг приложения (`app/core/config.py`, env), не в модуле отдельно.
- Токены пользователей — чувствительные данные: не логировать, не отдавать в API, хранить в БД в зашифрованном виде (см. раздел 6).
- Явное состояние подключения: `connected / needs_reauth / error`.
- Совместимость с текущим `mail_accounts` и fallback на пароль.

---

## 3. Модель данных (предложение)

Текущая таблица: `mail_accounts` (IMAP/SMTP host/port/user/password/ssl/tls, `email_address`, `is_active`, `last_synced_at`).

### 3.1. Расширение `mail_accounts` (миграция `mail_0003_oauth`)

Добавить поля (nullable/default-safe):

| Поле | Тип | Назначение |
|---|---|---|
| `auth_type` | `varchar(32)` not null default `'password'` | `password` \| `oauth2` |
| `provider` | `varchar(32)` null | `gmail` \| `mailru` \| `custom` (для password может быть null) |
| `oauth_access_token` | `text` null | access token (зашифрован) |
| `oauth_refresh_token` | `text` null | refresh token (зашифрован, может отсутствовать у провайдера/конфигурации) |
| `oauth_token_type` | `varchar(32)` null | обычно `Bearer` |
| `oauth_scope` | `text` null | фактически выданные scope |
| `oauth_expires_at` | `timestamptz` null | время истечения access token |
| `oauth_status` | `varchar(32)` not null default `'none'` | `none` \| `connected` \| `needs_reauth` \| `error` |
| `oauth_error` | `text` null | последняя ошибка (санитизированная, без токенов) |
| `oauth_account_sub` | `varchar(255)` null | стабильный идентификатор аккаунта у провайдера (если доступен) |

Примечания:
- Для `auth_type='oauth2'` поля `imap_password`/`smtp_password` могут быть пустыми/неиспользуемыми (нужна аккуратная адаптация текущих non-null ограничений — возможно сделать nullable в этой же миграции с backfill).
- `MigrationSpec.tables` в `module.py` не меняется (таблица та же), но схема и модели должны соответствовать.
- В `historical_tables` ничего не переносим.

### 3.2. Временное состояние OAuth flow (state/PKCE/verifier)

Нужен одноразовый state для защиты от CSRF и привязки callback к пользователю/аккаунту.

Варианты:
1. **Redis/ephemeral state store** (предпочтительно): ключ `mail:oauth:<state>` со TTL (10-15 мин), значение — `user_id`, `provider`, `account_id|null`, `code_verifier`, `redirect_uri`, `created_at`.
2. **Подписанный state** (JWT/HMAC) + server-side stash для PKCE verifier — сложнее, но stateless частично.
3. **Таблица `mail_oauth_states`** — просто, но требует миграции/очистки; допустимо как fallback.

Рекомендация для NetSanctum: **ephemeral state в Redis** (аналогично другим transient-состояниям), без новой таблицы, с коротким TTL.

---

## 4. Конфигурация приложения (централизованно)

Добавить в `app/core/config.py` (env):

### 4.1. Общие
- `MAIL_OAUTH_ENABLED: bool = False` — feature flag.
- `MAIL_OAUTH_CALLBACK_BASE_URL: str = ""` — опционально; если пусто, строить от `PUBLIC_BASE_URL`.
- `MAIL_OAUTH_STATE_TTL_SECONDS: int = 900`

### 4.2. Gmail
- `MAIL_GMAIL_OAUTH_CLIENT_ID: str = ""`
- `MAIL_GMAIL_OAUTH_CLIENT_SECRET: str = ""`
- `MAIL_GMAIL_OAUTH_REDIRECT_PATH: str = "/api/mail/oauth/gmail/callback"` (или фиксированный путь)
- `MAIL_GMAIL_OAUTH_SCOPES: str = "https://mail.google.com/"` (по умолчанию; для IMAP/SMTP XOAUTH2 обычно требуется широкий mail scope — уточнить при реализации)

### 4.3. Mail.ru
- `MAIL_MAILRU_OAUTH_CLIENT_ID: str = ""`
- `MAIL_MAILRU_OAUTH_CLIENT_SECRET: str = ""`
- `MAIL_MAILRU_OAUTH_REDIRECT_PATH: str = "/api/mail/oauth/mailru/callback"`
- `MAIL_MAILRU_OAUTH_SCOPES: str = ""` (уточнить поддерживаемые scope для IMAP/SMTP; см. раздел 8.2)

Требование: в production при включённом `MAIL_OAUTH_ENABLED` валидировать наличие обязательных client credentials для включённых провайдеров (в `validate_runtime_security` или отдельной проверке модуля/конфига — в соответствии с паттернами репозитория).

---

## 5. API и UX-поток

### 5.1. Пользовательский сценарий (подключение)
1. Пользователь открывает `/mail` → блок «Подключенные аккаунты».
2. Нажимает **«Подключить Gmail»** или **«Подключить Mail.ru»**.
3. Backend создаёт/резервирует `mail_accounts` запись в состоянии "pending/oauth" (или принимает `account_id` для переподключения существующего аккаунта) и возвращает `authorization_url`.
4. Браузер редиректится на провайдера (consent).
5. Провайдер возвращает пользователя на callback (`/api/mail/oauth/{provider}/callback?code=...&state=...`).
6. Backend валидирует `state`, обменивает `code` на токены, определяет email аккаунта (profile/id token/userinfo — в зависимости от провайдера), сохраняет токены, выставляет `oauth_status=connected`.
7. Редирект обратно в `/mail?oauth=success&provider=...` (или страница с результатом).

### 5.2. Предлагаемые endpoint'ы

- `POST /api/mail/oauth/{provider}/start`
  - auth required
  - body/query: `account_id?` (reauth), опционально имя
  - result: `{ authorization_url, account_id }`
- `GET /api/mail/oauth/{provider}/callback`
  - **browser redirect endpoint** (не JSON-only), `include_in_schema=False` допустимо
  - валидирует `state`, обменивает code, сохраняет токены, редиректит в UI
- `POST /api/mail/accounts/{id}/oauth/disconnect`
  - очищает токены / переводит в `needs_reauth` или `auth_type=password` (по политике)
- `POST /api/mail/accounts/{id}/oauth/refresh` (опционально, для диагностики; обычно не нужно)
- `GET /api/mail/oauth/providers`
  - возвращает, какие провайдеры включены и сконфигурированы (для UI)

Существующие endpoint'ы аккаунтов (`/api/mail/accounts*`) расширяются, но **никогда не возвращают** `oauth_access_token`, `oauth_refresh_token`, пароли.

### 5.3. UI изменения (`mail_dashboard.html`)
- Кнопки: `Подключить Gmail`, `Подключить Mail.ru` (показывать только если провайдер включён/сконфигурирован).
- В карточке аккаунта показывать:
  - тип подключения (`Пароль` / `OAuth: Gmail` / `OAuth: Mail.ru`)
  - статус (`Подключено`, `Требуется повторный вход`, `Ошибка`)
  - действия: `Синхронизировать`, `Переподключить`, `Отключить OAuth`, `Удалить`.
- Обработка query-параметров после callback (`oauth=success|error`, `reason`) для toast/баннера.

---

## 6. Хранение секретов и безопасность

### 6.1. Токены пользователей
- Access/refresh token хранить **только в зашифрованном виде** (использовать существующие криптопримитивы/подходы репозитория, если применимо; не изобретать ad-hoc схему без необходимости).
- В API responses исключить поля токенов и паролей.
- В логах маскировать заголовки/тела OAuth ответов, не писать `code`, `access_token`, `refresh_token`.
- `oauth_error` хранить санитизированно (код/краткое описание без секретов).

### 6.2. OAuth flow защита
- `state`: криптостойкий random, одноразовый, TTL, привязка к `user_id` и `provider`.
- **PKCE** (`S256`) использовать везде, где поддерживается (обязательно предусмотреть в реализации; для Google рекомендуется, для Mail.ru — проверить поддержку и включить при наличии).
- Строгая проверка `redirect_uri` (точное совпадение с зарегистрированным).
- Callback должен требовать валидный `state`; при ошибке — не создавать/не менять аккаунт опасным образом.

### 6.3. Минимизация привилегий
- Запрашивать минимально необходимые scopes для выбранного режима (IMAP/SMTP sync/send).
- Не запрашивать profile scopes сверх необходимого; email аккаунта получать только если нужно для привязки.

### 6.4. Хранение client secret
- Только env/config (`MAIL_GMAIL_OAUTH_CLIENT_SECRET`, `MAIL_MAILRU_OAUTH_CLIENT_SECRET`).
- Не коммитить, не показывать в UI, не отдавать в browser.

---

## 7. Интеграция с текущим IMAP/SMTP кодом

Текущая реализация: `services.sync_account_inbox()` использует `imaplib` + логин/пароль; `services.send_email()`/`_send_via_smtp()` используют `smtplib` + логин/пароль.

### 7.1. Абстракция аутентификации транспорта

Ввести внутренние helper'ы/стратегии:
- `build_imap_client(account) -> imaplib.IMAP4(_SSL)`
- `imap_authenticate(client, account)`
  - `password`: `login(username, password)`
  - `oauth2`: `authenticate('XOAUTH2', xoauth2_initial_client_response(user, access_token))`
- `smtp_authenticate(server, account)` аналогично.

`_imap_fetch_recent` и `_send_via_smtp` не должны сами решать, откуда брать секрет; они получают готовый аутентифицированный канал/параметры через единый слой.

### 7.2. XOAUTH2 формат (важно для реализации)

Для IMAP/SMTP XOAUTH2 обычно используется строка вида:
`user=<email>\x01auth=Bearer <access_token>\x01\x01`
(передаётся в механизм SASL XOAUTH2; детали кодирования зависят от API `imaplib`/`smtplib` и требуют проверки на тестах/интеграции).

### 7.3. Refresh и обработка ошибок
- Перед IMAP/SMTP операцией: если `auth_type=oauth2` и `oauth_expires_at` скоро истекает (например, <= 60с) — refresh.
- При ошибке auth от сервера (`401/invalid_grant` при refresh, IMAP/SMTP auth failure):
  - пометить `oauth_status='needs_reauth'`, сохранить санитизированную ошибку;
  - вернуть понятную ошибку в API (`409/502` по ситуации), чтобы UI показал «Переподключить аккаунт».

### 7.4. Определение email аккаунта при callback
- Если провайдер возвращает `id_token`/userinfo с email — использовать как `email_address` (и сверять, что доступ к почтовому ящику соответствует).
- Для переподключения (`account_id`) — валидировать совпадение email/идентификатора, чтобы не привязать чужой ящик к существующей записи.

---

## 8. Специфика провайдеров

> Ниже — проектные ориентиры. Точные endpoint'ы, названия scope и требования к продукту/verification нужно подтвердить по актуальной документации провайдеров перед реализацией и вынести в финальную версию спеки.

### 8.1. Gmail / Google

**Рекомендуемый подход (MVP):** OAuth 2.0 + XOAUTH2 для IMAP/SMTP (совместимо с текущей архитектурой синка/отправки).

Что нужно предусмотреть:
- Authorization Code flow (web server app), `offline_access`-эквивалент через параметры запроса refresh token (обычно `access_type=offline`, `prompt=consent` при первом подключении — проверить актуальные требования Google).
- Scopes для почты/IMAP-SMTP: часто используется `https://mail.google.com/`; возможно сужение — требует проверки совместимости с нужными IMAP/SMTP операциями.
- IMAP: `imap.gmail.com:993` (SSL), SMTP: `smtp.gmail.com:465` (SSL) или `587` (STARTTLS) — дефолты можно проставлять при выборе провайдера.
- Особенности Google OAuth:
  - refresh token может выдаваться не каждый раз; при первом подключении нужно гарантировать получение (повторный consent/параметры).
  - требуется настройка OAuth consent screen и тестовых пользователей на этапе разработки.
  - политика Google может требовать verification для чувствительных scope.

**Открытые вопросы (уточнить перед реализацией):**
- Финальный набор scopes (минимально достаточный) для нашего IMAP/SMTP сценария.
- Нужен ли отдельный scope `openid email profile` для получения подтверждённого email.
- Поведение для Google Workspace аккаунтов с ограничениями администратора.

### 8.2. Mail.ru

**Цель:** аналогичный UX (кнопка подключения, callback, токены), использование XOAUTH2 в IMAP/SMTP если поддерживается провайдером в нужном режиме.

Что нужно предусмотреть в спека/реализации:
- Зарегистрировать приложение в экосистеме Mail.ru/VK ID (в зависимости от текущего OAuth-провайдера Mail.ru — требует актуальной проверки).
- Определить актуальные:
  - authorization endpoint, token endpoint, userinfo/email endpoint (если нужен),
  - поддерживаемые grant/response types,
  - PKCE support,
  - scopes/права для доступа к почте (IMAP/SMTP),
  - срок жизни access/refresh token и формат ошибок.
- Дефолты серверов (проверить актуальные): IMAP/SMTP хосты и порты Mail.ru; заполнять автоматически в UI при выборе провайдера.

**Открытые вопросы (обязательно закрыть до кодирования):**
- Поддерживает ли Mail.ru OAuth 2.0 для **IMAP/SMTP XOAUTH2** в пользовательском сценарии NetSanctum (а не только REST API, если таковой есть).
- Нужны ли специальные настройки аккаунта (включение IMAP/пароли приложений как fallback) и как это отражать в UX.
- Стабильный идентификатор пользователя/email в token/userinfo ответе.

---

## 9. План реализации (фазы)

### Фаза 0 — Финализация спеки (текущий этап)
- [ ] Подтвердить актуальные OAuth endpoints/scopes/ограничения Gmail и Mail.ru.
- [ ] Зафиксировать финальные env-переменные и naming.
- [ ] Определить стратегию шифрования токенов/секретов аккаунта в этом репозитории.

### Фаза 1 — Backend фундамент
- [ ] Миграция `mail_0003_oauth` (поля auth/provider/oauth_*; nullable passwords при необходимости).
- [ ] Расширение моделей/схем/сервисов аккаунтов (без утечек секретов в API).
- [ ] Конфиг провайдеров в `app/core/config.py`.
- [ ] OAuth state storage (Redis/ephemeral) + helper'ы PKCE/state.

### Фаза 2 — Gmail OAuth (end-to-end)
- [ ] `start/callback` flow для Gmail.
- [ ] Обмен code→tokens, сохранение, получение/привязка email.
- [ ] XOAUTH2 auth в IMAP/SMTP путях (`sync_account_inbox`, `send_email`).
- [ ] Refresh token flow + обработка `needs_reauth`.
- [ ] UI: кнопка подключения Gmail, статусы, переподключение.

### Фаза 3 — Mail.ru OAuth
- [ ] Провайдерная конфигурация и flow после подтверждения спецификаций.
- [ ] Адаптация под особенности scope/token/идентификации.
- [ ] UI: подключение Mail.ru.

### Фаза 4 — Hardening
- [ ] Шифрование токенов/паролей аккаунтов (и миграция существующих значений при необходимости).
- [ ] Улучшение обработки ошибок/ретраев, таймаутов, аудита без утечек секретов.
- [ ] Тесты: unit (state/PKCE/token refresh/XOAUTH2 formatting), интеграционные mock-провайдером, UI smoke.
- [ ] Документация настройки OAuth apps/redirect URI для деплоя.

---

## 10. Тест-план (минимум)

- Unit:
  - генерация/валидация `state`, TTL, одноразовость;
  - формирование XOAUTH2 auth string;
  - выбор стратегии auth (`password` vs `oauth2`);
  - refresh при истёкшем токене и переход в `needs_reauth`.
- API:
  - `start` возвращает URL и не течёт секреты;
  - `callback` отклоняет невалидный/повторный `state`;
  - аккаунт-сериализация не содержит токенов/паролей.
- Интеграция (mock IMAP/SMTP/OAuth HTTP):
  - успешный sync/send через OAuth;
  - истёкший access token → refresh → успех;
  - `invalid_grant` → `needs_reauth`.

---

## 11. Решения, которые нужно принять до начала кодирования

1. **Шифрование токенов в БД**: какой существующий механизм NetSanctum использовать для секретов на уровне приложения/модуля (и как ротировать ключи).
2. **Где хранить OAuth state**: Redis (предпочтительно) vs таблица.
3. **Модель аккаунта для OAuth-only**: допускаем ли пустые IMAP/SMTP пароли и автозаполнение серверов по провайдеру.
4. **Gmail scopes (финальные)** и необходимость profile/email scopes.
5. **Mail.ru: подтверждённая поддержка IMAP/SMTP OAuth** и точные параметры интеграции.
6. **Поведение callback endpoint**: чистый browser redirect (рекомендуется) + возможный JSON режим для отладки.