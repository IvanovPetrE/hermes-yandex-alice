"""Адаптер платформы «Яндекс Алиса» (docs/PLAN.md, этап 3, срез «б», б3) — тонкая обёртка над Hermes и aiohttp.

Вся логика — в чистых модулях: `entry` (проверки входа), `engine` (исполнение действий `dialog.step`), `sends` (правило
итога S1), `history`, `cron_guard`. Здесь только то, что требует Hermes или aiohttp: loopback-listener, события хода,
чтение истории (`SessionDB`) и хук cron. Импорты из Hermes — только из `public-api.md`; `os.environ` не читается.

Модель по пробам: S5 (жизненный цикл, занятый порт, события хода), S6 (хук `pre_tool_call`, `validate_config`), S7 (чтение
истории в рабочем потоке), S3 (Telegram: свой `PlatformConfig` с токеном бота своего профиля, отправка в копии контекста
`connect()`, `notify=True`; очередь, исходы и повтор после обрыва — `delivery`, этап 5). Состояние беседы и отправки до
исхода — в SQLite плагина (`storage`) и переживают переподключение и перезапуск шлюза; хранилище недоступно — в памяти.
В логах нет секретного пути, токена, тела запроса и текста реплик; единственный ID — user_id в режиме привязки.
"""
import asyncio
import contextlib
import contextvars
import logging
import threading
import uuid
from pathlib import Path

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session_context import get_session_env

from . import cron_guard, delivery, dialog, engine, entry, history, protocol, sends, storage

PLATFORM = "yandex_alice"
PLUGIN_NAME = "yandex-alice"   # каталог данных плагина: plugin-data/<имя>/
TICK_SECONDS = 60.0     # период проверки сроков (Д13, Д9)
READ_TIMEOUT = 5.0      # с на чтение тела запроса: защита от медленной отправки

logger = logging.getLogger(__name__)


def _secret(name: str) -> str:
    try:
        return (get_scoped_secret(name, "") or "").strip()
    except Exception:  # noqa: BLE001 - значение в области не читается: считаем, что его нет
        return ""


def _telegram_sender():
    """Отправитель Telegram из реестра — в рабочем потоке: первый `get()` в процессе синхронно грузит плагин Telegram
    (~0,9 с, S3, вывод 6). Сбой — None (Telegram выключен), в лог — только класс."""
    try:
        from gateway.platform_registry import platform_registry

        return getattr(platform_registry.get("telegram"), "standalone_sender_fn", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram sender lookup failed: %s", type(exc).__name__)
        return None


def _port(extra: dict):
    try:
        port = int(extra.get("port"))
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


class AliceAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False
    interactive_resume = False   # автопродолжение Hermes должно вернуть ответ, а не спросить «что дальше» (S5, вывод 5)

    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        self._extra = dict(getattr(config, "extra", None) or {})
        self._cfg = None
        self._owner_id = ""
        self._engine = self._store = self._courier = None
        self._tg = None                 # (PlatformConfig, чат, отправитель) или None — Telegram выключен
        self._ctx = None                # снимок контекста connect(): прокси и режим уведомлений своего профиля (S3, T6)
        self._tg_warned = False
        self._runner = self._site = self._tick = None
        self._open = {}                 # chat_id -> (message_id открытого хода, внутренний ли он)
        self._sends = 0
        self._db_path = None
        self._read_lock = threading.Lock()
        self._unbound = entry.OnceNote(logger.warning)

    # --- жизненный цикл --------------------------------------------------------------------------------------------

    def _fail(self, code: str, text: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, text, retryable=retryable)   # без пути, значений и исключения
        return False

    async def connect(self, *, is_reconnect=False):
        secret, skill = _secret("ALICE_PATH_SECRET"), _secret("ALICE_SKILL_ID")
        if not secret or not skill:
            return self._fail("config_missing", "ALICE_PATH_SECRET and ALICE_SKILL_ID are required", retryable=False)
        port = _port(self._extra)
        if port is None:
            return self._fail("config_invalid", "platforms.yandex_alice.extra.port is not a valid port", retryable=False)
        self._owner_id = _secret("ALICE_OWNER_USER_ID")
        self._cfg = entry.EntryConfig(secret=secret, skill_id=skill, owner_user_id=self._owner_id)
        if self._courier is not None:   # повторный connect() без disconnect(): прежняя очередь — обрыв, не исход
            courier, self._courier = self._courier, None
            await courier.close()
        self._close_store()   # повторный connect() без disconnect(): прежнее соединение не должно остаться открытым
        self._store, loaded = await asyncio.to_thread(self._open_store)
        await self._telegram_setup()
        telegram = self._tg is not None
        self._courier = delivery.Courier(self._tg_send if telegram else None, self._store, self._delivered)
        # Движок — до listener'а: запрос, пришедший сразу после bind, не должен упереться в None.
        self._engine = engine.Engine(dialog.Settings(telegram=telegram), self._ask, self._recall,
                                     new_id=lambda: "alice-" + uuid.uuid4().hex[:12], state=loaded,
                                     save=self._store.save if self._store is not None else None,
                                     telegram=self._courier.enqueue if telegram else None)

        from aiohttp import web

        runner = None
        try:
            app = web.Application()
            app.router.add_post("/{tail:.*}", self._http)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", port)   # только loopback
            await site.start()
        except BaseException as exc:   # любой сбой запуска (и отмена) не оставляет движок и соединение с базой
            if runner is not None:
                with contextlib.suppress(Exception):
                    await runner.cleanup()
            self._engine = self._courier = None   # работник очереди ещё не запускался
            self._close_store()
            if isinstance(exc, OSError):
                return self._fail("bind_failed", "could not bind the loopback port", retryable=True)
            raise
        self._runner, self._site = runner, site

        try:   # часть цепочки истории, не зависящая от беседы: профиль и путь к state.db — один раз
            self._db_path = await asyncio.to_thread(self._resolve_db_path)
        except Exception as exc:  # noqa: BLE001 - тогда каждое чтение определит путь само
            logger.warning("history path not resolved: %s", type(exc).__name__)
            self._db_path = None
        # Каждый connect(), и переподключение тоже: состояние из базы могло застать удержание или ход, итог которого
        # этот экземпляр не узнает, — удержание сбрасывается, вопрос в работе идёт по Д9 (решение владельца, этап 4).
        self._engine.on_gateway_started()
        # Отправки без исхода от прошлого экземпляра: оборванная — один повтор, залежалая или без Telegram — uncertain.
        self._courier.resume(enabled=telegram)
        self._tick = asyncio.ensure_future(self._tick_loop())
        self._running = True
        self._mark_connected()
        return True

    async def disconnect(self):
        self._running = False
        self._mark_disconnected()
        if self._tick is not None:
            self._tick.cancel()
            await asyncio.gather(self._tick, return_exceptions=True)   # отмена самого disconnect() не глотается
        if self._site is not None:
            with contextlib.suppress(Exception):
                await self._site.stop()
        if self._courier is not None:   # идущая отправка обрывается: запись остаётся, следующий connect() повторит её
            courier, self._courier = self._courier, None
            await courier.close()
        if self._engine is not None:   # ожидающие запросы получают ответ до runner.cleanup, иначе он ждёт обработчики
            engine_, self._engine = self._engine, None
            await engine_.close()
        self._close_store()   # после close(): закрытый движок уже не пишет
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
        self._tick = self._site = self._runner = None

    @staticmethod
    def _open_store():
        """`(хранилище, загруженное состояние)` в рабочем потоке. `plugin_db` кладёт базу в `<дом Hermes>/plugin-data/`
        (дом — по области `connect()`). Любой сбой — `(None, None)`: беседа идёт в памяти (решение владельца, этап 4)."""
        conn = None
        try:
            from plugins.plugin_storage import plugin_db

            conn = plugin_db(PLUGIN_NAME, "state.db")
            store = storage.Store(conn)
            loaded = store.load()
            return store, loaded
        except Exception as exc:  # noqa: BLE001 - только класс: в базе тексты беседы
            logger.warning("state storage unavailable: %s", type(exc).__name__)
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            return None, None

    # --- Telegram (S3, этап 5) -------------------------------------------------------------------------------------

    async def _telegram_setup(self) -> None:
        """Свой `PlatformConfig` в области `connect()`: токен бота своего профиля (пустой не допускается — отправитель
        взял бы токен области вызова, S3 T1–T4), `extra` — белый список `telegram_*`. Чат не задан — Telegram выключен
        молча; чат есть, а токена или отправителя нет — одно предупреждение без значений, платформа работает."""
        self._tg = self._ctx = None
        chat = _secret("ALICE_TELEGRAM_CHAT")
        if not chat:
            return
        token = _secret("TELEGRAM_BOT_TOKEN")
        if not token:
            return self._telegram_off("no bot token")
        sender = await asyncio.to_thread(_telegram_sender)
        if sender is None:
            return self._telegram_off("no telegram sender")
        from gateway.config import PlatformConfig

        self._tg = (PlatformConfig(enabled=True, token=token, extra=delivery.telegram_extra(self._extra)), chat, sender)
        self._ctx = contextvars.copy_context()

    def _telegram_off(self, reason: str) -> None:
        if not self._tg_warned:
            self._tg_warned = True
            logger.warning("telegram disabled: %s (ALICE_TELEGRAM_CHAT is set)", reason)

    async def _tg_send(self, text: str):
        """Одна отправка — задачей в копии контекста `connect()`, а не области хода (иначе прокси и режим уведомлений
        чужого профиля, S3 T5/T6); общий срок и исход — `delivery.Courier`."""
        pconfig, chat, sender = self._tg
        task = asyncio.get_running_loop().create_task(sender(pconfig, chat, text, notify=True), context=self._ctx.copy())
        return await task

    def _delivered(self, row, outcome: str) -> None:
        if self._engine is not None:
            self._engine.on_delivery(row.conversation, outcome, row.text, row.fallback)

    def _close_store(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self._engine.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tick failed: %s", type(exc).__name__)

    # --- вход от Алисы ---------------------------------------------------------------------------------------------

    async def _read_body(self, request):
        chunks, total = [], 0
        async for chunk in request.content.iter_chunked(4096):
            total += len(chunk)
            if total > protocol.MAX_BODY:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _json(body: bytes):
        from aiohttp import web

        return web.Response(body=body, content_type="application/json")

    async def _http(self, request):
        from aiohttp import web

        try:
            if not entry.check_path(request.match_info["tail"], self._cfg):   # до чтения тела
                return web.Response(status=404)
            try:
                body = await asyncio.wait_for(self._read_body(request), READ_TIMEOUT)
            except asyncio.TimeoutError:
                return web.Response(status=408)
            if body is None:
                return web.Response(status=413)
            out = entry.screen(body, self._cfg, self._unbound)
            if isinstance(out, entry.Reject):
                return web.Response(status=out.status)
            if isinstance(out, entry.Reply):
                return self._json(out.body)
            return self._json(protocol.alice_reply(await self._engine.handle(out.request)))
        except Exception as exc:  # noqa: BLE001 - Алиса не должна показывать пользователю ошибку навыка
            logger.warning("http failed: %s", type(exc).__name__)
            return self._json(protocol.alice_reply(protocol.REPLY_ERROR))

    # --- ход Hermes ------------------------------------------------------------------------------------------------

    async def _ask(self, conversation: str, text: str) -> None:
        source = self.build_source(chat_id=conversation, chat_name="Alice", chat_type="dm",
                                   user_id=self._owner_id, user_name="owner")
        # id хода уникален между экземплярами адаптера: счётчик с единицы после переподключения совпал бы со старым
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                                               message_id="alice-" + uuid.uuid4().hex[:12],
                                               allow_gateway_control=False))

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        meta = dict(metadata or {})
        chat, text = str(chat_id), content or ""
        turn = self._open.get(chat)
        kind = sends.classify_send(open_turn=turn is not None, turn_mid=turn[0] if turn else None,
                                   reply_to=None if reply_to is None else str(reply_to), notify=meta.get("notify"),
                                   interim=bool(meta.get("_interim_send")), text=text)
        if self._engine is not None:
            if kind == sends.FINAL:
                self._engine.on_final(chat, text, turn[1])
            elif kind == sends.RECOVERED:   # ответ из леджера Hermes после перезапуска: пометка срезается по форме (S5)
                self._engine.on_final(chat, sends.strip_marker(text), True)
            else:   # вне хода (cron в голос, фон) — в Telegram «Алиса:»; служебное и неитоговое внутри хода — нет (S6)
                forwarded = sends.forward_text(open_turn=turn is not None, kind=kind, text=text)
                if forwarded:
                    self._engine.forward(chat, forwarded)
        logger.debug("send: kind=%s length=%d", kind, len(text))
        self._sends += 1
        return SendResult(success=True, message_id=f"out-{self._sends}")

    async def on_processing_start(self, event):
        chat, internal = str(event.source.chat_id), bool(getattr(event, "internal", False))
        self._open[chat] = (event.message_id, internal)
        if self._engine is not None:
            self._engine.on_turn_start(chat, event.message_id, internal)

    async def on_processing_complete(self, event, outcome):
        chat, internal = str(event.source.chat_id), bool(getattr(event, "internal", False))
        current = self._open.get(chat)
        if current is not None and current[0] != event.message_id:   # запоздалое завершение прошлого хода
            return
        self._open.pop(chat, None)
        if self._engine is not None:
            self._engine.on_turn_complete(chat, event.message_id, internal)

    # --- история Hermes («повтори», S7) ----------------------------------------------------------------------------

    def _resolve_db_path(self) -> Path:
        """Путь к state.db целевого профиля: маршрут `yandex_alice` без различителей → дом профиля. `profiles/<имя>` —
        допущение, выведенное из `profile_name_for_home`; оно проверяется, а не принимается на веру (S7)."""
        from gateway.config import load_gateway_config
        from hermes_constants import get_default_hermes_root, profile_name_for_home

        profile = history.pick_profile(load_gateway_config().profile_routes, PLATFORM)
        root = Path(get_default_hermes_root())
        if profile in ("", "default"):
            return root / "state.db"
        home = root / "profiles" / profile
        if profile_name_for_home(home) != profile:
            raise LookupError("profile_home")
        return home / "state.db"

    async def _recall(self, conversation: str):
        return await asyncio.to_thread(self._read_history, conversation)

    def _read_history(self, conversation: str):
        """`(исход, текст)` в рабочем потоке: одно чтение за раз; ручка `SessionDB` закрывается в `finally`."""
        if not self._read_lock.acquire(blocking=False):
            return dialog.RECALL_ERROR, None
        handle = None
        try:
            if self._db_path is None:
                self._db_path = self._resolve_db_path()
            if not self._db_path.exists():
                return dialog.RECALL_ERROR, None
            from hermes_state import SessionDB

            handle = SessionDB(self._db_path, read_only=True)
            sid = handle.find_session_by_origin(platform=PLATFORM, chat_id=str(conversation))
            if sid is None:
                return dialog.RECALL_MISSING, None
            text, _ = history.read_tail(handle, handle.resolve_resume_session_id(sid))
            return dialog.RECALL_FOUND, text
        except Exception as exc:  # noqa: BLE001 - только класс; текст истории в логи не пишем
            logger.warning("history read failed: %s", type(exc).__name__)
            return dialog.RECALL_ERROR, None
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
            self._read_lock.release()

    # --- то, что платформе не нужно --------------------------------------------------------------------------------

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        return SendResult(success=False, error="Not supported")

    async def delete_message(self, chat_id, message_id):
        return False

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"name": f"alice-{chat_id}", "type": "dm", "chat_id": chat_id}


def validate_config(config) -> bool:
    """Секрет должен быть в ТЕКУЩЕЙ области: профиль без него платформу не включает (S2). Чат Telegram, который нельзя
    подставить в `telegram:<чат>`, тоже отвергается — иначе хук молча не подменил бы адрес cron (решено 2026-11-17)."""
    if not _secret("ALICE_PATH_SECRET"):
        return False
    chat = _secret("ALICE_TELEGRAM_CHAT")
    if chat and cron_guard.valid_chat(chat) is None:
        logger.warning("ALICE_TELEGRAM_CHAT has no valid form (no spaces, commas or colons): platform disabled")
        return False
    return True


def cron_hook(tool_name=None, args=None, task_id=None, **kwargs):
    """`pre_tool_call`: задача cron из голосовой сессии не уходит в голосовой канал (S6). Не бросает — бросающий хук
    заблокировал бы все инструменты профиля; чужие инструменты и не-голосовые сессии пропускает сразу."""
    try:
        if tool_name != cron_guard.TOOL or get_session_env("HERMES_SESSION_PLATFORM", "") != PLATFORM:
            return None
        merged = cron_guard.guard_args(args, _secret("ALICE_TELEGRAM_CHAT") or None)
        return {"action": "modify", "args": merged} if merged else None
    except Exception:  # noqa: BLE001
        return None


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM,
        label="Yandex Alice",
        adapter_factory=AliceAdapter,
        check_fn=lambda: True,
        validate_config=validate_config,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=True,
        platform_hint=protocol.PLATFORM_HINT,
        display_tier="minimal",
    )
    ctx.register_hook("pre_tool_call", cron_hook)
