"""Pure layer of the S5 probe (stdlib only, no Hermes): what a ``send()`` of the voice platform is.

Model of what stage 2/3 will own. ``classify_send`` applies the S1 rule (a final is a send whose
``reply_to`` is the open turn's message id, with ``notify is True`` and no ``_interim_send``) and adds
the one case S5 needs: a send OUTSIDE any open turn whose first paragraph has the shape of Hermes'
delivery-ledger marker ("♻️ Recovered reply - ...:") is a recovered reply. The marker is cut by FORM
only (first paragraph starts with the recycling sign and ends with a colon, followed by a blank line),
never by words. A ledger redelivery of a row that was never started carries NO marker; it is
indistinguishable from other outside sends by text and is reported as OUTSIDE.
"""

FINAL, INTERIM, OUTSIDE, RECOVERED = "final", "interim", "outside", "recovered"
_SIGN = "♻"  # the recycling sign; Hermes appends U+FE0F after it, which we do not require


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
