# Прототип S1: финальность и привязка ответа к ходу

Этап 1, S1 (`docs/PLAN.md`). Отвечает на вопрос: может ли плагинная платформа `yandex_alice` по
`send()` и хукам базового адаптера отличить **итог хода** от промежуточных отправок и узнать, что
ход закончился (в том числе пустой или с ошибкой). Вердикт и выводы — `docs/RECON.md`, раздел «S1».

## Что внутри

- `probe_s1.py` — заглушка-адаптер `StubAliceAdapter` (журнал всех вызовов шлюза, без сети и порта),
  правило классификации `TurnTracker` (его unit-тесты `TrackerRuleTests` идут без Hermes, в том числе
  локально: `py -m unittest discover -s prototypes/s1 -p probe_s1.py -k TrackerRuleTests`) и сценарии на `unittest`:
  - группа A — `display_tier="minimal"`, глобальный стриминг включён, `busy_input_mode: interrupt`;
  - группа B — переопределения оператора (`display.tool_progress: all`,
    `display.interim_assistant_messages: true`, `busy_input_mode: queue`).
- `run.sh` — запуск во временном `HOME`/`HERMES_HOME`/каталоге блокировок, `env -i`, `timeout`,
  `nice`, `choom` (при нехватке памяти ядро убьёт пробу, а не шлюз).

Агент скриптованный — `tests/fakes/fake_llm_provider.py` из checkout Hermes: настоящей модели,
ключей и расходов нет.

## Только для пробы

Проба импортирует `gateway.run.GatewayRunner` и `tests.*` Hermes — это **не** публичный API плагинов
(`website/docs/developer-guide/plugins/public-api.md`), а тестовая обвязка, которая поднимает шлюз.
В плагин (`yandex-alice/`) ничего отсюда не переносится. В CI GitHub проба не запускается
(нужен полный рантайм Hermes); на этапе 6 её заменит тест совместимости адаптера.

## Запуск

Нужны checkout Hermes и `python` закоммиченного окружения зависимостей Hermes
(`<HERMES_HOME>/installs/<key>/environments/<generation>/venv/bin/python` — его `site-packages` виден в
`sys.path` после `import hermes_bootstrap`). Интерпретатор из `hermes --print-runtime-command` не
подходит: при временном `HERMES_HOME` он не находит окружение (`no dependency environment is
committed`). Каталог пробы кладётся в свежий `/tmp/hya-s1-XXXXXX`:

```sh
D=$(mktemp -d /tmp/hya-s1-XXXXXX)
cp -r prototypes/s1 "$D/"
bash "$D/s1/run.sh" <hermes-checkout> <hermes-python> A
bash "$D/s1/run.sh" <hermes-checkout> <hermes-python> B
rm -rf -- "$D"
```

Каждая группа — отдельный процесс с одним `GatewayRunner`. Вывод: журнал вызовов адаптера по
сценариям (JSON-строки), действующие настройки, пиковый RSS и итог `unittest`. Тексты реплик и
идентификаторы — синтетические.

Проверено против Hermes v0.23.1 (`3b42157858`).
