"""Pure layer of the S6 probe (stdlib only, no Hermes): what a ``send()`` of the voice platform is.

``classify_send``, ``split_marker`` and ``strip_marker`` are copied unchanged from ``prototypes/s5/plugin``
(the S1 final rule plus the ledger marker cut by FORM). S6 adds ``forward_text``: what the plugin sends
to Telegram for a send it does not voice - only a send OUTSIDE any open turn that is neither a service
message (``_interim_send``) nor a recovered reply, prefixed with the owner's chosen "Алиса:" label.
Non-final sends INSIDE an open turn (``📬 No home channel…``, tool progress, retry notices) are neither
voiced nor forwarded.
"""

FINAL, INTERIM, OUTSIDE, RECOVERED = "final", "interim", "outside", "recovered"
_SIGN = "♻"  # the recycling sign; Hermes appends U+FE0F after it, which we do not require
LABEL = "Алиса:"


def split_marker(text):
    """``(marker, rest)``; ``("", text)`` when the text has no marker-shaped first paragraph."""
    text = text or ""
    head, sep, rest = text.partition("\n\n")
    if sep and rest.strip() and head.startswith(_SIGN) and head.rstrip().endswith(":"):
        return head, rest
    return "", text


def strip_marker(text):
    return split_marker(text)[1]


def classify_send(*, open_turn, turn_mid, reply_to, notify, interim, text):
    """FINAL / INTERIM / RECOVERED / OUTSIDE for one ``send()``.

    ``open_turn``: the chat has a turn between ``on_processing_start`` and ``on_processing_complete``;
    ``turn_mid``: that turn's ``message_id`` (``None`` for an internal auto-resume turn).
    """
    if interim:
        return INTERIM
    if open_turn and notify is True and reply_to == turn_mid:
        return FINAL
    if not open_turn and split_marker(text)[0]:
        return RECOVERED
    return OUTSIDE


def forward_text(*, open_turn, kind, text):
    """The Telegram text for a send that is forwarded, or ``None`` when it is not forwarded."""
    if open_turn or kind != OUTSIDE or not (text or "").strip():
        return None
    return LABEL + "\n\n" + text
