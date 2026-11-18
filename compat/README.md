# Тесты совместимости с Hermes

Прогоняют **наш** `AliceAdapter` внутри настоящего `GatewayRunner` Hermes на закреплённой ревизии
(`docs/compatibility.md`): плагин ставится копией в `plugins/`, шлюз грузит его через `discover_plugins`, запрос Алисы идёт по
HTTP в loopback-listener, агент — скриптованный LLM из `tests/fakes` Hermes (ни модели, ни ключей, ни расходов).

Тестовая обвязка Hermes (`gateway.run`, `tests.fakes.*`) — не публичный API плагинов; в `compat/` и `prototypes/` она разрешена,
в `yandex-alice/` — нет.

| Группа | Что проверяет |
|---|---|
| `P` | `validate_plugin_dir` манифеста, регистрация платформы (`source`, `display_tier`, `allowed_users_env`) |
| `A` | текст, инструмент, пустой ответ модели и сбой провайдера → «не получилось», уведомление «📬» не озвучивается, порядок start → итог → complete |
| `B` | вторая фраза в занятую беседу не доходит до Hermes, гейт Р2, хук `on_processing_complete` после итога |

Не покрыто (следующий срез): перезапуск шлюза и `interactive_resume` (S5), чтение истории (S7).

## Запуск

```sh
bash compat/run.sh <hermes-checkout> <hermes-python> <P|A|B>
```

- `<hermes-checkout>` — git checkout Hermes на ревизии из `docs/compatibility.md`.
- `<hermes-python>` — `python` окружения зависимостей Hermes. На установленном Hermes это
  `<HERMES_HOME>/installs/<key>/environments/<generation>/venv/bin/python` (интерпретатор из `hermes --print-runtime-command`
  не годится: при временном `HERMES_HOME` он не находит закоммиченное окружение). В CI — `python` из `uv sync`.
- Скрипт сам создаёт временный каталог `hya-compat-*` (HOME, `HERMES_HOME`, блокировки), запускает под `env -i`, `timeout`, `nice`,
  `choom` (при нехватке памяти ядро убьёт прогон, а не живой шлюз) и удаляет каталог при выходе. Настоящий `HERMES_HOME` не
  используется. Каждая группа — отдельный процесс: пиковый RSS ~100 МБ (P) и ~250–270 МБ (A, B), A идёт около минуты.
- Пропущенный тест или ноль запущенных — выход 1. Без checkout или с неверным python — выход 2.
- Каталог лежит вне `tests/`, поэтому `python -m unittest discover -s tests` его не собирает.
