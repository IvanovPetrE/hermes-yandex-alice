# hermes-yandex-alice

Яндекс Алиса как платформа шлюза [Hermes Agent](https://github.com/NousResearch/hermes-agent):
плагин `kind: platform`, который принимает webhook приватного навыка, отдаёт реплику в обычную
сессию шлюза (профиль задаётся `gateway.profile_routes`), озвучивает короткий ответ с удержанием
сессии в пределах 4,5 с и присылает подробности в Telegram. Режим — диалог: после ответа сессия
Алисы остаётся открытой и следующая реплика продолжает ту же беседу.

Статус: **разведка**. Готово — `yandex-alice/protocol.py` (протокол Диалогов и разбор ответа, перенесён из
`old-bridge`) с тестами. Адаптера ещё нет; факты и открытые вопросы — `docs/RECON.md`.

## Структура

```
yandex-alice/      каталог плагина → ~/.hermes/plugins/yandex-alice
  plugin.yaml      манифест (черновик)
  protocol.py      чистая логика, без зависимостей от Hermes
tests/             unittest, Hermes не нужен
prototypes/s1/     проба S1 (финальность ответа) — запуск вручную против checkout Hermes
prototypes/s2/     проба S2 (профиль и profile_routes) — запуск вручную против checkout Hermes
docs/PLAN.md       план работ
docs/RECON.md      разведка
```

## Тесты

```
py -m unittest discover -s tests
```
