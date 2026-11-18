"""Машина состояний «Модели беседы» (docs/RECON.md, М1–М6; docs/PLAN.md, этап 2, срез «б») — без Hermes.

step(state, event, settings) -> (state, actions): чистая функция. Часы — в событии, состояние хранит адаптер/хранилище
(этап 4), глобального состояния модуля нет (S5). Действия исполняет адаптер (этап 3).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

try:
    from . import protocol as p
except ImportError:  # тесты импортируют модули плагина как модули верхнего уровня
    import protocol as p  # type: ignore[no-redef]

IDLE, WAIT, NEW_OR_OLD, READY, TG_FAILED, CONTINUE = "idle", "wait", "new_or_old", "ready", "tg_failed", "continue"


@dataclass(frozen=True)
class Settings:
    continue_hours: float = 2.0     # М2/Д6: беседа продолжается, если с последней реплики владельца меньше N часов
    lost_after: float = 600.0       # Д9: с после старта шлюза без итога и без внутреннего хода — «вопрос потерялся»
    turn_lost_after: float = 900.0  # HERMESYAND-3: с без признаков жизни хода (нет complete, итога, отправок) — потерян
    unclaimed_ttl: float = 86400.0  # Д13: срок незабранного ответа
    rate_per_minute: int = 6        # Д16: ходов агента в минуту
    telegram: bool = True           # доставка в Telegram настроена


@dataclass(frozen=True)
class Question:
    """Вопрос в работе текущей беседы (один на беседу — Р2)."""
    asked_at: float
    approval: bool = False             # ход ждёт подтверждения в Telegram (S4)
    to_telegram: bool = False          # владелец попросил «пришли в Телеграм» — итог уходит туда
    restarted_at: float | None = None  # шлюз стартовал, пока вопрос был в работе (Д9)
    internal_seen: bool = False        # после старта Hermes дозапустил внутренний ход (S5)
    alive_at: float | None = None      # последний признак жизни хода (старт, отправка, конец ожидания); None — хода нет


@dataclass(frozen=True)
class Answer:
    """Незабранный ответ (М3): озвучивается по частям, подробности — в Telegram при озвучке."""
    parts: tuple[str, ...]
    telegram: str          # подробности с подписью; "" — нет
    whole: str             # весь ответ с подписью — для «пришли в Телеграм», «новый» и Д13
    ready_at: float
    undelivered: bool = False   # не дошёл до Telegram (Д12): предложить «Озвучить?», повторно сам не отправляется
    offered: bool = False       # «Не смогла отправить… Озвучить?» уже прозвучало


@dataclass(frozen=True)
class State:
    conversation: str                  # chat_id беседы Hermes, выдаёт плагин
    question: Question | None = None
    answer: Answer | None = None
    holding: str = ""                # "" | "ask" | "wait" — запрос Алисы ждёт итог; "recall" — ждёт Recalled
    prompt: str = IDLE                 # последний вопрос Алисы, на который отвечает следующая реплика
    wait_round: int = 0                # петля Д8: какую из трёх фраз сказать
    held: str = ""                     # фраза, на которой Алиса спросила «новый или прошлый?» (М1)
    orphans: tuple[str, ...] = ()      # прошлые беседы с вопросом в работе: их итог — в Telegram («новый», М5)
    last_owner_at: float | None = None  # последняя реплика владельца (М2/Д6)
    reading: tuple[str, ...] = ()      # части длинного ответа, читаемого по «Продолжить?»
    reading_pos: int = 0               # какая часть прозвучала последней
    last_said: str = ""                # последняя реплика Алисы — для «повтори» вне обычной беседы
    announce: str = ""                 # сказать на ближайшей реплике вместо её разбора (О8, О9, Д9)
    notice: str = ""                   # предупредить один раз перед ближайшей репликой (Д12, uncertain)
    asks: tuple[float, ...] = ()       # времена ходов агента за последнюю минуту (лимит Д16)
    last_turn: str = ""                # turn_id (session_id-message_id) последней реплики — дедупликация ретрая
    last_reply: str = ""               # что ей ответили; "" — ответ ещё готовится (удержание)
    answered: bool = False             # в текущей беседе был итог: пустая история при «повтори» — сбой, а не «нечего» (S7)


# --- события -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Utterance:
    now: float
    turn_id: str
    text: str
    intents: tuple[str, ...] = ()
    launch: bool = False
    fresh_id: str = ""


@dataclass(frozen=True)
class Expired:
    """Бюджет ожидания итога (~3,5 с) для удержанного запроса Алисы вышел."""
    now: float


@dataclass(frozen=True)
class Approval:
    """Ход беседы ждёт (или перестал ждать) нажатия карточки подтверждения в Telegram (S4)."""
    now: float
    conversation: str
    waiting: bool


@dataclass(frozen=True)
class Final:
    now: float
    conversation: str
    text: str
    internal: bool = False
    card_failed: bool = False     # карточка подтверждения хода не дошла до Telegram (S4)
    approvals_off: bool = False   # подтверждения недоступны заранее: карточки не было (этап 5, срез «б»)


@dataclass(frozen=True)
class Recovered:
    """Отправка вне хода с формой пометки «♻️ …:» (HERMESYAND-4). Восстановленным ответом она считается только при
    вопросе в работе и перезапуске шлюза после него; иначе это обычная отправка вне хода (cron, фон) — в Telegram."""
    now: float
    conversation: str
    text: str   # без пометки
    raw: str    # как пришло, с пометкой


@dataclass(frozen=True)
class Delivery:
    """Исход отправки Telegram (S3): delivered / failed / uncertain; text и fallback — из действия Telegram."""
    now: float
    conversation: str
    outcome: str
    text: str = ""
    fallback: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tick:
    """Периодическая проверка сроков адаптером, без реплики владельца (Д13, Д9)."""
    now: float


@dataclass(frozen=True)
class GatewayStarted:
    """connect() адаптера — первый в процессе шлюза и переподключение (S5, L4; этап 4): ход агента мог потеряться."""
    now: float


@dataclass(frozen=True)
class InternalStarted:
    """Hermes дозапустил внутренний ход беседы после перезапуска (S5): вопрос не потерян."""
    now: float
    conversation: str


@dataclass(frozen=True)
class TurnAlive:
    """Признак жизни хода беседы — его старт или отправка шлюза в нём (HERMESYAND-3): срок потерянного хода — заново."""
    now: float
    conversation: str


RECALL_FOUND, RECALL_MISSING, RECALL_ERROR = "found", "missing", "error"   # исход чтения истории (S7)


@dataclass(frozen=True)
class Recalled:
    """Последний ответ беседы из истории Hermes по RecallLast. outcome: found — прочитали (text None — ответа с
    текстом нет); missing — сессии беседы в истории нет; error — чтение не удалось (база занята, срок, исключение)."""
    now: float
    text: str | None
    outcome: str = RECALL_FOUND


@dataclass(frozen=True)
class TurnClosed:
    """Ход владельца закрыт (on_processing_complete) без итога: исключение или пустой ход (S1, S5 R6).
    Для внутреннего хода адаптер его не шлёт — пустой первый внутренний ход вопрос не закрывает (S5, вывод 6)."""
    now: float
    conversation: str


# --- действия ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Say:
    text: str


@dataclass(frozen=True)
class Ask:
    conversation: str
    text: str


@dataclass(frozen=True)
class Hold:
    pass


@dataclass(frozen=True)
class Telegram:
    """Отправить в Telegram (текст уже с подписью). fallback — части голоса для «Озвучить?», если доставка failed (Д12)."""
    conversation: str
    text: str
    fallback: tuple[str, ...] = ()


@dataclass(frozen=True)
class DropTurn:
    """Ход беседы потерян (HERMESYAND-3): снять записи об открытом ходе и освободить гейт Р2."""
    conversation: str


@dataclass(frozen=True)
class RecallLast:
    """«Повтори» в обычной беседе: адаптер берёт последний ответ из истории Hermes и шлёт Recalled (этап 3)."""
    conversation: str


def step(state: State, event: object, settings: Settings = Settings()) -> tuple[State, tuple[object, ...]]:
    st, acts = _step(state, event, settings)
    if not settings.telegram:   # Telegram не настроен — отправок нет (П-OFF); тексты голоса это уже учли
        acts = tuple(a for a in acts if not isinstance(a, Telegram))
    return st, acts


def _clock(st: State, now: float, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """Правила по времени на каждом событии: Д13 — незабранный ответ старше срока уходит в Telegram и удаляется
    (не доставленный однажды — только удаляется, повторной отправки нет); Д9 — «вопрос потерялся»."""
    acts: tuple[object, ...] = ()
    ans = st.answer
    if ans and now - ans.ready_at >= settings.unclaimed_ttl:
        acts = () if ans.undelivered else (Telegram(st.conversation, ans.whole),)
        st = replace(st, answer=None)
    q = st.question
    if q and q.restarted_at is not None and not q.internal_seen and now - q.restarted_at >= settings.lost_after:
        st = replace(st, question=None, announce=p.REPLY_LOST, wait_round=0)
    # Срок идёт только у начатого хода (`alive_at` ставит его старт): вопрос без хода гейт Р2 не держит. После старта
    # шлюза правит Д9, на ожидании подтверждения (Д8) отсчёт стоит.
    lost = bool(q and q.alive_at is not None and q.restarted_at is None and not q.approval
                and now - q.alive_at >= settings.turn_lost_after)
    if lost:   # HERMESYAND-3: тихо, без озвучки; фраза, на которой это случилось, идёт агенту обычным порядком
        st = replace(st, question=None, wait_round=0)
        acts += (DropTurn(st.conversation), Telegram(st.conversation, p.telegram_text(p.REPLY_TURN_LOST)))
        if st.holding == "wait":   # запрос Алисы ждёт итог, которого не будет — как закрытый ход при удержании
            st, said = _say(st, p.REPLY_FAILED, holding="")
            acts += said
    if st.answer is None and (st.prompt in (READY, TG_FAILED)
                              or (st.prompt in (WAIT, NEW_OR_OLD) and st.question is None)):
        st = replace(st, prompt=IDLE, held="")
    if (st.question is None and not lost and st.answer is None and not st.holding and st.last_owner_at is not None
            and now - st.last_owner_at >= settings.unclaimed_ttl):
        # тексты — персональные данные: после суток тишины владельца не храним (этап 4, решение владельца)
        st = replace(st, last_said="", last_reply="", last_turn="", reading=(), reading_pos=0, held="", prompt=IDLE,
                     announce="", notice="")
    return st, acts


def _step(state: State, event: object, settings: Settings) -> tuple[State, tuple[object, ...]]:
    state, timed = _clock(state, getattr(event, "now"), settings)
    state, acts = _dispatch(state, event, settings)
    return state, timed + acts


def _dispatch(state: State, event: object, settings: Settings) -> tuple[State, tuple[object, ...]]:
    if isinstance(event, GatewayStarted):
        q = state.question and replace(state.question, restarted_at=event.now, internal_seen=False)
        return replace(state, question=q, holding=""), ()
    if isinstance(event, InternalStarted):
        if event.conversation == state.conversation and state.question:
            return replace(state, question=replace(state.question, internal_seen=True)), ()
        return state, ()
    if isinstance(event, TurnAlive):
        if event.conversation == state.conversation and state.question:
            return replace(state, question=replace(state.question, alive_at=event.now)), ()
        return state, ()
    if isinstance(event, Utterance):
        return _utterance(state, event, settings)
    if isinstance(event, Recovered):
        q = state.question
        if event.conversation == state.conversation and q and q.restarted_at is not None:
            return _dispatch(state, Final(now=event.now, conversation=event.conversation, text=event.text,
                                          internal=True), settings)
        return state, (Telegram(event.conversation, p.telegram_text(event.raw)),)
    if isinstance(event, Final):
        if event.conversation == state.conversation:
            state = replace(state, answered=True)
        return _final(state, event, settings)
    if isinstance(event, Expired):
        return _expired(state, event, settings)
    if isinstance(event, TurnClosed):
        return _turn_closed(state, event, settings)
    if isinstance(event, Recalled):
        return _recalled(state, event)
    if isinstance(event, Delivery):
        return _delivery(state, event)
    if isinstance(event, Approval):
        if event.conversation == state.conversation and state.question:
            q = replace(state.question, approval=event.waiting)
            if not event.waiting and q.alive_at is not None:   # отсчёт стоял на ожидании — с нажатия заново
                q = replace(q, alive_at=event.now)
            return replace(state, question=q), ()
        return state, ()
    return state, ()


def _expired(st: State, ev: Expired, settings: Settings) -> tuple[State, tuple[object, ...]]:
    if st.announce:
        return _say(st, st.announce, announce="", holding="", prompt=IDLE)
    if st.holding == "recall":   # чтение истории не уложилось в бюджет: вопроса агенту нет, ждать нечего (S7, Р-S7-2)
        return _say(st, p.REPLY_FAILED, holding="", prompt=IDLE)
    if st.holding == "ask":
        return _say(st, p.REPLY_ACCEPTED, holding="", prompt=WAIT)
    if st.question and st.question.approval:
        return _say(st, p.REPLY_WAIT_APPROVAL, holding="", prompt=WAIT)
    text = p.WAIT_ROUNDS[st.wait_round % len(p.WAIT_ROUNDS)]
    return _say(st, text, holding="", prompt=WAIT, wait_round=st.wait_round + 1)


def _ask(st: State, text: str, now: float, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """Ход агенту — только в беседу без вопроса в работе (Р2); лимит считает только ходы агента (Д16)."""
    asks = tuple(t for t in st.asks if now - t < 60)
    if len(asks) >= settings.rate_per_minute:
        return _say(st, p.REPLY_RATE, asks=asks)
    return (replace(st, question=Question(asked_at=now), holding="ask", wait_round=0, asks=asks + (now,)),
            (Ask(st.conversation, text), Hold()))


def _hold(st: State) -> tuple[State, tuple[object, ...]]:
    return replace(st, holding="wait"), (Hold(),)


# Несколько интентов в одной реплике: свои hya.* главнее YANDEX.* (О6).
_PRIORITY = (p.INTENT_DIALOG_NEW, p.INTENT_TELEGRAM, p.INTENT_ANSWER_GET, p.INTENT_DIALOG_CONTINUE, p.INTENT_NEXT,
             p.INTENT_LATER, p.INTENT_WAIT, p.YANDEX_HELP, p.YANDEX_REPEAT, p.YANDEX_CONFIRM, p.YANDEX_REJECT)
NONE, EMPTY, REJECTED = "none", "empty", "rejected"   # реплика без интента / пустая / отклонённая (Д16)


def kind_of(text: str, intents: tuple[str, ...]) -> str:
    """Что сказал владелец: интент с наибольшим приоритетом, иначе NONE / EMPTY / REJECTED."""
    for name in _PRIORITY:
        if name in intents:
            return name
    if not text:
        return EMPTY
    return REJECTED if p.is_rejected_command(text) else NONE


def _say(st: State, text: str, **changes: object) -> tuple[State, tuple[object, ...]]:
    said = f"{st.notice} {text}" if st.notice else text
    return replace(st, last_said=text, last_reply=said, notice="", **changes), (Say(said),)


def _delivery(st: State, ev: Delivery) -> tuple[State, tuple[object, ...]]:
    """Д12: failed — ответ становится незабранным («Озвучить?»), если есть что озвучить и своего нет;
    uncertain — один раз предупредить. Повторной отправки нет."""
    if ev.outcome == "uncertain":
        return replace(st, notice=p.REPLY_TG_UNCERTAIN), ()
    if ev.outcome == "failed" and ev.fallback and st.answer is None:
        ans = Answer(parts=ev.fallback, telegram="", whole=ev.text, ready_at=ev.now, undelivered=True)
        return replace(st, answer=ans), ()
    return st, ()


def _offer_undelivered(st: State, ev: Utterance, k: str) -> tuple[State, tuple[object, ...]] | None:
    """Первая реплика после сбоя доставки — «Не смогла отправить в Телеграм. Озвучить?»; фраза держится (М1)."""
    ans = st.answer
    if (ans is None or not ans.undelivered or ans.offered
            or k in (p.INTENT_ANSWER_GET, p.INTENT_DIALOG_CONTINUE, p.INTENT_DIALOG_NEW, p.INTENT_TELEGRAM)):
        return None
    return _say(st, p.REPLY_TG_FAILED, prompt=TG_FAILED, held=ev.text if k == NONE else "",
                answer=replace(ans, offered=True))


_OFFERS = {READY: p.REPLY_READY, TG_FAILED: p.REPLY_TG_FAILED, CONTINUE: p.REPLY_CONTINUE}


def _help(st: State) -> tuple[State, tuple[object, ...]]:
    """Подсказка по последнему вопросу Алисы (П-HELP); вопрос остаётся в силе."""
    if st.prompt == WAIT:
        return _say(st, p.REPLY_WAIT_HELP)
    if st.prompt == NEW_OR_OLD:
        return _say(st, p.REPLY_SAY_NEW_OR_OLD)
    if st.prompt in _OFFERS:
        return _say(st, f"{p.REPLY_HELP} {_OFFERS[st.prompt]}")
    return _say(st, p.REPLY_HELP)


def _recalled(st: State, ev: Recalled) -> tuple[State, tuple[object, ...]]:
    """Повтор из истории — голосом целиком, без отправки в Telegram; ошибка шлюза — «не получилось»."""
    if st.holding != "recall":   # поздний ответ: срок вышел, беседа сменилась или шлюз перезапустился
        return st, ()
    st = replace(st, holding="")
    if ev.outcome == RECALL_ERROR:
        return _say(st, p.REPLY_FAILED)
    parts = tuple(p.spoken(ev.text)) if ev.text else ()
    if not parts:   # текста нет: итог в беседе был — прочитать не удалось (Р-S7-3), не было — повторять нечего
        return _say(st, p.REPLY_FAILED if st.answered else p.REPLY_NOTHING_TO_REPEAT)
    return _part(st, parts, 0)


def _part(st: State, parts: tuple[str, ...], pos: int, **changes: object) -> tuple[State, tuple[object, ...]]:
    """Озвучить часть pos; если есть ещё — «Продолжить?» (предела числа частей нет)."""
    if pos + 1 < len(parts):
        return _say(st, f"{parts[pos]} {p.REPLY_CONTINUE}", prompt=CONTINUE, reading=parts, reading_pos=pos, **changes)
    return _say(st, parts[pos], prompt=IDLE, reading=(), reading_pos=0, **changes)


def _voice(st: State, ans: Answer) -> tuple[State, tuple[object, ...]]:
    """Озвучить ответ; подробности («ТЕЛЕГРАМ:», ссылка, текст ошибки) — в Telegram при озвучке."""
    st, acts = _part(st, ans.parts, 0, answer=None)
    return st, ((Telegram(st.conversation, ans.telegram),) if ans.telegram else ()) + acts


def _rest(st: State) -> tuple[str, ...]:
    return st.reading[st.reading_pos + 1:]


def _row_continue(st: State, ev: Utterance, k: str, settings: Settings) -> tuple[State, tuple[object, ...]] | None:
    """«Продолжить?» (Д2): «да»/«дальше» — следующая часть; «нет» — остаток в Telegram (исключение из Р3);
    «повтори» — текущая часть (О1); реплика без интента — агенту (None: разбор как в обычной беседе)."""
    if k in _SAY_YES:
        return _part(st, st.reading, st.reading_pos + 1)
    if k in _SAY_NO or k == p.INTENT_TELEGRAM:
        rest = _rest(st)
        st = replace(st, prompt=IDLE, reading=(), reading_pos=0)
        if not settings.telegram:
            return _say(st, p.REPLY_OK)
        st, acts = _say(st, p.REPLY_REST_IN_TELEGRAM)
        return st, (Telegram(st.conversation, p.telegram_text(" ".join(rest)), rest),) + acts
    if k == p.YANDEX_REPEAT:
        return _part(st, st.reading, st.reading_pos)
    if k == p.YANDEX_HELP:
        return _help(st)
    if k == REJECTED:
        return _say(st, p.REPLY_REJECTED)
    if k == EMPTY:
        return _say(st, p.REPLY_CONTINUE)
    return None


def _fork(st: State, text: str) -> tuple[State, tuple[object, ...]]:
    """Шаг 4: другая фраза, пока ответ не забран — фраза держится до выбора (М1)."""
    return _say(st, p.REPLY_NEW_OR_OLD, prompt=NEW_OR_OLD, held=text)


def _old(st: State) -> tuple[State, tuple[object, ...]]:
    """«Прошлый» / «скинь ответ»: готово — озвучить, нет — ждать (шаг 3); удержанная фраза отбрасывается (М1)."""
    st = replace(st, held="", prompt=IDLE)
    if st.answer:
        return _voice(st, st.answer)
    return _hold(st)


def _new_dialog(st: State, ev: Utterance, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """«Новый диалог»: незабранный ответ — в Telegram, вопрос в работе — сирота (итог в Telegram), новая беседа."""
    sent: list[object] = []
    orphans = st.orphans
    if st.answer and not st.answer.undelivered:   # не доставленный однажды — не повторяем (Д12), он в истории Hermes
        sent.append(Telegram(st.conversation, st.answer.whole, st.answer.parts))
    if st.prompt == CONTINUE and _rest(st):   # остаток чтения по частям — тоже в Telegram (П-CONT)
        sent.append(Telegram(st.conversation, p.telegram_text(" ".join(_rest(st))), _rest(st)))
    if st.question:
        orphans = orphans + (st.conversation,)
    to_telegram = (bool(sent) or st.question is not None) and settings.telegram
    held = st.held
    st = replace(st, conversation=ev.fresh_id, question=None, answer=None, holding="", prompt=IDLE, held="",
                 wait_round=0, orphans=orphans, reading=(), reading_pos=0, answered=False)
    if held:   # М1: удержанная фраза — первый вопрос новой беседы; про Telegram — перед ближайшим ответом
        if to_telegram:
            st = replace(st, notice=f"{st.notice} {p.REPLY_IN_TELEGRAM}".strip())
        st, acts = _ask(st, held, ev.now, settings)
    else:      # П-NEW
        st, acts = _say(st, f"{p.REPLY_IN_TELEGRAM} {p.REPLY_HELLO}" if to_telegram else p.REPLY_HELLO)
    return st, tuple(sent) + acts


def _launch(st: State, ev: Utterance, settings: Settings) -> tuple[State, State | None, tuple[object, ...]]:
    """Новая сессия Алисы: развилка не переживает запуск; беседа — по М2 (только без вопроса и без ответа, И4).
    Пустой запуск отвечает сам (второй элемент — None), запуск с вопросом идёт дальше обычной репликой."""
    st = replace(st, prompt=IDLE, held="", holding="", reading=(), reading_pos=0)
    fresh = (st.question is None and st.answer is None
             and (st.last_owner_at is None or ev.now - st.last_owner_at >= settings.continue_hours * 3600))
    if fresh:
        st = replace(st, conversation=ev.fresh_id, answered=False)
    if ev.text or ev.intents:
        return st, st, ()
    st = replace(st, last_owner_at=ev.now)
    offer = _offer_undelivered(st, ev, EMPTY)
    if offer:
        st, acts = offer
    elif st.answer:
        st, acts = _say(st, p.REPLY_READY, prompt=READY)
    elif st.question:
        st, acts = _say(st, p.REPLY_NOT_READY, prompt=WAIT, wait_round=1)
    else:
        st, acts = _say(st, p.REPLY_HELLO if fresh else p.REPLY_HELLO_CONTINUE)
    return st, None, acts


def _utterance(st: State, ev: Utterance, settings: Settings) -> tuple[State, tuple[object, ...]]:
    if ev.turn_id and ev.turn_id == st.last_turn:   # ретрай Яндекса: прежний ответ, а пока он готовится — ждать
        if st.last_reply:
            return st, (Say(st.last_reply),)
        return st, ((RecallLast(st.conversation),) if st.holding == "recall" else (Hold(),))
    st = replace(st, last_turn=ev.turn_id, last_reply="")
    if st.announce:   # фраза отбрасывается: владелец сам повторит (П-LOST, О8, О9)
        return _say(st, st.announce, announce="", prompt=IDLE, held="", reading=(), reading_pos=0,
                    last_owner_at=ev.now)
    if ev.launch:
        st, cont, acts = _launch(st, ev, settings)
        if cont is None:
            return st, acts
    st = replace(st, last_owner_at=ev.now)
    k = kind_of(ev.text, ev.intents)
    if k == p.INTENT_DIALOG_NEW:
        return _new_dialog(st, ev, settings)
    if st.prompt in (IDLE, WAIT):
        offer = _offer_undelivered(st, ev, k)
        if offer:
            return offer
    if st.prompt == CONTINUE:
        done = _row_continue(st, ev, k, settings)
        if done is not None:
            return done
        st = replace(st, prompt=IDLE, reading=(), reading_pos=0)   # остаток есть в истории Hermes (П-CONT)
    if st.prompt == NEW_OR_OLD:
        return _row_fork(st, k)
    if k == p.YANDEX_HELP:
        return _help(st)
    if k == p.YANDEX_REPEAT:
        if st.prompt == IDLE and st.answer is None and st.question is None:   # из истории Hermes (адаптер)
            return replace(st, holding="recall"), (RecallLast(st.conversation),)
        return _say(st, st.last_said or p.REPLY_HELLO)
    if st.prompt == WAIT:
        return _row_wait(st, ev, k, settings)
    if k == REJECTED:   # Д16; вопрос Алисы остаётся прежним (П-REJ)
        said, acts = _say(st, p.REPLY_REJECTED)
        return replace(said, last_said=st.last_said), acts
    if k == EMPTY:      # П-EMPTY: повторить последнюю реплику Алисы
        busy = st.answer or st.question or st.prompt != IDLE
        return _say(st, (st.last_said if busy else "") or p.REPLY_HELLO)
    if st.prompt in (READY, TG_FAILED):
        return _row_offer(st, ev, k, settings)
    if st.answer:
        return _row_pending_answer(st, ev, k, settings)
    if st.question:
        return _row_pending_question(st, ev, k, settings)
    if k == p.INTENT_ANSWER_GET:
        return _say(st, p.REPLY_NO_ANSWERS)   # М6
    return _ask(st, ev.text, ev.now, settings)


_SAY_YES = (p.YANDEX_CONFIRM, p.INTENT_WAIT, p.INTENT_ANSWER_GET, p.INTENT_DIALOG_CONTINUE, p.INTENT_NEXT)
_SAY_NO = (p.YANDEX_REJECT, p.INTENT_LATER)


def _send_answer(st: State, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """«Пришли в Телеграм» при готовом ответе — отправить сейчас (без Telegram — как «позже», П-OFF)."""
    ans = st.answer
    if ans is None or not settings.telegram:
        return _say(st, p.REPLY_ANSWER_WAITS, prompt=IDLE)
    st, acts = _say(st, p.REPLY_IN_TELEGRAM, answer=None, prompt=IDLE)
    return st, (Telegram(st.conversation, ans.whole, ans.parts),) + acts


def _send_later(st: State, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """«Пришли в Телеграм» при вопросе в работе — итог уйдёт туда (без Telegram — как «позже», П-OFF)."""
    if st.question is None or not settings.telegram:
        return _say(st, p.REPLY_LATER, prompt=IDLE)
    return _say(st, p.REPLY_IN_TELEGRAM, prompt=IDLE, question=replace(st.question, to_telegram=True))


def _row_fork(st: State, k: str) -> tuple[State, tuple[object, ...]]:
    if k in (p.INTENT_DIALOG_CONTINUE, p.INTENT_ANSWER_GET):
        return _old(st)
    if k in (p.YANDEX_CONFIRM, p.YANDEX_REJECT, p.INTENT_TELEGRAM, p.YANDEX_HELP):
        return _say(st, p.REPLY_SAY_NEW_OR_OLD)
    return _say(st, p.REPLY_NEW_OR_OLD)


def _row_wait(st: State, ev: Utterance, k: str, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """«Подождёте?»: всё, кроме «нет/позже» и команд, — ждать (М5); ответ уже готов — озвучить (О7)."""
    if k == p.INTENT_TELEGRAM:
        return _send_answer(st, settings) if st.answer else _send_later(st, settings)
    if k in _SAY_NO:
        return _say(st, p.REPLY_ANSWER_WAITS if st.answer else p.REPLY_LATER, prompt=IDLE)
    if st.answer:
        return _voice(st, st.answer)
    return _hold(st)


def _row_offer(st: State, ev: Utterance, k: str, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """«Ответ на прошлый вопрос готов. Скинуть?» (М3, Д2) и «Озвучить?» (Д12, О3)."""
    if k in _SAY_YES:
        return _old(st)
    if k in _SAY_NO:
        return _say(st, p.REPLY_ANSWER_WAITS, prompt=IDLE, held="")
    if k == p.INTENT_TELEGRAM:
        return _send_answer(st, settings)
    return _fork(st, ev.text)


def _row_pending_answer(st: State, ev: Utterance, k: str, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """Ответ готов, Алиса ничего не спрашивает: «да»/«дальше» — озвучить, «нет» — подождёт, фраза — шаг 4."""
    if k in _SAY_YES:
        return _old(st)
    if k in _SAY_NO:
        return _say(st, p.REPLY_ANSWER_WAITS, prompt=IDLE)
    if k == p.INTENT_TELEGRAM:
        return _send_answer(st, settings)
    return _fork(st, ev.text)


def _row_pending_question(st: State, ev: Utterance, k: str, settings: Settings) -> tuple[State, tuple[object, ...]]:
    """Вопрос в работе после «спросите позже»/«пришлю в Телеграм»: «да» — ждать, «нет» — позже (П-IDLEQ)."""
    if k in (p.YANDEX_CONFIRM, p.INTENT_WAIT, p.INTENT_ANSWER_GET, p.INTENT_DIALOG_CONTINUE):
        return _old(st)
    if k in _SAY_NO:
        return _say(st, p.REPLY_LATER, prompt=IDLE)
    if k == p.INTENT_TELEGRAM:
        return _send_later(st, settings)
    return _fork(st, ev.text)


def _drop_orphan(st: State, conversation: str) -> State:
    return replace(st, orphans=tuple(c for c in st.orphans if c != conversation))


def _turn_closed(st: State, ev: TurnClosed, settings: Settings) -> tuple[State, tuple[object, ...]]:
    failed = p.telegram_text(p.REPLY_FAILED)
    if ev.conversation != st.conversation:
        return _drop_orphan(st, ev.conversation), (Telegram(ev.conversation, failed),)
    if st.question is None:
        return st, ()
    sent = (Telegram(st.conversation, failed),) if st.question.to_telegram else ()
    st = replace(st, question=None, wait_round=0, prompt=IDLE if st.prompt == WAIT else st.prompt)
    if st.holding:
        st, acts = _say(st, p.REPLY_FAILED, holding="")
        return st, sent + acts
    return replace(st, announce=p.REPLY_FAILED), sent   # О8: на ближайшей реплике


def _final(st: State, ev: Final, settings: Settings) -> tuple[State, tuple[object, ...]]:
    if ev.conversation != st.conversation:   # беседа-сирота или итог старой беседы: голосом его не ждут
        return _drop_orphan(st, ev.conversation), (Telegram(ev.conversation, p.whole_text(ev.text)),)
    if st.question is None:   # итог без вопроса в работе (run, фон, делегирование; C4) — голосом не звучит
        return st, (Telegram(st.conversation, p.whole_text(ev.text)),)
    whole = p.whole_text(ev.text)
    card = ev.card_failed or ev.approvals_off
    if card:   # карточка не дошла (S4) или подтверждения не настроены: своя озвучка, текст агента — в Telegram
        said = p.REPLY_APPROVALS_OFF if ev.approvals_off else p.REPLY_CARD_FAILED
        ans = Answer(parts=(said,), telegram=whole, whole=whole, ready_at=ev.now)
    elif not settings.telegram:   # Telegram нет — весь ответ голосом, ничего не обещаем (П-OFF)
        ans = Answer(parts=tuple(p.spoken(ev.text)) or (p.REPLY_FAILED,), telegram="", whole=whole, ready_at=ev.now)
    else:
        reply = p.voice_reply(ev.text)
        ans = Answer(parts=tuple(reply.parts), telegram=reply.telegram, whole=whole, ready_at=ev.now)
    question, st = st.question, replace(st, question=None)
    if question.to_telegram:   # обещано в Telegram; дождались голосом — озвучить тоже (О4)
        sent: tuple[object, ...] = (Telegram(st.conversation, ans.whole, ans.parts),)
        if st.holding:
            st, acts = _voice(replace(st, holding=""), replace(ans, telegram=""))
            return st, sent + acts
        return replace(st, prompt=IDLE if st.prompt == WAIT else st.prompt), sent
    if st.holding:
        return _voice(replace(st, holding=""), ans)
    if card or p.is_gateway_error(ev.text):   # не «ответ готов»: сказать один раз, текст уже в Telegram (О9)
        return (replace(st, announce=ans.parts[0], prompt=IDLE if st.prompt == WAIT else st.prompt),
                (Telegram(st.conversation, ans.telegram),))
    if ans.telegram:   # подробности не ждут озвучивания (живой прогон 2026-11-18: модель не уложилась в бюджет, «скинь в тг»
        # не сработало, пока владелец не вернулся): уходят сразу, а при озвучивании не повторяются
        return replace(st, answer=replace(ans, telegram="")), (Telegram(st.conversation, ans.telegram),)
    return replace(st, answer=ans), ()
