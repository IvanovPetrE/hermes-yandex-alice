# hermes-yandex-alice

Яндекс Алиса как платформа шлюза [Hermes Agent](https://github.com/NousResearch/hermes-agent):
плагин `kind: platform`, который принимает webhook приватного навыка, отдаёт реплику в обычную
сессию шлюза (профиль задаётся `gateway.profile_routes`), озвучивает ответ с удержанием сессии в
пределах 4,5 с (длинный — по частям) и по запросу присылает подробности в Telegram. Режим — диалог:
после ответа сессия Алисы остаётся открытой и следующая реплика продолжает ту же беседу.

Статус: **разведка**. Готово — чистая логика с тестами: `yandex-alice/protocol.py` (протокол Диалогов под диалог:
интенты, нарезка голоса на части, разбор ответа агента и ошибок шлюза), `yandex-alice/dialog.py` (машина беседы:
ожидание, ответ позже, чтение по частям, сбои доставки) и `yandex-alice/sends.py` (что такое отправка шлюза). Адаптера
ещё нет; факты и открытые вопросы — `docs/RECON.md`.

## Структура

```
yandex-alice/      каталог плагина → ~/.hermes/plugins/yandex-alice
  plugin.yaml      манифест (черновик)
  protocol.py      протокол Диалогов, тексты, интенты — без зависимостей от Hermes
  dialog.py        машина беседы step(state, event) — без зависимостей от Hermes
  sends.py         правило итога хода и пересылки вне хода — без зависимостей от Hermes
tests/             unittest, Hermes не нужен
prototypes/s1/     проба S1 (финальность ответа) — запуск вручную против checkout Hermes
prototypes/s2/     проба S2 (профиль и profile_routes) — запуск вручную против checkout Hermes
prototypes/s3/     проба S3 (подробности в Telegram) — запуск вручную против checkout Hermes
prototypes/s4/     проба S4 (подтверждения через Telegram) — запуск вручную против checkout Hermes
prototypes/s5/     проба S5 (жизненный цикл и перезапуск шлюза) — запуск вручную против checkout Hermes
prototypes/s6/     проба S6 (сессия и cron из голоса) — запуск вручную против checkout Hermes
docs/PLAN.md       план работ
docs/RECON.md      разведка
```

## Тесты

```
py -m unittest discover -s tests
```
