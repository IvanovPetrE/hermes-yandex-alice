"""Approval cards of the S4 probe plugin: pure logic, standard library only.

One card per Hermes approval request. A card is born ``sending`` (the Telegram send is in flight),
becomes ``open`` once Telegram acknowledged it, ``resolving`` while a press is being settled, and
ends ``resolved`` (a press settled the request), ``failed`` (the card could not be delivered in time
and the request was denied) or ``expired`` (the request ended some other way: timeout, turn over, a
press that found nothing). Every transition happens under one lock, so a press and the send
deadline can never both win.

Who may press: ``ALICE_TELEGRAM_OWNER_ID`` when set; otherwise the id of ``ALICE_TELEGRAM_CHAT`` when
that chat is a private one (positive id: in a private chat chat_id == user_id). A group chat
without an explicit owner disables approvals (every request is denied at once). Presses count only
in the configured chat.

This module is the model of the future clean layer (stage 5); it never imports Hermes.
"""

from __future__ import annotations

import html
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

PREFIX = "hya"
CHOICES = ("once", "deny")  # one-time approvals only: no session / always tier
MAX_CMD_ON_CARD = 3500      # escaped chars of command a card may show whole (Telegram cap is 4096)
PREVIEW_CHARS = 3000
FULL_COMMAND_MIN = 3000     # Hermes' base _EA_CMD_BUDGET: longer commands arrive as a fenced block first
LIVE = ("sending", "open", "resolving")

# Press decisions.
RESOLVE = "resolve"
STALE = "stale"            # unknown token, or the card is no longer live
WAIT = "wait"              # the send is not acknowledged yet: press again in a second
WRONG_CHAT = "wrong_chat"
NOT_OWNER = "not_owner"
NOT_OFFERED = "not_offered"


def parse_id(raw: object) -> Optional[int]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def resolve_owner(chat_raw: object, owner_raw: object) -> tuple[Optional[int], Optional[int]]:
    """``(chat_id, owner_id)``; owner is ``None`` (approvals disabled) when it cannot be told."""
    chat = parse_id(chat_raw)
    if chat is None:
        return None, None
    if str(owner_raw or "").strip():
        return chat, parse_id(owner_raw)
    return chat, (chat if chat > 0 else None)


def callback_data(choice: str, token: str) -> str:
    return f"{PREFIX}:{choice}:{token}"


def parse_callback(data: str) -> Optional[tuple[str, str]]:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != PREFIX or not parts[2]:
        return None
    return parts[1], parts[2]


def is_full_command_block(content: str, metadata: Optional[dict]) -> bool:
    """The whole command Hermes sends through the voice adapter before a truncated card
    (``base_exec_approval.send_full_command``): an interim fenced block longer than the card budget."""
    text = content or ""
    return (bool((metadata or {}).get("_interim_send")) and text.startswith("```\n")
            and text.endswith("\n```") and len(text) > FULL_COMMAND_MIN)


def render_card(command: str, description: str, smart_denied: bool) -> tuple[str, bool]:
    """``(HTML text, whole)``: ``whole`` is False when the command does not fit the card."""
    escaped = html.escape(command or "")
    whole = len(escaped) <= MAX_CMD_ON_CARD
    shown = escaped if whole else html.escape((command or "")[:PREVIEW_CHARS]) + "…"
    lines = ["⚠️ <b>Hermes просит подтвердить команду</b>", f"<pre>{shown}</pre>",
             "Причина: " + html.escape((description or "")[:500])]
    if not whole:
        lines.append("Команда показана не целиком — разрешить нельзя, только отклонить.")
    if smart_denied:
        lines.append("Модель безопасности Hermes советует отклонить.")
    lines.append("Без ответа команда не выполнится.")
    return "\n".join(lines), whole


def offered_choices(actions: list, whole: bool) -> tuple[str, ...]:
    """Choices the card shows: Hermes' ``(label, choice, style)`` rows cut to once/deny; Deny always."""
    offered = [c for _label, c, _style in actions or [] if c in CHOICES]
    if not whole:
        offered = [c for c in offered if c == "deny"]
    if "deny" not in offered:
        offered.append("deny")
    return tuple(c for c in CHOICES if c in offered)


@dataclass
class Card:
    card_id: str
    token: str
    session_key: str
    request_id: str
    alice_chat: str
    choices: tuple
    text: str
    state: str = "sending"
    tg_message_id: Optional[int] = None
    outcome: Optional[str] = None
    final_shown: bool = False  # the Telegram card already shows its final text (no buttons)


@dataclass
class Event:
    t: float
    card_id: str
    event: str
    alice_chat: str
    awaiting: bool
    detail: dict = field(default_factory=dict)


class ApprovalBook:
    def __init__(self, chat_raw: object = None, owner_raw: object = None,
                 clock: Callable[[], float] = time.monotonic,
                 new_token: Callable[[], str] = lambda: secrets.token_urlsafe(12),
                 new_card_id: Callable[[], str] = lambda: "hya-card:" + secrets.token_hex(6)) -> None:
        self.chat, self.owner = resolve_owner(chat_raw, owner_raw)
        self._clock, self._new_token, self._new_card_id = clock, new_token, new_card_id
        self._lock = threading.Lock()
        self._by_token: dict[str, Card] = {}
        self._by_id: dict[str, Card] = {}
        self.events: list[Event] = []

    @property
    def enabled(self) -> bool:
        return self.chat is not None and self.owner is not None

    # -- internals (caller holds the lock) --

    def _awaiting(self, alice_chat: str) -> bool:
        return any(c.alice_chat == alice_chat and c.state in LIVE for c in self._by_id.values())

    def _log(self, card: Card, event: str, **detail) -> None:
        self.events.append(Event(self._clock(), card.card_id, event, card.alice_chat,
                                 self._awaiting(card.alice_chat), dict(detail)))

    def _move(self, card: Card, frm: tuple, to: str, event: str, **detail) -> bool:
        if card.state not in frm:
            return False
        card.state = to
        self._log(card, event, **detail)
        return True

    # -- lifecycle --

    def open(self, session_key: str, request_id: Optional[str], alice_chat: str,
             choices: tuple, text: str) -> Optional[Card]:
        """A new card in ``sending``; ``None`` without a request id (resolving one would hit the oldest)."""
        if not request_id or not session_key:
            return None
        card = Card(self._new_card_id(), self._new_token(), session_key, str(request_id), str(alice_chat),
                    tuple(choices), text)
        with self._lock:
            self._by_token[card.token] = card
            self._by_id[card.card_id] = card
            self._log(card, "open", choices=list(card.choices))
        return card

    def mark_sent(self, card_id: str, tg_message_id: int) -> bool:
        """sending -> open. False: the deadline already failed the card (a late delivery)."""
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None:
                return False
            card.tg_message_id = tg_message_id
            return self._move(card, ("sending",), "open", "sent")

    def fail(self, card_id: str, why: str) -> bool:
        """sending -> failed. True means the caller must deny the request now."""
        with self._lock:
            card = self._by_id.get(card_id)
            return card is not None and self._move(card, ("sending",), "failed", "failed", why=why)

    def note_late_delivery(self, card_id: str, tg_message_id: int) -> Optional[Card]:
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None:
                return None
            card.tg_message_id = tg_message_id
            self._log(card, "late_delivery")
            return card

    def press(self, token: str, choice: str, user_id: object, chat_id: object) -> tuple[str, Optional[Card]]:
        """Decide one press; ``RESOLVE`` moves the card to ``resolving`` (exactly one press wins)."""
        with self._lock:
            card = self._by_token.get(token)
            if card is None:
                return STALE, None
            if parse_id(chat_id) is None or parse_id(chat_id) != self.chat:
                return WRONG_CHAT, card
            if self.owner is None or parse_id(user_id) != self.owner:
                return NOT_OWNER, card
            if card.state == "sending":
                return WAIT, card
            if card.state != "open":
                return STALE, card
            if choice not in card.choices:
                return NOT_OFFERED, card
            self._move(card, ("open",), "resolving", "press", choice=choice)
            return RESOLVE, card

    def finish(self, card_id: str, choice: str, count: int) -> Optional[Card]:
        """resolving -> resolved (count > 0) or expired (the request was already gone)."""
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None:
                return None
            card.outcome = choice if count else "gone"
            self._move(card, ("resolving",), "resolved" if count else "expired", "finish",
                       choice=choice, count=count)
            return card

    def expire(self, card_id: str, why: str) -> Optional[Card]:
        """open -> expired (Hermes' timeout notice edits the card)."""
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None or not self._move(card, ("open",), "expired", "expire", why=why):
                return None
            return card

    def close_chat(self, alice_chat: str) -> list[Card]:
        """The turn is over: open cards of this chat can no longer resolve anything."""
        with self._lock:
            closed = [c for c in self._by_id.values() if c.alice_chat == str(alice_chat) and c.state == "open"]
            for card in closed:
                self._move(card, ("open",), "expired", "expire", why="turn_complete")
            return closed

    def awaiting(self, alice_chat: str) -> bool:
        with self._lock:
            return self._awaiting(str(alice_chat))

    def card(self, card_id: str) -> Optional[Card]:
        with self._lock:
            return self._by_id.get(card_id)
