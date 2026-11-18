# Прототип S6b: где работает хук `pre_tool_call` голосового хода

Этап 1, дополнение к S6 (`docs/DESIGN.md`). Вопрос: если шлюз ведёт ход в управляемом рабочем процессе
(`agent/managed_worker.py`), сработает ли там хук плагина целевого профиля и увидит ли он `ALICE_TELEGRAM_CHAT`?
По коду Hermes v0.23.1 у хода платформы `yandex_alice` рабочего процесса нет вовсе: `managed_policy`
(`gateway/session_managed_worker.py`) возвращает политику только для `Platform.LOCAL`. Проба проверяет это в
работающем шлюзе, с `gateway.managed_workers: true` в обоих профилях. Вердикт — `docs/RECON.md`, «S6» → «S6b».

## Что внутри

- `plugin/` — плагин S6 (`prototypes/s6/plugin`) + `pid` и файловый журнал `<HERMES_HOME>/s6b-journal.jsonl` в
  `_rec` + `provides_hooks: [pre_tool_call]` в манифесте. `cron_guard.py` и `recovery.py` — байтовые копии S6
  (`CopyTests` следит за расхождением).
- `probe_s6b.py` — использует обвязку S6 (`prototypes/s6/probe_s6.py`: настоящий `GatewayRunner`, мультиплекс
  default + `p2`, скриптованный LLM, поддельный Bot API) и подменяет путь плагина. Добавляет `gateway.managed_workers:
  true` в оба `config.yaml`, шпиона на `subprocess.Popen` и сцену W0.
- `run.sh` — как у S6, но `HERMES_EXEC_ASK=1` (его ставит `start_gateway()`) вместо `HERMES_GATEWAY_SESSION=1`.

Сцены: W0 — для всех сессий `managed_policy` = `None`, запусков `agent.managed_worker` нет; W1 — шпион работает
(видит процесс, который запустила сама проба); W2 — хук сработал в pid шлюза, видит чат и переписал `deliver` (B);
W3 — задача из голоса дошла до Telegram (B); W4 — без чата в `.env` p2 хук его не видит (C).

## Только для пробы

Обвязка трогает `gateway.*`, `tests.*`, приватные атрибуты runner'а и `subprocess.Popen.__init__` — это то, что
поднимает и осматривает шлюз, а не код плагина (плагин импортирует только `public-api.md`). В CI не запускается.

## Запуск

Чистый слой — без Hermes (Python ≥ 3.11):

```sh
cd prototypes/s6b && python -m unittest probe_s6b.GuardTests probe_s6b.ForwardTests probe_s6b.CopyTests
```

Шлюз — нужны checkout Hermes и `python` окружения зависимостей (`compat/README.md`); каталоги `s6` и `s6b`
рядом, по процессу на группу:

```sh
D=$(mktemp -d /tmp/hya-s6b-XXXXXX)
# выложить prototypes/s6 и prototypes/s6b в "$D/"
for G in B C; do bash "$D/s6b/run.sh" <hermes-checkout> <hermes-python> "$G"; done
case "$D" in /tmp/hya-s6b-*) rm -rf -- "$D" ;; esac
```

После прогона проверить, что шлюз пробы не оставил `python -m hermes_cli.main --profile default gateway`
(родитель — init, `HERMES_HOME` в `/proc/<pid>/environ` — временный дом); такой процесс завершить до удаления
каталога, иначе повторный запуск на том же каталоге даёт `FATAL: session storage stopped writing…`.

Группа B ждёт тикера cron (60 с) и drain доставки (60 с); память ~260–300 МБ на группу.
