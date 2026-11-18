# Прототип S3: подробный ответ в Telegram через `standalone_sender_fn`

Этап 1, S3 (`docs/DESIGN.md`). Отвечает на вопрос: может ли плагин `kind: platform`, живущий в профиле
**по умолчанию** мультиплексирующего шлюза, доставить текст в настроенный чат Telegram ботом профиля по
умолчанию через `platform_registry.get("telegram").standalone_sender_fn(pconfig, chat_id, text)` — из
контекста своего `connect()` и из области маршрутизированного профиля, внутри работающего event loop,
с различимым результатом и без утечки токена и текста в логи. Вердикт и выводы — `docs/RECON.md`, «S3».

## Что внутри

- `probe_s3.py` — один процесс, один `asyncio`-loop, **без GatewayRunner**. Временный дом: профиль по
  умолчанию (свой токен бота, настройка чата `ALICE_TELEGRAM_CHAT`), профиль `p2` без своего токена
  (в его config — `display.platforms.telegram.notifications: all`, в `.env` — `TELEGRAM_PROXY` на
  loopback-регистратор), профиль `p3` со своим токеном бота. Области профилей ставятся так же, как шлюз
  входит в ход маршрутизированного профиля (`set_hermes_home_override` + `set_secret_scope`).
  Telegram — поддельный Bot API на loopback (`extra.base_url`); настоящий Telegram не вызывается.
  Токены и ID синтетические, но в формате токена (маскировка Hermes узнаёт только `<8+ цифр>:<30+ символов>`).
  - `DetailsSender` — **модель адаптера**: импортирует только модули из `public-api.md`; в `connect()`
    читает токен и чат, берёт отправителя из реестра, собирает свой `PlatformConfig` (ключи `extra` — по
    белому списку с префиксом `telegram_`) и снимает контекст; `deliver()` — задача в копии этого
    контекста, `notify=True`, общий срок 60 с, без своих повторов; итог `delivered` / `failed` /
    `uncertain` / `disabled`.
  - Группы (по порядку): `A_Registry` — первый `get("telegram")` в каждой области и `connect()` в
    свежем процессе (время, лаг loop); `B_TokenScope` — чей токен уходит при `token=None` и при своём
    pconfig, область вызова против контекста `connect()` (по прокси и `disable_notification`);
    `C_Results` — успех, нет токена, 401, 403, 400, 500, 429, HTML 502, закрытый порт, «повис», ошибка
    разметки, длинный текст, срок; `D_Loop` — loop и listener во время «повисшей» отправки, fd и
    ResourceWarning за 20 отправок; `E_Logs` — логи как в шлюзе (`hermes_logging`, `RedactingFormatter`)
    решают; сырой DEBUG всех логгеров — только для сведения.
- `run.sh` — запуск во временном `HOME`/`HERMES_HOME`/`TMPDIR`, `env -i`, `timeout 600`, `nice`,
  `choom`; сторож памяти в самой пробе (`S3_RSS_LIMIT_MB=300`, выход с кодом 137).

## Только для пробы

Обвязка импортирует `hermes_cli.plugins.discover_plugins`, `hermes_logging`, функции областей
(`agent.secret_scope`, `hermes_constants`) и смотрит приватные карты реестра — это то, что поднимает и
осматривает окружение, а не код плагина. В плагин (`yandex-alice/`) из обвязки ничего не переносится.
В CI GitHub проба не запускается.

## Запуск

Нужны checkout Hermes и `python` закоммиченного окружения зависимостей Hermes (как в
`compat/README.md`):

```sh
D=$(mktemp -d /tmp/hya-s3-XXXXXX)
cp -r prototypes/s3 "$D/"
bash "$D/s3/run.sh" <hermes-checkout> <hermes-python>
rm -rf -- "$D"
```

Вывод — JSON-строки `{"s3": ...}` (журнал запросов к поддельному Bot API: метод, чей токен, chat_id,
длина текста, `parse_mode`, `disable_notification`, через прокси или напрямую; итог модели, время, текст
ошибки), пиковый RSS и итог `unittest`. Тексты, токены и ID — синтетические.

Написано под Hermes v0.23.1 (`3b42157858`); результат прогона — `docs/RECON.md`, «S3».
