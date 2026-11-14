"""Протокол Яндекс Диалогов и разбор ответа агента — без зависимостей от Hermes.

Перенесено из old-bridge (bridge/alice_bridge.py) без изменения поведения;
выброшены HTTP-клиент, подпись V2 и обрезка под тело webhook — в плагине их нет.
Модуль импортируется и тестируется без checkout Hermes.

Это стартовая точка: логика будет переделана под диалог (сессия Алисы не закрывается после
ответа) — см. docs/RECON.md, «Модель диалога».
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

REPLY_HELLO = "Скажите задачу для секретаря."
REPLY_NOT_OWNER = "Этот навык работает только для владельца."
REPLY_UNBOUND = "Навык ещё не привязан к владельцу."
REPLY_REJECTED = "Такую команду я не передам."
REPLY_RATE = "Слишком много команд подряд, подождите минуту."
REPLY_THINKING = "Думаю... Готовы подождать?"
REPLY_WAIT_HELP = "Я готовлю ответ. Можно подождать или сказать „пришли в Телеграм“. Готовы подождать?"
REPLY_TELEGRAM = "Пришлю результат в Телеграм."
REPLY_ERROR = "Секретарь сейчас недоступен, повторите позже."
REPLY_LOST = "Я потеряла эту задачу, повторите команду."
VOICE_TAIL = "Ответ — в Телеграме."

MAX_BODY = 16 * 1024   # байт тела запроса от Яндекса
MAX_TEXT = 1000        # символов в одной команде
MAX_REPLY = 1024       # лимит response.text Алисы


def alice_reply(text: str, end_session: bool = True) -> bytes:
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


def speakable(text: str, limit: int) -> str:
    text = _clean_voice(text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    sentences = list(re.finditer(r"[.!?…](?=\s|$)", head))
    if sentences:
        return head[:sentences[-1].end()].strip()
    return head.rsplit(" ", 1)[0].strip() if " " in head else head


def split_answer(raw: str, voice_max: int = 600) -> tuple[str, str, bool]:
    """(голос, телеграм, формат_соблюдён) из ответа в формате «ГОЛОС: … ТЕЛЕГРАМ: …»."""
    markers = list(re.finditer(r"(?im)^\s*[#*]*\s*(ГОЛОС|ТЕЛЕГРАММ?)\s*[*]*\s*:\s*[*]*[ \t]*", raw))
    # Формат — первый ГОЛОС и первый ТЕЛЕГРАМ после него; такие же строки в теле ответа не ломают разбор.
    ok, tail = False, None
    if markers and markers[0].group(1).upper() == "ГОЛОС":
        tail = next((m for m in markers[1:] if m.group(1).upper().startswith("ТЕЛЕГРАМ")), None)
        ok = tail is not None
    voice, telegram = "", raw
    if ok:
        voice = _clean_voice(raw[markers[0].end():tail.start()])
        telegram = raw[tail.end():].strip()
        ok = bool(voice and telegram)
    if not ok:
        voice = speakable(raw, min(300, voice_max - len(VOICE_TAIL) - 1)) + " " + VOICE_TAIL
        telegram = raw
    elif len(voice) > voice_max:
        voice = speakable(voice, voice_max - len(VOICE_TAIL) - 1) + " " + VOICE_TAIL
    return voice.strip(), telegram, ok


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


_STRONG_REFUSAL = {"потом", "позже", "хватит", "стоп", "отмена", "некогда"}


def _telegram_word(word: str) -> bool:
    return word in {"telegram", "пришли"} or word.startswith(("телеграм", "присла", "присыл", "текст"))


def _negatable_word(word: str) -> bool:
    """Слова, отрицание которых («не буду», «не сейчас») — отказ ждать."""
    return (word in {"буду", "жду", "ждать", "надо", "нужно", "хочу", "могу", "давай", "сейчас", "вслух", "готов", "готова"}
            or word.startswith(("подожд", "голос", "озвуч")))


def _consent_word(word: str) -> bool:
    return (word in {"да", "жду", "ждать", "давай", "ладно", "хорошо", "буду", "конечно", "ок", "угу", "ага", "вслух",
                     "скажи", "говори", "готов", "готова"} or word.startswith(("подожд", "голос", "озвуч")))


REPLY_INTENTS = ("bridge.wait", "bridge.telegram", "bridge.no_wait", "YANDEX.CONFIRM",
                 "YANDEX.REJECT", "YANDEX.HELP", "YANDEX.REPEAT")


def reply_intents(nlu: object) -> tuple[str, tuple[str, ...]]:
    """Для решений и диагностики — только известные имена; слоты и произвольный ввод не сохраняются."""
    if nlu is None:
        return "absent", ()
    if not isinstance(nlu, dict):
        return "invalid", ()
    if "intents" not in nlu:
        return "absent", ()
    if not isinstance(nlu["intents"], dict):
        return "invalid", ()
    return "valid", tuple(name for name in REPLY_INTENTS if isinstance(nlu["intents"].get(name), dict))


def _classify_intents(intents: tuple[str, ...]) -> str:
    if "bridge.wait" in intents:
        return "wait"
    if "bridge.telegram" in intents or "bridge.no_wait" in intents:
        return "telegram"
    # HELP — согласие ждать, но при неготовом результате нужна подсказка пользователю.
    if "YANDEX.HELP" in intents:
        return "help"
    if "YANDEX.CONFIRM" in intents or "YANDEX.REPEAT" in intents:
        return "wait"
    if "YANDEX.REJECT" in intents:
        return "telegram"
    return "wait"


def classify_reply(nlu: object, *, intents: Optional[tuple[str, ...]] = None) -> str:
    """Только для открытого вопроса: свои интенты > встроенные; без интентов ждём."""
    return _classify_intents(reply_intents(nlu)[1] if intents is None else intents)


def _control_word(word: str) -> bool:
    return (_telegram_word(word) or _negatable_word(word) or _consent_word(word)
            or word in _STRONG_REFUSAL or word in {"в", "не", "нет"})


def is_control_phrase(text: str) -> bool:
    words = _words(text)
    return bool(words) and all(_control_word(word) for word in words)
