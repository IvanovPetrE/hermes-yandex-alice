# Разведка: Алиса как платформенный плагин Hermes

Дата: 2026-11-14. Источник — checkout Hermes v0.23.1 на сервере (только чтение) и плагин
`alice-voice` на закреплённом sha `e31f2c8`. Пути ниже — относительно checkout Hermes.

## Почему не `alice-voice`

`alice-voice` — плагин вида `standalone`, а не платформа:

- HTTP-сервер (`http.server`, поток) поднимается из `ctx.register_telegram_handler(...)` —
  только пока подключён Telegram; в режиме `plugins.isolation: host` не работает.
- Реплика уходит `ctx.inject_message(text, session_key="agent:main:telegram:dm:<id>")` в одну
  фиксированную личку **main**; вызов возвращает только «принято» — ответа агента плагин не видит,
  поэтому Алиса ничего не озвучивает. Профиль — только `default`.
- Стоит взять: строгая проверка тела, `hmac.compare_digest` для пути, отказ от слэш-команд и
  управляющих символов, полное молчание в логах (`log_message`/`handle_error` пустые).

## Целевая форма: `kind: platform`

Штатный путь для нового канала без правки ядра —
`website/docs/developer-guide/adding-platform-adapters.md`, «Plugin Path (Recommended)»:

```
Алиса → Caddy (TLS) → 127.0.0.1:<порт> в процессе шлюза (AliceAdapter.connect: aiohttp)
      → self.handle_message(MessageEvent) → сессия шлюза → AliceAdapter.send(chat_id, ответ)
```

`register(ctx)` → `ctx.register_platform(name="alice", adapter_factory=..., check_fn=..., platform_hint=...,
display_tier="minimal", max_message_length=..., allowed_users_env=...)`.

## Установленные факты

| Вопрос | Ответ | Где |
|---|---|---|
| Профиль voice-profile | `gateway.profile_routes` сравнивает `platform` строкой — `{platform: alice, profile: voice-profile}` подходит плагинной платформе | `gateway/profile_routing.py` (`ProfileRoute.matches`) |
| Инструменты | `platform_toolsets.alice` — плагинные платформы допустимы как `--platform` | adding-platform-adapters.md, «provides_tools» |
| Голосовой стиль и формат «ГОЛОС/ТЕЛЕГРАМ» | `platform_hint` попадает в системный промпт | там же, таблица «What the Plugin System Handles» |
| Удержание ответа ≤ 4,5 с | Готовый образец — LINE: `RequestCache` PENDING→READY→DELIVERED, `send()` кладёт ответ в кэш, `_keep_typing` по порогу, `interrupt_session_activity` на `/stop`; служебные отправки отличает `metadata["_interim_send"]` и префиксы `⚡ ⏳ ⏩ 💾` | `plugins/platforms/line/adapter.py` |
| HTTP-сервер | `aiohttp` есть в окружении шлюза (им пользуются webhook/api_server/LINE); `gateway/platforms/tcp_site.py` не публичен — `web.TCPSite` напрямую | — |
| Секреты | Только `gateway.platforms._shared.get_scoped_secret` / `extra_or_secret`; `os.environ` не читать и не менять (мультиплекс профилей) | `plugins/AGENTS.md` |
| Публичный API | Разрешённые импорты — `website/docs/developer-guide/plugins/public-api.md`; остальное `hermes plugins validate` помечает. Нужное нам: `gateway.platforms.base`, `.event`, `.helpers` (`MessageDeduplicator`, `cancel_task`), `._shared`, `gateway.config`, `gateway.platform_registry`, `tools.approval.resolve_gateway_approval_card` | public-api.md |
| Доставка в Telegram | Штатно, через публичные модули: `platform_registry.get("telegram").standalone_sender_fn(pconfig, chat_id, text)` — тот же путь, что `hermes send` и cron вне шлюза. Токен — `pconfig.token`, иначе `get_secret("TELEGRAM_BOT_TOKEN")` **текущего профиля** | `plugins/platforms/telegram/standalone.py:425`, `gateway/platform_registry.py` |
| Подтверждения опасных команд | `alice` **не** входит в `_UNATTENDED_APPROVAL_PLATFORMS = {"webhook", "msgraph_webhook", "api_server"}` (жёсткий список ядра) → голосовая сессия считается интерактивной: агент ждёт ответа на карточку до таймаута 60–300 с | `tools/approval_context.py:173` |

## Открытые вопросы (проверить до кода адаптера)

1. **Подтверждения.** Что делает базовый `send_exec_approval` у адаптера без кнопок (текст через `send()`?).
   Варианты: сразу отклонять через `resolve_gateway_approval_card(..., "deny", request_id)`; либо
   спрашивать голосом «Разрешить …? да/нет» и решать по ответу; либо переслать карточку в Telegram.
   Решение — за владельцем.
2. **Токен Telegram в сессии voice-profile.** Чат профиля обслуживается ботом main через
   `profile_routes`; есть ли `TELEGRAM_BOT_TOKEN` в области voice-profile — проверить по именам переменных.
   Если нет — брать `pconfig` профиля, владеющего ботом, или отправлять через живой адаптер.
3. **Какие `send()` приходят за ход:** итог, промежуточные (`_interim_send`), стриминг —
   хватит ли `display_tier="minimal"`, чтобы получить один итоговый текст.
4. **Ключ сессии** — см. «Модель диалога» ниже; история — в сессии Hermes (решение 1 ниже).
5. **Cron из Алисы.** Задача, созданная в сессии `alice`, по умолчанию доставит результат в `alice`,
   где никто не слушает → в `platform_hint` назначить доставку в Telegram (аналог `ALICE_CRON_DELIVER`)
   или задать `standalone_sender_fn`, пересылающий в Telegram.
6. **Где лежит плагин при мультиплексе:** `~/.hermes/plugins/alice` main или каталог профиля voice-profile;
   какой профиль поднимает адаптер и слушает порт (`acquire_scoped_lock`, `PORT_BINDING_PLATFORM_VALUES`).
7. **Авторизация.** Владелец — одна учётка Алисы: `allowed_users_env="ALICE_OWNER_USER_ID"` плюс
   собственная проверка `skill_id`/`user_id` до `handle_message`; `trusted_inbound` не ставить.
8. **Перезапуск шлюза** теряет PENDING-кэш — Алиса отвечает `REPLY_LOST` (как сейчас при перезапуске bridge).

## Модель диалога (решение владельца 2026-11-14)

Логика старого bridge не сохраняется. Старая схема — одна команда на голосовую сессию: ответ
озвучивается и сессия закрывается (`end_session: true`), «Голосовая сессия не имеет памяти между
командами». Новая — **диалог**: после ответа сессия Алисы остаётся открытой, следующая реплика
продолжает ту же беседу Hermes с историей. Отсюда вопросы для проектирования:

- **Беседа Hermes ↔ сессия Алисы.** Естественно `chat_id` = `session.session_id` (новый запуск навыка —
  новая беседа). Альтернатива — `user_id` (одна длинная беседа на все запуски); тогда нужен сброс (`/new`).
- **Завершение.** Только по явному «хватит/стоп/выход» или по таймауту Алисы; закрытие сессии Алисой
  само по себе ничего у агента не отменяет.
- **Медленный ответ внутри диалога.** Удержание «Думаю…» остаётся, но реплику во время ожидания
  надо различать: согласие ждать, «пришли в Телеграм» или новая фраза в диалог. Для новой фразы
  шлюз умеет очередь, прерывание или подмешивание (`⚡ ⏳ ⏩`) — выбрать поведение.
- **Telegram.** Дублировать каждый ход или только длинные/«пришли в Телеграм» ответы — от этого
  зависит, нужен ли формат «ГОЛОС/ТЕЛЕГРАМ» в `platform_hint`.
- **Тексты ответов** («Скажите задачу…», «Ответ — в Телеграме.») пересматриваются под диалог.

### Принятые решения (2026-11-14)

1. **История диалога хранится в Hermes**, а не в памяти плагина или bridge: сессия шлюза профиля
   voice-profile, `state.db`. Собственного кэша истории (ходы/байты/TTL) не делаем — переживает
   перезапуск шлюза, агент видит в контексте и результаты инструментов, а не только текст.
2. **Сессия Алисы после ответа не закрывается** (`end_session: false`) — всегда, без маркера от агента.
   Закрытие — явное «хватит/стоп/выход» или таймаут Алисы.
3. **Два открытых состояния вместо одного:**
   - `waiting` — агент ещё думает, Алиса спросила «Думаю… Готовы подождать?»: реплика — ждать
     или «пришли в Телеграм» (интенты, как в bridge);
   - `dialog` — ответ озвучен: **любая** реплика, включая «да»/«нет», — новый ход беседы.
     Эвристика `is_control_phrase` → `REPLY_LOST` в этом состоянии не применяется (иначе «да» на вопрос
     агента теряется).
4. **Один активный ход на сессию Алисы.** Поведение новой фразы во время `waiting` (очередь /
   прерывание / подмешивание) — ещё не выбрано, см. вопрос выше.
5. «Да» в диалоге — подтверждение по смыслу для агента, но не обход шлюза подтверждений Hermes.

### Хранение сессий в Hermes — проверка по коду (2026-11-14)

Только чтение checkout `3b42157858` (v0.23.1); живая проба не ставилась. Это путь API Chat
Completions (старый bridge); плагинная платформа получает сессию шлюза штатно через `handle_message`,
её поведение надо проверить отдельно.

| Что | Как устроено | Где |
|---|---|---|
| Продолжение беседы | `X-Hermes-Session-Id` → история из `state.db` (`get_messages_as_conversation`), история из тела запроса **игнорируется**; после сжатия контекста id ведёт на живой «кончик» | `gateway/platforms/api_server_openai_routes.py:899–927` |
| Ограничения id | Только при настроенном `API_SERVER_KEY` (иначе 403); ≤ 256 символов, без `\r\n\0` и небезопасных для пути символов | там же, `api_server.py:1598` |
| Без заголовка (текущий bridge) | Id выводится из системного промпта + первой реплики пользователя (`_derive_chat_session_id`) → одинаковые команды bridge попадают в одну сессию `state.db`, но история из базы не подгружается | там же, `:928–936` |
| Чужие сессии | API не может привязаться к сессии другой поверхности (Telegram, CLI) — 409 `permission_denied` | `website/docs/user-guide/features/api-server.md:502` |
| Удаление | Удалённый id навсегда «выведен»: новый запрос с ним — `not_found` | api-server.md:149 |
| Подтверждения | Non-streaming Chat Completions не регистрирует notifier → `api_server` решает по `approvals.unattended_mode` (у нас `deny`) **независимо от заголовка сессии**; streaming регистрирует notifier и ждёт `POST /v1/runs/{id}/approval` | `tools/approval.py:1050–1085`, `tools/approval_context.py:178` |
| Фоновые subagent'ы | Отложенная доставка результата делегирования — только с явным `X-Hermes-Session-Id`; без него делегирование синхронное | api-server.md:571 |

Вывод: сессии Hermes закрывают хранение истории; для плагина это штатное поведение шлюза. Для
`alice` остаётся открытым вопрос 1 (подтверждения): в отличие от `api_server`, платформа считается
интерактивной.

## Что переносится из старого bridge

Стартовая точка, будет переделываться под диалог. Перенесено в `alice/protocol.py` без изменения поведения: тексты ответов, `alice_reply`, проверка тела,
`speakable`/`split_answer`, интенты и `classify_reply`, `is_control_phrase`, `RateLimiter`.
Не нужны: `ask_agent` (Chat Completions + ключи профилей), `deliver` + `sign_v2` (webhook `alice-reply`),
`_DeadlineSocket`/`_post`, свой `ThreadingHTTPServer`. `Jobs`/`Turn` заменяются кэшем по образцу LINE.

## Риски

- Код работает внутри шлюза: ошибка или зависание плагина задевают все профили и Telegram;
  перезапуск шлюза ≈ минута без Алисы.
- Внутренние модули Hermes — не API (старые пути удаляются без шимов); держаться `public-api.md`
  и прогонять `hermes plugins validate` после каждого `hermes update`.
- Локально Hermes нет: адаптер тестируется на заглушках базовых классов или на checkout сервера.
