"""Исполнитель машины беседы (docs/PLAN.md, этап 3, срез «б», б2) — без Hermes и без aiohttp.

`Engine` держит `State` и исполняет действия `dialog.step`: `Say` — ответ Алисе, `Ask` — фраза шлюзу (`ask`), `Hold` — запрос
ждёт итог не дольше бюджета, `RecallLast` — одно чтение истории Hermes (`recall`) под сроком. События Hermes (итог, начало и
конец хода, старт шлюза, тик) вызывает адаптер. Состояние — атрибут экземпляра, не модуля (S5). Часы: `wall` — для `now`
в событиях `step` (настенные), бюджет и срок чтения идут по часам цикла событий asyncio (монотонные).
Telegram (этап 5): действие `Telegram` и внеходовая отправка (`forward`) ставятся в очередь доставки (`telegram` —
`delivery.Courier.enqueue`) после записи снимка; исход приходит в `on_delivery`. Без Telegram (`Settings.telegram=False`)
`step` действий `Telegram` не выдаёт, `forward` ничего не делает.
Хранилище (этап 4): начальное `state` — из `storage.Store.load()`, `save` — после каждого шага, изменившего состояние,
до исполнения действий. Запись синхронная, в цикле событий (одна строка SQLite) — принятый предел. Загруженный вопрос в
работе повторно не отправляется: `Ask` даёт только реплика владельца.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

try:
    from . import approvals as ap
    from . import dialog as d
    from . import protocol as p
except ImportError:  # тесты импортируют модули плагина как модули верхнего уровня
    import approvals as ap  # type: ignore[no-redef]
    import dialog as d  # type: ignore[no-redef]
    import protocol as p  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: d.Settings, ask: Callable[[str, str], Awaitable[None]],
                 recall: Callable[[str], Awaitable[tuple[str, str | None]]], new_id: Callable[[], str],
                 wall: Callable[[], float] = time.time, budget: float = 3.5, recall_timeout: float = 1.5,
                 gate_timeout: float = 3.0, log: Callable[[str], None] | None = None, state: d.State | None = None,
                 save: Callable[[d.State], None] | None = None,
                 telegram: Callable[[str, str, tuple[str, ...]], None] | None = None):
        self.settings, self._ask, self._recall, self._new_id = settings, ask, recall, new_id
        self._wall, self._budget, self._recall_timeout, self._gate_timeout = wall, budget, recall_timeout, gate_timeout
        self._log = log or logger.warning
        self._save, self._save_failed = save, False
        self._telegram = telegram
        self.state = state if state is not None else d.State(conversation=new_id())
        self._waiters: list[asyncio.Future[str]] = []
        self._tasks: set[asyncio.Task] = set()
        self._open: dict[str, tuple[str | None, asyncio.Event]] = {}   # беседа -> (message_id хода, сигнал «ход закрыт»)
        self._closed = False
        self._reading: asyncio.Future | None = None  # идущее чтение истории
        self._finals: set[str] = set()               # беседы, где в текущем ходе уже был итог

    # --- запросы Алисы -------------------------------------------------------------------------------------------

    async def handle(self, request: p.AliceRequest) -> str:
        """Текст ответа Алисе на реплику владельца (уже прошедшую `entry.screen`). Не бросает: непредвиденная ошибка —
        вежливый отказ, в лог — только класс исключения."""
        try:
            event = d.Utterance(now=self._wall(), turn_id=request.turn_id, text=request.text,
                                intents=p.intents(request.nlu)[1], launch=request.new, fresh_id=self._new_id())
            say, hold = self._apply(event)
            if say is not None:
                return say
            if not hold:
                return p.REPLY_ERROR
            return await self._wait()
        except Exception as exc:  # noqa: BLE001
            self._log(f"handle failed: {type(exc).__name__}")
            return p.REPLY_ERROR

    async def _wait(self) -> str:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await asyncio.wait({fut}, timeout=self._budget)
            if not fut.done():
                say, _ = self._apply(d.Expired(now=self._wall()))
                return say if say is not None else p.REPLY_ERROR
            return fut.result()
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    async def close(self) -> None:
        """`disconnect()` адаптера: ожидающие запросы получают отказ, фоновые задачи (Ask, чтение) отменяются —
        после переподключения (новый экземпляр) от них ничего не должно остаться."""
        self._closed = True
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(p.REPLY_ERROR)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # --- события Hermes ------------------------------------------------------------------------------------------

    def on_final(self, conversation: str, text: str, internal: bool, card: str = "") -> None:
        """`card` — метка хода из `approvals.Approvals.outcome`: `failed` (карточка не дошла), `off` (подтверждения
        не настроены) или ""."""
        self._finals.add(conversation)
        self._apply(d.Final(now=self._wall(), conversation=conversation, text=text, internal=internal,
                            card_failed=card == ap.FAILED, approvals_off=card == ap.OFF))

    def on_approval(self, conversation: str, waiting: bool) -> None:
        """Ход беседы ждёт нажатия карточки в Telegram (или перестал ждать) — Д8 «Жду подтверждения…»."""
        self._apply(d.Approval(now=self._wall(), conversation=conversation, waiting=waiting))

    def on_turn_start(self, conversation: str, mid: str | None, internal: bool) -> None:
        self._open[conversation] = (mid, asyncio.Event())
        self._finals.discard(conversation)
        if internal:   # Hermes дозапустил ход после перезапуска (S5): вопрос не потерян
            self._apply(d.InternalStarted(now=self._wall(), conversation=conversation))

    def on_turn_complete(self, conversation: str, mid: str | None, internal: bool) -> None:
        turn = self._open.get(conversation)
        if turn is not None and turn[0] != mid:   # запоздалое завершение прошлого хода: новый ход идёт, гейт не трогаем
            return
        self._open.pop(conversation, None)
        if turn is not None:
            turn[1].set()
        had_final = conversation in self._finals
        self._finals.discard(conversation)
        if not internal and not had_final:   # пустой внутренний ход вопрос не закрывает (S5, вывод 6)
            self._apply(d.TurnClosed(now=self._wall(), conversation=conversation))

    def on_gateway_started(self) -> None:
        """Каждый `connect()` адаптера, и переподключение тоже (этап 4): ход агента мог потеряться (S5)."""
        self._apply(d.GatewayStarted(now=self._wall()))

    def on_delivery(self, conversation: str, outcome: str, text: str, fallback: tuple[str, ...]) -> None:
        """Исход отправки в Telegram (`delivery.Courier`): Д12 — failed → «Озвучить?», uncertain — предупредить раз."""
        self._apply(d.Delivery(now=self._wall(), conversation=conversation, outcome=outcome, text=text,
                               fallback=tuple(fallback)))

    def forward(self, conversation: str, text: str) -> None:
        """Отправка шлюза вне хода (S6) — в Telegram как есть (текст уже с подписью), без «Озвучить?»."""
        if not self._closed and self.settings.telegram:
            self._queue(conversation, text, ())

    def tick(self) -> None:
        """Сроки без реплики владельца: Д13 (незабранный ответ), Д9 («вопрос потерялся»)."""
        self._apply(d.Tick(now=self._wall()))

    # --- исполнение действий -------------------------------------------------------------------------------------

    def _apply(self, event: object) -> tuple[str | None, bool]:
        """Шаг машины и исполнение действий; `(текст ответа Алисе или None, нужно ли ждать итог)`.
        После `close()` события игнорируются: поздний итог или тик не должен породить задач у отключённого адаптера."""
        if self._closed:
            return None, False
        before = self.state
        self.state, acts = d.step(before, event, self.settings)
        if self._save is not None and self.state != before:   # до действий: Ask уходит, когда вопрос уже записан
            self._store(self.state)
        say, hold = None, False
        for act in acts:
            if isinstance(act, d.Say):
                say = act.text
                for fut in self._waiters:
                    if not fut.done():
                        fut.set_result(act.text)
            elif isinstance(act, d.Hold):
                hold = True
            elif isinstance(act, d.Ask):
                self._spawn(self._do_ask(act.conversation, act.text))
            elif isinstance(act, d.Telegram):
                self._queue(act.conversation, act.text, act.fallback)
            elif isinstance(act, d.RecallLast):
                hold = True   # ответит Recalled; ретрай во время чтения ждёт то же чтение
                if self._reading is None or self._reading.done():
                    self._reading = asyncio.ensure_future(self._do_recall(act.conversation))
                    self._tasks.add(self._reading)
                    self._reading.add_done_callback(self._tasks.discard)
        return say, hold

    def _queue(self, conversation: str, text: str, fallback: tuple[str, ...]) -> None:
        """В очередь доставки; её сбой беседу не ломает — в лог только класс ошибки (текст в лог не пишем)."""
        if self._telegram is None:
            return
        try:
            self._telegram(conversation, text, fallback)
        except Exception as exc:  # noqa: BLE001
            self._log(f"telegram enqueue failed: {type(exc).__name__}")

    def _store(self, state: d.State) -> None:
        """Сбой хранилища беседу не ломает: она идёт в памяти (решение владельца, этап 4); в лог — класс ошибки,
        один раз за экземпляр (текст исключения может нести текст ответа)."""
        try:
            self._save(state)
        except Exception as exc:  # noqa: BLE001
            if not self._save_failed:
                self._save_failed = True
                self._log(f"state save failed: {type(exc).__name__}")

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _do_recall(self, conversation: str) -> None:
        """Чтение истории под сроком (S7, Р-S7-2): срок вышел или чтение упало — исход `error`, поздний результат
        отбрасывается. Одно чтение за раз: пока оно идёт, ретраи Яндекса новых не запускают."""
        outcome, text = d.RECALL_ERROR, None
        try:
            outcome, text = await asyncio.wait_for(self._recall(conversation), self._recall_timeout)
        except Exception as exc:  # noqa: BLE001 - включая TimeoutError; текст истории в лог не пишем
            self._log(f"recall failed: {type(exc).__name__}")
        self._apply(d.Recalled(now=self._wall(), text=text, outcome=outcome))

    async def _do_ask(self, conversation: str, text: str) -> None:
        """Гейт Р2: фраза уходит шлюзу, только когда в беседе нет открытого хода Hermes. Не вышло — вопрос закрывается
        (`step` уже записал его в состояние), ответ владельцу — «не получилось»; в лог — класс ошибки, без текста."""
        try:
            turn = self._open.get(conversation)
            if turn is not None:
                await asyncio.wait_for(turn[1].wait(), self._gate_timeout)
            await self._ask(conversation, text)
        except Exception as exc:  # noqa: BLE001 - включая TimeoutError гейта; ни одна ошибка не должна оставить вопрос в работе
            self._log(f"ask failed: {type(exc).__name__}")
            self._apply(d.TurnClosed(now=self._wall(), conversation=conversation))
