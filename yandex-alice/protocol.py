"""Протокол Яндекс Диалогов, интенты и разбор итога хода агента — без зависимостей от Hermes.

Сессия Алисы после ответа не закрывается: беседа продолжается (docs/RECON.md, «Модель беседы»).
Команды — только интенты (М5). Ответ агента не сжимается: голос режется на реплики по предложениям
(«Продолжить?» и позиция чтения — dialog.py), в Telegram — только по запросу (Р3) и ответ со ссылкой.
Модуль импортируется и тестируется без checkout Hermes.
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple

# Дословно из «Модели беседы» (М1–М6) и решений по S1/S4 — docs/RECON.md.
REPLY_ACCEPTED = "Вопрос принят. Спросите ответ позже или готовы подождать?"
REPLY_LATER = "Хорошо, спросите ответ позже."
REPLY_NOT_READY = "Ещё не готово. Подождёте?"
REPLY_NEW_OR_OLD = "Начать новый диалог или продолжить прошлый?"
REPLY_READY = "Ответ на прошлый вопрос готов. Скинуть?"
REPLY_NO_ANSWERS = "Ответов нет, задайте вопрос."
REPLY_FAILED = "Не получилось, попробуйте ещё раз."
REPLY_LOST = "Вопрос потерялся, повторите."
REPLY_TURN_LOST = "Прошлый ответ не пришёл."   # только в Telegram: ход без complete и без итога закрыт по сроку
REPLY_CARD_FAILED = "Запрос на подтверждение не дошёл до Телеграма, команда не выполнена."
# Подтверждения недоступны заранее: Telegram не настроен или чат — группа без владельца (этап 5, решение 2026-11-17).
REPLY_APPROVALS_OFF = "Эту команду нужно подтвердить в Телеграме, а подтверждения не настроены. Команда не выполнена."
REPLY_CONTINUE = "Продолжить?"
# Остальные — в стиле bridge.
REPLY_HELLO = "Слушаю."
REPLY_WAIT_HELP = "Я готовлю ответ. Можно подождать, спросить ответ позже или сказать „пришли в Телеграм“. Подождёте?"
REPLY_IN_TELEGRAM = "Ответ пришлю в Телеграм."   # исход доставки ещё неизвестен — будущее время
REPLY_ERROR = "Сейчас не могу ответить, повторите позже."
REPLY_NOT_OWNER = "Этот навык работает только для владельца."
REPLY_UNBOUND = "Навык ещё не привязан к владельцу."
REPLY_REJECTED = "Такую команду я не передам."
REPLY_RATE = "Слишком много команд подряд, подождите минуту."
# Решения владельца 2026-11-17 (этап 2, срез «б», docs/PLAN.md).
REPLY_SAY_NEW_OR_OLD = "Скажите „новый“ или „прошлый“."
REPLY_ANSWER_WAITS = "Хорошо, ответ подождёт."
REPLY_REST_IN_TELEGRAM = "Хорошо, остальное пришлю в Телеграм."
REPLY_OK = "Хорошо."
REPLY_HELLO_CONTINUE = "Слушаю. Продолжаем прошлый разговор."
REPLY_STILL_WORKING = "Всё ещё работаю. Подождёте?"
REPLY_NOT_YET = "Пока нет. Ждём дальше?"
WAIT_ROUNDS = (REPLY_NOT_READY, REPLY_STILL_WORKING, REPLY_NOT_YET)   # петля Д8 по кругу
REPLY_WAIT_APPROVAL = "Жду подтверждения в Телеграме. Подождёте?"
REPLY_TG_FAILED = "Не смогла отправить в Телеграм. Озвучить?"
REPLY_TG_UNCERTAIN = "Возможно, ответ не дошёл до Телеграма."
REPLY_HELP = ("Задайте вопрос, я передам его агенту. Пока ответ готовится, можно подождать, спросить позже или сказать "
              "„пришли в Телеграм“. „Новый диалог“ начинает разговор заново.")
REPLY_NOTHING_TO_REPEAT = "Пока нечего повторить."
REPLY_PING = "pong"         # служебная проверка Яндекса — без хода агента (Д17; порядок проверок — адаптер)
TELEGRAM_LABEL = "Алиса:"   # подпись всего, что плагин шлёт в Telegram
# platform_hint голосовой платформы (этап 5, текст утверждён владельцем): формат «ГОЛОС:/ТЕЛЕГРАМ:» — только для
# непроизносимого (разбор — split_answer); правило cron — S6 (адрес доставки подставляет хук cron_guard).
PLATFORM_HINT = (
    "Канал — голос Яндекс Алисы: ответ прозвучит вслух. Обычно отвечай простым разговорным текстом, без разметки, "
    "списков и ссылок; длинный ответ прочитается по частям. Если в ответе есть то, что нельзя произнести, — код, "
    "таблица, ссылка, длинный список, — раздели его: строка «ГОЛОС:» и короткий пересказ для озвучки, затем строка "
    "«ТЕЛЕГРАМ:» и подробности — они уйдут владельцу в Telegram. Напоминания и задания по расписанию, созданные здесь, "
    "нельзя доставить голосом: не указывай для них deliver, адрес доставки подставится сам.")

MAX_BODY = 16 * 1024   # байт тела запроса от Яндекса
MAX_TEXT = 1000        # символов в одной команде
MAX_REPLY = 1024       # лимит response.text Алисы
VOICE_LIMIT = 900      # символов голоса в одной реплике; запас под «Продолжить?»


def alice_reply(text: str, end_session: bool = False) -> bytes:
    """Беседа владельца не закрывается; end_session=True — только отказ чужому."""
    return json.dumps({"version": "1.0", "response": {"text": text[:MAX_REPLY], "end_session": end_session}},
                      ensure_ascii=False).encode()


class BadRequest(ValueError):
    """Тело не похоже на запрос Яндекс Диалогов; причина не содержит ввода."""


@dataclass(frozen=True)
class AliceRequest:
    text: str
    skill_id: str
    user_id: str           # "" — пользователь не авторизован в Алисе
    session_id: str
    message_id: str
    new: bool
    nlu: object

    @property
    def turn_id(self) -> str:
        return f"{self.session_id}-{self.message_id}"


def parse_request(body: bytes) -> AliceRequest:
    if not body or len(body) > MAX_BODY:
        raise BadRequest("size")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise BadRequest("json") from None
    if not isinstance(data, dict) or not isinstance(data.get("session"), dict):
        raise BadRequest("shape")
    session = data["session"]
    request = data.get("request")
    request = {} if request is None else request
    user = session.get("user")
    user = {} if user is None else user
    if not isinstance(request, dict) or not isinstance(user, dict):
        raise BadRequest("shape")
    utterance = request.get("original_utterance")
    utterance = "" if utterance is None else utterance
    session_id, message_id = session.get("session_id", ""), session.get("message_id", "")
    new, skill_id = session.get("new", False), session.get("skill_id", "")
    if (not isinstance(utterance, str) or not isinstance(session_id, str) or not isinstance(skill_id, str)
            or isinstance(message_id, bool) or not isinstance(message_id, (int, str)) or not isinstance(new, bool)):
        raise BadRequest("types")
    user_id = user.get("user_id") or ""
    if not isinstance(user_id, str):
        raise BadRequest("types")
    return AliceRequest(text=utterance.strip(), skill_id=skill_id, user_id=user_id, session_id=session_id,
                        message_id=str(message_id), new=new, nlu=request.get("nlu"))


def is_owner(user_id: str, owner_user_id: str) -> bool:
    return bool(owner_user_id) and hmac.compare_digest(user_id.encode(), owner_user_id.encode())


def is_rejected_command(text: str) -> bool:
    """Слэш-команды шлюза и управляющие символы с голоса не передаём (как alice-voice)."""
    return text.lstrip().startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in text) or len(text) > MAX_TEXT


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time):
        self.per_minute, self.clock = per_minute, clock
        self._lock, self._hits = threading.Lock(), []  # type: ignore[var-annotated]

    def allow(self) -> bool:
        now = self.clock()
        with self._lock:
            self._hits = [t for t in self._hits if now - t < 60]
            if len(self._hits) >= self.per_minute:
                return False
            self._hits.append(now)
            return True


def _clean_voice(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text, flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"[*_`#~\[\]]", "", text)
    return " ".join(text.split())


def voice_parts(text: str, limit: int = VOICE_LIMIT) -> list[str]:
    """Очищенный голос — на реплики ≤ limit: по предложениям жадно; предложение длиннее limit — по словам,
    слово длиннее limit — жёстко. Текст не теряется: склейка частей пробелом даёт исходный (кроме жёстких разрезов)."""
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        for word in sentence.split():
            pieces.extend(word[i:i + limit] for i in range(0, len(word), limit))
    parts: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        joined = f"{current} {piece}" if current else piece
        if len(joined) <= limit:
            current = joined
        else:
            parts.append(current)
            current = piece
    if current:
        parts.append(current)
    return parts


class Answer(NamedTuple):
    voice: str       # очищенный текст для голоса, без усечения
    telegram: str    # "" — в Telegram ничего
    marked: bool     # агент соблюл формат «ГОЛОС: … ТЕЛЕГРАМ: …» (для диагностики)


_MARKER = re.compile(r"(?im)^\s*[#*]*\s*(ГОЛОС|ТЕЛЕГРАММ?)\s*[*]*\s*:\s*[*]*[ \t]*")


def split_answer(raw: str) -> Answer:
    """Голос и Telegram из ответа «ГОЛОС: … ТЕЛЕГРАМ: …»; голос не усекается (длинный читается по частям).

    Формат — первый ГОЛОС и первый ТЕЛЕГРАМ после него; такие же строки в теле ответа не ломают разбор,
    текст до ГОЛОС остаётся в голосе. Без маркеров — всё голосом. Один маркер (только ГОЛОС или ТЕЛЕГРАМ первым)
    считается форматом, только если написан заглавными, как в platform_hint: «Телеграм: @ivan» в списке контактов —
    обычный текст. ТЕЛЕГРАМ первым — весь текст в Telegram (агент явно попросил)."""
    markers = list(_MARKER.finditer(raw))
    if not markers:
        return Answer(_clean_voice(raw), "", False)
    first = markers[0]
    head = raw[:first.start()]
    if first.group(1).upper() == "ГОЛОС":
        tail = next((m for m in markers[1:] if m.group(1).upper().startswith("ТЕЛЕГРАМ")), None)
        if tail is not None:
            voice, telegram = _clean_voice(head + " " + raw[first.end():tail.start()]), raw[tail.end():].strip()
            return Answer(voice, telegram, bool(voice and telegram))
        if first.group(1).isupper():
            return Answer(_clean_voice(head + " " + raw[first.end():]), "", False)
    elif first.group(1).isupper():
        return Answer("", "\n".join(s for s in (head.strip(), raw[first.end():].strip()) if s), False)
    return Answer(_clean_voice(raw), "", False)


def _strip_markers(raw: str) -> str:
    """Текст для Telegram без маркеров формата (заглавных); строки тела вроде «Телеграм: @ivan» остаются."""
    return _MARKER.sub(lambda m: "" if m.group(1).isupper() else m.group(0), raw).strip()


# Итог-ошибка шлюза Hermes (v0.23.1) приходит обычным итогом хода; отличить её можно только по тексту.
# Форма 1 — префикс locales/*.yaml explainer.no_reply_prefix перед любым объяснением выхода без ответа
# (в т. ч. explainer.empty_response); язык — display.language, по умолчанию en.
# Форма 2 — agent/turn_failure_copy.py exhausted_copy (только en). Закрепляется тестом совместимости (этап 6).
_NO_REPLY_PREFIXES = ("⚠ No reply: ", "⚠ Нет ответа: ")   # сравнение — без U+FE0F
_EXHAUSTED_MARK = "add a backup provider with `hermes fallback add`.\n\nProvider said: "
# Форма 3 — только в истории (S7): после исчерпанных повторов пустого ответа Hermes пишет в сессию строку «(empty)»
# (agent/turn_empty_response.py), а текст «No reply…» уходит в чат и в историю не попадает.
_EMPTY_SENTINEL = "(empty)"
# Форма 4 — тоже только в истории (S7): остановка шлюза посреди хода оставляет служебную фразу прерванного хода
# (agent/conversation_loop.py INTERRUPT_WAITING_FOR_MODEL_PREFIX, agent/turn_*.py); английские константы, не локализуются.
_INTERRUPTED_PREFIXES = ("Operation interrupted:", "Operation interrupted during ", "Operation interrupted.")
# Форма 5 — тоже только в истории (compat, группа H): ход, закончившийся без ответа (сбой провайдера), Hermes закрывает
# строкой-границей (agent/turn_failure_copy.py FAILED_TURN_NOTICE / PARTIAL_FAILED_TURN_NOTICE); целиком и дословно,
# чтобы настоящий ответ с цитатой этой фразы остался ответом.
_FAILED_TURN_NOTICES = (
    "Your request was not processed. Send it again if you still want me to carry it out.",
    "This turn did not complete. Some actions may already have run; verify their effects before resending.",
)


def is_gateway_error(text: str) -> bool:
    """Известная форма текста ошибки шлюза (пустой ответ модели, сбой провайдера, прерванный ход), а не ответ агента."""
    text = text.lstrip().replace("️", "")
    if (text.strip() in (_EMPTY_SENTINEL,) + _FAILED_TURN_NOTICES
            or text.startswith(_NO_REPLY_PREFIXES + _INTERRUPTED_PREFIXES)):
        return True
    return _EXHAUSTED_MARK in text and " — " in text.split("\n", 1)[0]


class Reply(NamedTuple):
    parts: list[str]   # реплики голосом по порядку; больше одной — чтение по частям («Продолжить?» — dialog.py)
    telegram: str      # готовый текст для Telegram с подписью; "" — не отправлять


def telegram_text(text: str) -> str:
    return f"{TELEGRAM_LABEL}\n{text}"


# Ссылка (со схемой, www., домен с путём вроде t.me/…) или адрес почты — голосом не прочесть (П5).
_LINK = re.compile(r"https?://|www\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S|[\w.+-]+@[\w-]+\.[\w.]+", re.I)


def whole_text(raw: str) -> str:
    """Весь итог хода для Telegram с подписью — «пришли в Телеграм», «новый диалог», истёкший незабранный (Д13).
    Маркеры формата срезаются; ошибка шлюза уходит как есть."""
    return telegram_text(raw.strip() if is_gateway_error(raw) else _strip_markers(raw))


def spoken(raw: str) -> list[str]:
    """Весь ответ голосом, без разделения на ГОЛОС/ТЕЛЕГРАМ: когда Telegram нет (не настроен, или это повтор из
    истории). Ошибка шлюза — «не получилось»."""
    if is_gateway_error(raw):
        return [REPLY_FAILED]
    return voice_parts(_clean_voice(_strip_markers(raw)))


def voice_reply(raw: str) -> Reply:
    """Итог хода → что сказать голосом и что отправить в Telegram.

    Telegram — только по явному «ТЕЛЕГРАМ:» агента, для ошибки шлюза и для ответа со ссылкой (голосом ссылку
    не прочесть — решение владельца 2026-11-17); длинный ответ без ссылок читается по частям и не дублируется."""
    if is_gateway_error(raw):
        return Reply([REPLY_FAILED], telegram_text(raw))
    voice, telegram, _ = split_answer(raw)
    if not telegram and _LINK.search(raw):
        telegram = _strip_markers(raw)
    if voice:
        return Reply(voice_parts(voice), telegram_text(telegram) if telegram else "")
    if telegram:
        return Reply([REPLY_IN_TELEGRAM], telegram_text(telegram))
    return Reply([REPLY_FAILED], "")


INTENT_ANSWER_GET = "hya.answer.get"            # «скинь ответ»
INTENT_DIALOG_NEW = "hya.dialog.new"            # «новый диалог» (грамматика без `...`, М5)
INTENT_DIALOG_CONTINUE = "hya.dialog.continue"  # «прошлый»
INTENT_WAIT = "hya.wait"                        # «подожду»
INTENT_LATER = "hya.later"                      # «позже»
INTENT_TELEGRAM = "hya.telegram"                # «пришли в Телеграм»
INTENT_NEXT = "hya.next"                        # «дальше» — следующая часть длинного ответа
YANDEX_CONFIRM, YANDEX_REJECT = "YANDEX.CONFIRM", "YANDEX.REJECT"
YANDEX_HELP, YANDEX_REPEAT = "YANDEX.HELP", "YANDEX.REPEAT"
INTENTS = (INTENT_ANSWER_GET, INTENT_DIALOG_NEW, INTENT_DIALOG_CONTINUE, INTENT_WAIT, INTENT_LATER,
           INTENT_TELEGRAM, INTENT_NEXT, YANDEX_CONFIRM, YANDEX_REJECT, YANDEX_HELP, YANDEX_REPEAT)


def intents(nlu: object) -> tuple[str, tuple[str, ...]]:
    """(статус nlu, известные интенты) — только имена из INTENTS; слоты и произвольный ввод не сохраняются.
    Смысл и приоритет зависят от состояния беседы и решаются в dialog.py."""
    if nlu is None:
        return "absent", ()
    if not isinstance(nlu, dict):
        return "invalid", ()
    if "intents" not in nlu:
        return "absent", ()
    if not isinstance(nlu["intents"], dict):
        return "invalid", ()
    return "valid", tuple(name for name in INTENTS if isinstance(nlu["intents"].get(name), dict))
