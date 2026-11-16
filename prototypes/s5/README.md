# Прототип S5: жизненный цикл адаптера и восстановление после перезапуска шлюза

Этап 1, S5 (`docs/PLAN.md`). Отвечает на вопросы: плагин `kind: platform`, только через публичный API,
(L1) не открывает порт при импорте, (L2) держит ровно один listener после `connect()`, (L3) не плодит и
не роняет его при `reload-plugins`/включении, (L4) и при переподключении шлюзом, (L5) освобождает порт при
остановке; (P1) при занятом порте не ломает остальной шлюз и сам переподключается, когда порт свободен;
(P2) не пишет секретный путь в логи; (R1–R5) что приходит в адаптер после перезапуска шлюза посреди хода и
как отличить восстановленный ответ по форме, а не по английскому тексту пометки Hermes. Вердикт и выводы —
`docs/RECON.md`, «S5».

## Что внутри

- `plugin/` — **модель адаптера** `yandex-alice-s5` (находится штатным обнаружением Hermes). Импортирует
  только модули из `public-api.md` и `aiohttp`, `os.environ` не читает (порт и флаги — из
  `platforms.yandex_alice.extra`, секретный путь — `get_scoped_secret`).
  - `__init__.py`: `register(ctx)` только регистрирует (`yandex_alice` с `validate_config` по секрету и
    платформа-заглушка `s5_other` без порта); `connect()` — `AppRunner(access_log=None)` + `TCPSite` на
    loopback; занятый порт — `bind_failed`, **retryable**; `disconnect()` — `site.stop` + `runner.cleanup`;
    `POST /<секрет>` → `handle_message`; `send()` классифицирует и пишет в журнал только метаданные;
    `_send_exec_approval_prompt` — журнал вместо карточки; `interactive_resume` из `extra`.
  - `recovery.py` — чистая логика (stdlib): `classify_send` (правило итога S1 + «восстановленный вне
    хода») и срез пометки леджера **по форме** (первый абзац начинается с «♻» и кончается «:» перед пустой
    строкой). Образец для чистого слоя этапов 2–4.
- `probe_s5.py` — обвязка: `RecoveryTests` (без Hermes) и группы A–D. Проба держит скриптованный LLM
  Hermes и «чужой» listener, а шлюз запускает **дочерним процессом** (`probe_s5.py child`): настоящий
  `GatewayRunner` с мультиплексом (default + `p2`, цель маршрута `yandex_alice`). «Перезапуск» — новый
  дочерний процесс в том же доме: штатная остановка (`runner.stop()`) или `SIGKILL`. У каждой сцены
  перезапуска свой дом (счётчик перезапусков Hermes — на дом). Команды ребёнку — JSON-строки в stdin.
- `run.sh` — запуск во временном `HOME`/`HERMES_HOME`/`TMPDIR`/lock, `env -i`, `timeout 1500`, `nice`,
  `choom`; сторож памяти считает пробу и всех живых детей вместе (`S5_RSS_LIMIT_MB=500`, выход 137).

Группы: A — `RecoveryTests` + L1–L6 и R6 (ошибка после закрытого хода); B — занятый порт, P1/P2 на уровне
логов DEBUG; C — R1–R5 при `interactive_resume=false` и `gateway_restart_notification: false`; D — R1, R2
при настройках Hermes по умолчанию (`interactive_resume=true`, уведомление включено).

Утверждения жёсткие там, где критерий двоичный (L1–L6, P1, P2); сцены перезапуска (R1–R5, D1–D2) печатают
факты (строки журнала, запросы к LLM, строки лога шлюза), по которым записан вердикт.

## Только для пробы

Обвязка импортирует `gateway.run.GatewayRunner`, `gateway.run_plugin_rewire.reload_plugins_verb`,
`tests.fakes.fake_llm_provider`, `hermes_cli.plugin_validate`, трогает приватные атрибуты runner'а и адаптера
(`_failed_platforms`, `_message_handler`, `_set_fatal_error`, `_notify_fatal_error`) — это то, что поднимает и
осматривает шлюз, а не код плагина. В CI GitHub проба не запускается.

## Запуск

`RecoveryTests` — без Hermes (Python ≥ 3.11):

```sh
cd prototypes/s5 && python -m unittest probe_s5 -k RecoveryTests
```

Шлюз — нужны checkout Hermes и `python` закоммиченного окружения зависимостей (как в
`prototypes/s1/README.md`); по процессу на группу:

```sh
D=$(mktemp -d /tmp/hya-s5-XXXXXX)
cp -r prototypes/s5 "$D/"
for G in A B C D; do bash "$D/s5/run.sh" <hermes-checkout> <hermes-python> "$G"; done
case "$D" in /tmp/hya-s5-*) rm -rf -- "$D" ;; esac
```

Вывод — JSON результата каждой группы (журнал адаптера по сценам, запросы к LLM после перезапуска,
отобранные строки лога шлюза), отчёт `plugins validate`, пиковый суммарный RSS и итог `unittest`.
Память: проба ~100 МБ + шлюз ~250 МБ; перед запуском `available` ≥ 150 МБ. Группа B ждёт повторных попыток
шлюза (30–300 с) — около 4–5 минут.

Написано под Hermes v0.23.1 (`3b42157858`); результат прогона — `docs/RECON.md`, «S5».
