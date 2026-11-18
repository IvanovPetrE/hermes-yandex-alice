# Прототип S2: профиль и `profile_routes` для плагинной платформы

Этап 1, S2 (`docs/DESIGN.md`). Отвечает на вопрос: при мультиплексе профилей
(`gateway.multiplex_profiles: true`) может ли плагин `kind: platform` жить в профиле **по умолчанию**
(каталог плагина, `plugins.enabled`, `platforms.yandex_alice`, `ALICE_*` в его `.env`), а маршрут
`gateway.profile_routes` без `user_id` переводить каждый ход в другой профиль — с его SOUL,
`platform_toolsets.yandex_alice`, `state.db` и историей; один адаптер и один listener; список
допуска владельца — из профиля по умолчанию. Отдельно: доходят ли `platform_hint` и `display_tier`
плагина до хода в целевом профиле (регистрации плагинов в Hermes разделены по `HERMES_HOME`).
Вердикт и выводы — `docs/RECON.md`, раздел «S2».

## Что внутри

- `probe_s2.py` — строит временный дом: профиль по умолчанию и профиль `p2`, кладёт **настоящий
  каталог плагина** (`plugin.yaml` + `__init__.py` с `register(ctx)` → `ctx.register_platform`), который
  находит штатное обнаружение плагинов Hermes. Заглушка-адаптер импортирует только модули из
  `public-api.md`, пишет журнал вызовов шлюза (с `get_hermes_home()` и `get_scoped_secret` в каждом
  вызове), в `connect()` открывает один настоящий loopback-сокет. Группы — по процессу на каждую:

  | Группа | Плагин / `plugins.enabled` | `platforms.yandex_alice.enabled` | Ещё | `allowlist_suppresses_pairing` |
  |---|---|---|---|---|
  | A1 | только default | default | — | `True` |
  | A2 | default и `p2` | только default | — | `True` |
  | A2g | default и `p2` | только default | `validate_config` требует `ALICE_PATH_SECRET` в текущей области | `True` |
  | A3 | только default | default | в `p2`: `platform_hints`, `display.platforms` | `True` |
  | A4 | только default | default | в default: `extra.unauthorized_dm_behavior: ignore` | `True` |
  | A0 | только default | default | — | `False` (только сценарий чужого) |
  | B | default и `p2` | default и `p2` | как A2g, `ALICE_PATH_SECRET` и в `.env` `p2` | `True` |

- `run.sh` — запуск во временном `HOME`/`HERMES_HOME`/каталоге блокировок, `env -i`, `timeout`,
  `nice`, `choom`; сторож памяти в самой пробе (`S2_RSS_LIMIT_MB=400`, выход с кодом 137).
  `ALICE_*` в окружение процесса не передаются — только через `.env` профилей.

Агент скриптованный — `tests/fakes/fake_llm_provider.py` из checkout Hermes: настоящей модели,
ключей и расходов нет. Тела запросов к нему показывают системный промпт и набор инструментов хода.

## Только для пробы

Проба импортирует `gateway.run.GatewayRunner`, `tests.*` Hermes и читает приватные атрибуты
runner'а — это **не** публичный API плагинов, а обвязка, которая поднимает и осматривает шлюз.
В плагин (`yandex-alice/`) ничего отсюда не переносится. В CI GitHub проба не запускается.

## Запуск

Нужны checkout Hermes и `python` закоммиченного окружения зависимостей Hermes (как в
`compat/README.md`) и свободный loopback-порт (проверить `ss -ltn`):

```sh
D=$(mktemp -d /tmp/hya-s2-XXXXXX)
cp -r prototypes/s2 "$D/"
for G in A1 A2 A2g A3 A4 A0 B; do bash "$D/s2/run.sh" <hermes-checkout> <hermes-python> "$G" <port>; done
rm -rf -- "$D"
```

Вывод: действующие настройки обвязки, журнал вызовов каждого адаптера (JSON-строки), строки
`sessions` обоих `state.db`, сводка по запросам к LLM (инструменты, маркеры SOUL и подсказки), пиковый
RSS и итог `unittest`. Тексты реплик и идентификаторы — синтетические.

Написано под Hermes v0.23.1 (`3b42157858`); результат прогона — `docs/RECON.md`, «S2».
