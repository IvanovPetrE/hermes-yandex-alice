# Развёртывание

Схема: Алиса → HTTPS-прокси на 443 → loopback-listener плагина (`127.0.0.1:<port>`, внутри процесса шлюза Hermes) →
сессия профиля. Плагин сам HTTPS не терминирует и наружу не слушает.

Все значения ниже — плейсхолдеры: `alice.example.org`, `<ALICE_PATH_SECRET>`, `<skill-id>`.

## Требования

- Hermes и Python из [таблицы совместимости](compatibility.md#закреплённая-ревизия).
- Домен с A-записью на сервер, доступный порт 443 снаружи (Яндекс ходит только на 443).
- `aiohttp` в окружении Hermes (есть в стандартной установке шлюза).

## 1. Плагин

```
mkdir -p ~/.hermes/plugins
cp -r yandex-alice ~/.hermes/plugins/yandex-alice          # или git archive HEAD yandex-alice | tar -x
ln -s ~/.hermes/plugins/yandex-alice ~/.hermes/profiles/<профиль>/plugins/yandex-alice   # раскладка A2
hermes plugins validate ~/.hermes/plugins/yandex-alice
```

Файлы копируйте от пользователя, под которым работает шлюз. Симлинк нужен при маршрутизации в другой профиль.
См. [раскладки плагина](configuration.md#раскладка-плагина).

## 2. Конфигурация

[Переменные и config.yaml](configuration.md). После правки `config.yaml` и до перезапуска:
`hermes -p <профиль> config check` (дубликат ключа делает YAML невалидным, и Hermes молча остаётся на старых
настройках). Затем:

```
systemctl --user restart hermes-gateway
journalctl --user -u hermes-gateway | grep yandex_alice     # или ~/.hermes/logs/gateway.log
```

Ожидаем `✓ yandex_alice connected`. Без `ALICE_PATH_SECRET` или `ALICE_SKILL_ID` адаптер не стартует и пишет причину.

## 3. Caddy в Docker

`compose.yaml` (только 443; сертификаты Let's Encrypt через TLS-ALPN-01, порт 80 не нужен):

```yaml
name: alice-caddy
services:
  caddy:
    image: caddy:2-alpine            # лучше закрепить digest
    network_mode: host               # loopback-listener плагина виден как 127.0.0.1 хоста
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 64m
    tmpfs: [/tmp]
    environment:
      ALICE_DOMAIN: ${ALICE_DOMAIN:?не задан ALICE_DOMAIN}
      ALICE_PATH_SECRET: ${ALICE_PATH_SECRET:?не задан ALICE_PATH_SECRET}
      ALICE_PORT: ${ALICE_PORT:-8789}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data             # сертификаты — не удалять без нужды (лимиты выпуска)
      - caddy_config:/config
volumes:
  caddy_data:
  caddy_config:
```

`network_mode: host` нужен потому, что listener слушает только 127.0.0.1 хоста; из обычной bridge-сети Docker он
недоступен.

`Caddyfile`:

```
{
	admin off
	auto_https disable_redirects
	# Ошибки (502/504) пишут request.uri, а там секретный путь: уводим их в отдельный лог с заменой.
	log default {
		exclude http.log.error
	}
	log errors {
		include http.log.error
		format filter {
			wrap json
			fields {
				request>uri replace <redacted>
			}
		}
	}
}

{$ALICE_DOMAIN} {
	tls {
		issuer acme {
			disable_http_challenge
		}
	}
	@alice path /alice/{$ALICE_PATH_SECRET}
	handle @alice {
		uri strip_prefix /alice
		reverse_proxy 127.0.0.1:{$ALICE_PORT:8789}
	}
	handle {
		respond 404
	}
}
```

Файл окружения (права 600, вне git), например `~/.config/alice-caddy/env`:

```
ALICE_DOMAIN=alice.example.org
ALICE_PATH_SECRET=<ALICE_PATH_SECRET>
```

Запуск: `docker compose --env-file ~/.config/alice-caddy/env up -d`. После правки Caddyfile (админ-API выключен,
горячей перезагрузки нет): `docker compose restart caddy`. Если Caddyfile смонтирован одним файлом, правьте его
записью в тот же inode (`cat > Caddyfile`), а не `sed -i`: иначе контейнер продолжит видеть старый файл.

## 4. Проверка

1. Снаружи: `curl -i https://alice.example.org/alice/<ALICE_PATH_SECRET>` с неверным методом/телом — ответ не 404 (404
   значит, что путь не совпал).
2. Запрос с чужим `skill_id` должен вернуть 200 с текстом «Этот навык работает только для владельца».
3. Живой запрос через приложение Алисы: ответ приходит, в логе шлюза нет ошибок адаптера.

Для первичной привязки нужен ID из запроса Яндекса. Особенности журналирования описаны в
[конфигурации](configuration.md#предупреждения).
