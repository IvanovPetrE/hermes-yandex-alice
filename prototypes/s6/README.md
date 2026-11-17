# Прототип S6: сессия голосовой платформы и cron, созданный голосом

Этап 1, S6 (`docs/PLAN.md`). Отвечает на вопросы: может ли плагин `kind: platform` в профиле по умолчанию
мультиплексирующего шлюза, только через публичный API, (а) опираться на сессию шлюза как на историю беседы —
ключ, история, что делает сжатие контекста; (б) доставить в Telegram результат cron, созданного голосом, а не
потерять его на адресе `origin` (Алиса не может заговорить первой); (в) переслать в Telegram с подписью
«Алиса:» прочие отправки вне хода; (г) закрыть ход без `clarify`, если агент спросил встречно. Вердикт и
выводы — `docs/RECON.md`, «S6».

## Что внутри

- `plugin/` — **модель адаптера** `yandex-alice-s6` (находится штатным обнаружением Hermes). Импортирует
  только модули из `public-api.md`, `os.environ` не читает (секреты и чат — `get_scoped_secret`).
  HTTP-входа нет: его проверила S5, ходы проба подаёт напрямую (`probe_inject`).
  - `__init__.py`: `send()` классифицирует отправку; отправка **вне** открытого хода, не служебная и не
    восстановленная, уходит в Telegram как `Алиса:\n\n<текст>` ботом профиля по умолчанию (свой
    `PlatformConfig` из `connect()`, задача в копии его контекста — путь S3). Хук `pre_tool_call`: в голосовой
    сессии к аргументам `cronjob_manage` добавляется результат `cron_guard.guard_args`; хук никогда не бросает.
    `standalone_sender_fn` лишь отмечает, что cron его вызвал, и отказывает.
  - `cron_guard.py` — чистая логика (stdlib): при `create` пустой `deliver` и при `create`/`update` явные
    `origin`/`yandex_alice[:…]` в `deliver` и `failure_deliver` → `telegram:<ALICE_TELEGRAM_CHAT>`; явный выбор
    агента (`local`, `telegram:…`, прочее) не трогается; пропущенный `deliver` при `update` — «не менять».
  - `recovery.py` — копия из S5 (`classify_send`, срез пометки леджера) плюс `forward_text`: что пересылать.
- `probe_s6.py` — `GuardTests`, `ForwardTests` (без Hermes) и группы A–C: настоящий `GatewayRunner` с
  мультиплексом (default + `p2` — цель маршрута `yandex_alice` и Telegram-чата владельца, без своего бота),
  настоящий Telegram-адаптер на поддельном Bot API Hermes, тикер cron и housekeeping шлюза, скриптованный LLM.
- `run.sh` — запуск во временном `HOME`/`HERMES_HOME`/`TMPDIR`/lock, `env -i`, `timeout 1500`, `nice`, `choom`,
  сторож памяти `S6_RSS_LIMIT_MB=450`.

Группы: A — плагин только в профиле по умолчанию: K1/K3 (ключ и история), Z1 (сжатие), Q1–Q3 (`clarify`),
C1–C3 (cron без `deliver`, в чат с маршрутом, в чат без маршрута), C4 (`action=run` из голоса), H1 (хук),
O1 (ошибка вне хода → Telegram); B — плагин ещё и в `p2`, `ALICE_TELEGRAM_CHAT` в его `.env`: H2 (что хук
переписывает), H3 (задача из голоса доходит до Telegram), H5 (Telegram-ход `p2` хук не трогает); C — плагин в
`p2` без этого чата: H4 (что видит хук).

Утверждения жёсткие там, где критерий двоичный (K1, K3, Q2, C2, O1, H3, H5, «внутри хода ничего не
пересылается», validate); остальные сцены печатают факты для вердикта.

## Только для пробы

Обвязка импортирует `gateway.run.GatewayRunner`, `gateway.run_bootstrap._start_gateway_start_cron_and_housekeeping`,
`tests.fakes.*`, `hermes_cli.plugin_validate`, читает `state.db`, `cron/jobs.json` и `cron/deliveries.db`
профиля и трогает приватные атрибуты runner'а и адаптера (`_profile_adapters`, `_failed_platforms`,
`_message_handler`) — это то, что поднимает и осматривает шлюз, а не код плагина. В CI GitHub проба не запускается.

## Запуск

`GuardTests` и `ForwardTests` — без Hermes (Python ≥ 3.11):

```sh
cd prototypes/s6 && python -m unittest probe_s6 -k GuardTests -k ForwardTests
```

Шлюз — нужны checkout Hermes и `python` закоммиченного окружения зависимостей (как в
`prototypes/s1/README.md`); по процессу на группу:

```sh
D=$(mktemp -d /tmp/hya-s6-XXXXXX)
cp -r prototypes/s6 "$D/"
for G in A B C; do bash "$D/s6/run.sh" <hermes-checkout> <hermes-python> "$G"; done
case "$D" in /tmp/hya-s6-*) rm -rf -- "$D" ;; esac
```

Вывод — JSON результата каждой сцены (журнал адаптера и хука, строки `sessions`/`gateway_routing`, задачи
cron и строки очереди доставки, сообщения поддельного Bot API), отчёт `plugins validate`, пиковый RSS и итог
`unittest`. Память: ~260 МБ на группу; группы A и B ждут тикера cron (60 с) и drain доставки (60 с).

Написано под Hermes v0.23.1 (`3b42157858`); результат прогона — `docs/RECON.md`, «S6».

Каноническая копия чистого слоя — `yandex-alice/cron_guard.py` (этап 3, срез «а»); здесь — запись эксперимента.
