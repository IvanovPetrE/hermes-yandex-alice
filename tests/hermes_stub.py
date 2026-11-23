"""Заглушки модулей Hermes для тестов адаптера (этап 6, срез «а») — без checkout Hermes.

`install()` кладёт в `sys.modules` ровно то, что `adapter.py` импортирует из Hermes (сигнатуры сверены с
`BasePlatformAdapter` и `MessageEvent` Hermes v0.23.1), поэтому подмена безусловна: на машине с настоящим Hermes тесты тоже
идут против заглушек. Изменяемое окружение заглушек — объект `world`: тест задаёт «область секретов», дом, отправитель
Telegram и сбой базы; `world.reset()` — в `setUp`. Файл не `test*` — `discover` его не собирает.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import sqlite3
import sys
import tempfile
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "yandex-alice"
PLUGIN_MODULE = "hya_plugin"


class World:
    """Окружение заглушек; атрибуты читают сами заглушки, тест их переопределяет."""

    def __init__(self) -> None:
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.reset()

    def reset(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.scope: dict[str, str] = {}           # имя секрета -> значение в «текущей области»
        self.session_env: dict[str, str] = {}
        self.db_error: BaseException | None = None   # plugin_db бросает его
        self.sender = None                        # standalone_sender_fn Telegram или None
        self.profile_routes: list = []
        self.on_message = None                    # async (adapter, event) — «шлюз»
        self.store = None                         # хранилище для open_persistence_reader (FakeStore) или None
        self.reader_targets: list = []            # с чем открывали open_persistence_reader

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


world = World()


# --- gateway.* ----------------------------------------------------------------------------------------------------------

class Platform:
    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other) -> bool:
        return isinstance(other, Platform) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)


@dataclass
class PlatformConfig:
    enabled: bool = False
    token: str | None = None
    extra: dict = field(default_factory=dict)


class MessageType(Enum):
    TEXT = "text"


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    source: object = None
    message_id: str | None = None
    internal: bool = False
    allow_gateway_control: bool = True


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = False


class BasePlatformAdapter:
    """Только то, что берёт адаптер Алисы: поля и методы `base.py` v0.23.1 (`:1851`, `:2025–2028`, `:2058–2092`, `:4701`)."""

    gateway_runner = None   # `base.py:1827`; шлюз ставит свой GatewayRunner, тесты — заглушку с `_draining`

    def __init__(self, config, platform) -> None:
        self.config, self.platform = config, platform
        self._running = False
        self._fatal_error_code = self._fatal_error_message = None
        self._fatal_error_retryable = True

    has_fatal_error = property(lambda self: self._fatal_error_message is not None)
    fatal_error_message = property(lambda self: self._fatal_error_message)
    fatal_error_code = property(lambda self: self._fatal_error_code)
    fatal_error_retryable = property(lambda self: self._fatal_error_retryable)

    def is_connected(self) -> bool:
        return self._running

    def _mark_connected(self, *, listener_base=None) -> None:
        self._running = True
        self._fatal_error_code = self._fatal_error_message = None
        self._fatal_error_retryable = True

    def _mark_disconnected(self) -> None:
        self._running = False

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_code, self._fatal_error_message = code, message
        self._fatal_error_retryable = retryable

    def build_source(self, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None, **_kw):
        return SimpleNamespace(platform=self.platform, chat_id=str(chat_id), chat_name=chat_name, chat_type=chat_type,
                               user_id=None if user_id in (None, "") else str(user_id), user_name=user_name)

    async def handle_message(self, event) -> None:
        if world.on_message is not None:
            await world.on_message(self, event)


def _get_scoped_secret(name, default=None, *, external_fallback=False):
    return world.scope.get(name, default)


class _Registry:
    def get(self, name):
        return SimpleNamespace(standalone_sender_fn=world.sender) if name == "telegram" else None


def _plugin_db(name, filename="data.db"):
    if world.db_error is not None:
        raise world.db_error
    folder = world.home / "plugin-data" / name
    folder.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(folder / filename, check_same_thread=False)


# --- persistence_assembly / persistence (хранилище сессий Hermes v0.23.3) ------------------------------------------------

@dataclass(frozen=True)
class TranscriptRead:
    """Подмножество `persistence.conversation.messages.TranscriptRead`: страница расшифровки."""
    limit: int | None = None
    offset: int = 0
    latest: bool = False


@contextlib.contextmanager
def _open_persistence_reader(target, **kw):
    world.reader_targets.append(target)
    if world.store is None:
        raise FileNotFoundError("no store")
    yield world.store


class FakeRow:
    def __init__(self, row: dict) -> None:
        self._row = row

    def as_wire_row(self) -> dict:
        return dict(self._row)


class FakeStore:
    """`store.routing.session_for_origin`, `store.lineage.resolve_resume_target`, `store.messages.read_transcript`.
    `origins` — {(platform, chat_id): session_id}, `tips` — {session_id: tip}, `rows` — {session_id: [dict, …]} (старые
    первыми); `reads` — запросы `read_transcript`."""

    def __init__(self, *, origins=None, tips=None, rows=None) -> None:
        self.origins, self.tips, self.rows = dict(origins or {}), dict(tips or {}), dict(rows or {})
        self.reads: list = []
        store = self

        class Routing:
            def session_for_origin(self, *, platform, chat_id, thread_id=None, user_id=None):
                return store.origins.get((platform, chat_id))

        class Lineage:
            def resolve_resume_target(self, session_id):
                return store.tips.get(session_id, session_id)

        class Messages:
            def read_transcript(self, session_id, request=TranscriptRead()):
                store.reads.append((session_id, request))
                rows = store.rows.get(session_id, [])
                if request.latest:
                    end = len(rows) - request.offset
                    start = max(0, end - request.limit) if request.limit is not None else 0
                    page = rows[start:max(end, 0)]
                else:
                    page = rows[request.offset:][:request.limit]
                return tuple(FakeRow(r) for r in page)

        self.routing, self.lineage, self.messages = Routing(), Lineage(), Messages()


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    if "." in name:   # родитель должен быть пакетом
        parent = sys.modules.setdefault(name.rpartition(".")[0], types.ModuleType(name.rpartition(".")[0]))
        parent.__path__ = getattr(parent, "__path__", [])
        setattr(parent, name.rpartition(".")[2], mod)
    sys.modules[name] = mod
    return mod


def install() -> None:
    _module("gateway")
    _module("gateway.config", Platform=Platform, PlatformConfig=PlatformConfig,
            load_gateway_config=lambda: SimpleNamespace(profile_routes=world.profile_routes))
    _module("gateway.platforms")
    _module("gateway.platforms._shared", get_scoped_secret=_get_scoped_secret)
    _module("gateway.platforms.base", BasePlatformAdapter=BasePlatformAdapter, SendResult=SendResult)
    _module("gateway.platforms.event", MessageEvent=MessageEvent, MessageType=MessageType)
    _module("gateway.session_context", get_session_env=lambda name, default="": world.session_env.get(name, default))
    _module("gateway.platform_registry", platform_registry=_Registry())
    _module("hermes_constants", get_hermes_home=lambda: world.home, get_default_hermes_root=lambda: world.home,
            profile_name_for_home=lambda home: Path(home).name)
    _module("plugins")
    _module("plugins.plugin_storage", plugin_db=_plugin_db)
    _module("tools")
    _module("tools.approval", resolve_gateway_approval=lambda *a, **k: 0, resolve_gateway_approval_card=lambda *a, **k: 0)
    _module("hermes_state")
    _module("persistence_assembly", open_persistence_reader=_open_persistence_reader)
    _module("persistence")
    _module("persistence.conversation")
    _module("persistence.conversation.messages", TranscriptRead=TranscriptRead)


def load_plugin() -> types.ModuleType:
    """Каталог плагина как пакет `hya_plugin` (как грузит Hermes): модули верхнего уровня остальных тестов не затронуты.
    Возвращает `hya_plugin.adapter`."""
    install()
    if PLUGIN_MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(PLUGIN_MODULE, PLUGIN_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PLUGIN_MODULE] = module
        spec.loader.exec_module(module)
    return sys.modules[PLUGIN_MODULE + ".adapter"]


class FakeCtx:
    """Контекст `register(ctx)`: запоминает регистрации."""

    def __init__(self) -> None:
        self.platform: dict = {}
        self.hooks: dict = {}
        self.handlers: dict = {}

    def register_platform(self, **kw) -> None:
        self.platform = kw

    def register_hook(self, name, fn) -> None:
        self.hooks[name] = fn

    def register_platform_handler(self, name, fn) -> None:
        self.handlers[name] = fn


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def eventually(check, timeout: float = 3.0) -> bool:
    """Ждёт условие циклом событий (без `time.sleep`); True, если оно наступило до срока."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if check():
            return True
        await asyncio.sleep(0.01)
    return bool(check())
