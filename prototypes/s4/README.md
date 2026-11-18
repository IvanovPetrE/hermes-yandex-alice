# Прототип S4: подтверждения опасных действий — карточка в Telegram и нажатие владельца

Этап 1, S4 (`docs/PLAN.md`). Отвечает на вопрос: может ли плагин `kind: platform` в профиле **по
умолчанию** мультиплексирующего шлюза, только через публичный API, показать владельцу в Telegram
карточку «Разрешить один раз» / «Отклонить» (ботом профиля по умолчанию) для подтверждения, которое
поднял ход, маршрутизированный в другой профиль; принять **только** нажатие владельца в своём
обработчике и разрешить именно этот запрос; а при любом сбое доставки — сразу отказать. Вердикт и
выводы — `docs/RECON.md`, «S4».

## Что внутри

- `plugin/` — **модель адаптера**: настоящий каталог плагина `yandex-alice-s4`, его находит штатное
  обнаружение Hermes. Импортирует только модули из `public-api.md`; python-telegram-bot — внутри функций.
  - `__init__.py`: `register(ctx)` → `ctx.register_platform(...)` и
    `ctx.register_platform_handler("telegram", _wire)`. Фабрика запоминает PTB `Application` только из
    дома плагина и ставит `CallbackQueryHandler(pattern="^hya:")`. Адаптер переопределяет
    `_send_exec_approval_prompt`: карточка из полей `ExecApprovalPrompt`, ботом, чей токен совпадает с
    токеном области плагина; срок отправки 10 с; ошибка, срок, нет бота или подтверждения отключены →
    `resolve_gateway_approval(..., "deny", reason=..., request_id=...)`. Нажатие → проверки книги →
    `resolve_gateway_approval_card(session_key, choice, request_id)` → правка карточки.
    `send()` отвечает отказом только на блок полной команды перед урезанной карточкой (метка Hermes
    «только отклонить» остаётся); `edit_message` правит карточку на таймауте Hermes.
  - `approval_book.py` — чистая логика (stdlib): состояния карточки, кто нажимает, правила кнопок,
    текст карточки. Образец чистого слоя этапа 5.
- `probe_s4.py` — обвязка: `BookTests` (без Hermes) и группы `GroupA`/`GroupB`/`GroupC` — настоящий
  `GatewayRunner` с мультиплексом: default (плагин, `yandex_alice`, настоящий Telegram-адаптер на
  поддельном Bot API Hermes `tests/fakes/platforms/telegram_standin`), `p2` — цель маршрута
  (`platform_toolsets.yandex_alice: [terminal]`, `approvals: manual`, `timeout: 20`), `p3` — свой бот на
  втором поддельном API. Агент — скриптованный LLM Hermes. Опасная команда — `rm -r <корень>/victim/<сцена>`:
  каталог исчезает только при разрешении. Токены и ID синтетические (токены — не в формате Telegram).
- `run.sh` — запуск во временном `HOME`/`HERMES_HOME`/`TMPDIR`/lock, `env -i`, `timeout 900`, `nice`,
  `choom`; сторож памяти в пробе (`S4_RSS_LIMIT_MB=400`: шлюз с двумя PTB-приложениями — ~310 МБ уже
  при старте; выход с кодом 137).

Группы: A — личка владельца без `ALICE_TELEGRAM_OWNER_ID` (сцены разрешить, отклонить, чужой и другой
чат, позднее нажатие, таймаут, ошибка Bot API, «повисшая» отправка с нажатием во время неё, длинные
команды, подделанный токен, две команды за ход, долгий ход в той же личке); B — группа без
`ALICE_TELEGRAM_OWNER_ID` (подтверждения отключены); C — группа с `ALICE_TELEGRAM_OWNER_ID`.

## Только для пробы

Обвязка импортирует `gateway.run.GatewayRunner`, `tests.*`, `tools.approval.list_gateway_approvals`,
`hermes_cli.plugin_validate` и смотрит приватные атрибуты runner'а — это то, что поднимает и осматривает
шлюз, а не код плагина. В CI GitHub проба не запускается.

## Запуск

`BookTests` — без Hermes (Python ≥ 3.11):

```sh
cd prototypes/s4 && python -m unittest probe_s4 -k BookTests
```

Шлюз — нужны checkout Hermes и `python` закоммиченного окружения зависимостей (как в
`compat/README.md`); по процессу на группу:

```sh
D=$(mktemp -d /tmp/hya-s4-XXXXXX)
cp -r prototypes/s4 "$D/"
for G in A B C; do bash "$D/s4/run.sh" <hermes-checkout> <hermes-python> "$G"; done
rm -rf -- "$D"
```

Вывод — журнал плагина (подключения фабрики, подсказки, отказы, нажатия, правки), итог каждой сцены
(кнопки, ответы на нажатия, текст результата инструмента, итог хода, временная шкала «ждёт
подтверждения»), отчёт `plugins validate`, пиковый RSS и итог `unittest`.

Написано под Hermes v0.23.1 (`3b42157858`); результат прогона — `docs/RECON.md`, «S4».
