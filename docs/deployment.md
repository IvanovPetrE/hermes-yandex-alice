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

## 2а. Hermes в Docker

Вместо разделов 1–2, если шлюз Hermes работает в контейнере (проверено на образе `nousresearch/hermes-agent:v0.23.2`).
Каталог данных внутри контейнера — `/opt/data` (`HERMES_HOME`); шлюз работает от пользователя `hermes`. `<контейнер>` —
имя вашего контейнера со шлюзом.

```
# 1. Плагин в каталог данных; docker cp создаёт файлы от root, поэтому chown
docker exec -u hermes <контейнер> mkdir -p /opt/data/plugins
docker cp yandex-alice <контейнер>:/opt/data/plugins/
docker exec <контейнер> chown -R hermes:hermes /opt/data/plugins/yandex-alice
docker exec -u hermes <контейнер> hermes plugins validate /opt/data/plugins/yandex-alice

# 2. Включить плагин и платформу
docker exec -u hermes <контейнер> hermes plugins enable yandex-alice
docker exec -u hermes <контейнер> hermes config set platforms.yandex_alice.enabled true
docker exec -u hermes <контейнер> hermes config set platforms.yandex_alice.extra.port 8789

# 3. Переменные — в /opt/data/.env (имена и смысл — в configuration.md)
docker exec -u hermes <контейнер> sh -c 'printf "ALICE_PATH_SECRET=%s\nALICE_SKILL_ID=<skill-id>\n" "$(openssl rand -hex 32)" >> /opt/data/.env'

# 4. Перезапуск шлюза и проверка
docker exec -u hermes <контейнер> hermes gateway restart --detach -y
docker exec <контейнер> grep yandex_alice /opt/data/logs/gateway.log     # ждём «✓ yandex_alice connected»
```

- `docker cp` с Windows принимает только путь относительно текущего каталога (`cd` в папку с `yandex-alice`).
- Секрет генерируется внутри контейнера и в терминал не печатается; в историю shell его не вводите. Узнать его для
  прокси можно, прочитав `/opt/data/.env` на хосте; не выводите файл целиком.
- `config set` для `platforms.yandex_alice.*` на v0.23.2 прошёл без `--force`. Если ваша версия Hermes откажет
  («неизвестный ключ»), добавьте `--force`: ключ платформы плагина может отсутствовать в схеме.
- `hermes plugins enable` дописывает плагин в `plugins.enabled` в `config.yaml`; не удаляйте и не пересоздавайте этот файл
  после включения: плагин молча перестанет загружаться, а в логе будет «No messaging platforms enabled».
- Маршрут `gateway.profile_routes` (если реплики идут в другой профиль) — в [configuration.md](configuration.md#configyaml).
- Listener слушает только `127.0.0.1` контейнера. Внешний прокси увидит его лишь при `network_mode: host` у контейнера со
  шлюзом (или внутри того же сетевого пространства имён). Общая bridge-сеть не поможет.

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

### Локальная проверка без Яндекса

Запрос шлют прямо в listener. В Docker — изнутри контейнера (`docker exec`, в образе есть `curl`); без Docker —
с хоста. Значения `<skill-id>`, `<user-id>` и `session_id` ниже — плейсхолдеры; секрет берётся из `.env`, не печатается.

Тело запроса (файл `req.json`; `skill_id` подставьте нужный):

```json
{
  "request": {"original_utterance": "привет", "command": "привет", "type": "SimpleUtterance"},
  "version": "1.0",
  "session": {
    "session_id": "test-session", "message_id": 1, "skill_id": "<skill-id>", "new": true,
    "user": {"user_id": "<user-id>"}
  }
}
```

```
docker exec <контейнер> sh -c 'set -a; . /opt/data/.env; set +a;
  curl -s -w "\nHTTP %{http_code}\n" --data-binary @/tmp/req.json http://127.0.0.1:8789/$ALICE_PATH_SECRET'
```

(перед этим `docker cp req.json <контейнер>:/tmp/req.json`).

| Запрос | Ответ |
|---|---|
| `skill_id` чужой | HTTP 200, «Этот навык работает только для владельца.», `end_session: true` |
| `skill_id` верный, `ALICE_OWNER_USER_ID` пуст (режим привязки) | HTTP 200, «Навык ещё не привязан к владельцу.», `end_session: true`; в логе шлюза однажды печатается `user_id` запроса |
| любой, путь не равен `ALICE_PATH_SECRET` | HTTP 404 без тела |
| реплика `ping` | HTTP 200, `pong` |

**Не проверяйте телом с `user_id` владельца:** при заданном `ALICE_OWNER_USER_ID` такой запрос проходит в боевую сессию
Hermes — тратит токены, пишет в историю беседы и может отправить сообщение в Telegram. Для проверки оставьте
`ALICE_OWNER_USER_ID` пустым или используйте чужой `skill_id`.

Для первичной привязки нужен ID из запроса Яндекса. Особенности журналирования описаны в
[конфигурации](configuration.md#предупреждения).
