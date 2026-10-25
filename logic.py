"""Pure helpers for the notice plugin.

The helpers in this module deliberately do not import NoneBot.  Keeping the
validation, scope parsing and delivery-key calculations here makes the
important behavior easy to test offline and keeps the runtime matcher focused
on message I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_NOTICES = 5
MAX_NOTICE_LENGTH = 2000

# Draft, published and delivered are three independent facts.  A published
# notice may never have been delivered to any group; delivery state lives in
# the attempt ledger, not on the notice row.
NOTICE_STATES = frozenset({"draft", "published", "withdrawn"})
DELIVERY_STATES = frozenset({"in_flight", "accepted", "failed", "unknown"})
TERMINAL_DELIVERY_STATES = frozenset({"accepted", "failed", "unknown"})

SCOPE_KINDS = frozenset({"group", "private"})


@dataclass(frozen=True, slots=True)
class NoticeCommand:
    """The small command grammar used by the announcement matcher."""

    operation: str
    argument: str | None = None


def get_content_hash(content: str) -> str:
    """Return the legacy version identifier used by the old JSON history."""

    return hashlib.md5(content.encode("utf-8")).hexdigest()


def payload_digest(content: str) -> str:
    """Return the fingerprint stored with one delivery attempt.

    The digest lets the ledger reject a second attempt that reuses the same
    logical key with different content instead of silently sending a payload
    nobody approved.
    """

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def target_key(*, group_id: object | None = None, user_id: object | None = None) -> str:
    """Return a namespace-safe history key for one delivery target."""

    if group_id is not None:
        return group_scope(group_id)
    if user_id is not None:
        return private_scope(user_id)
    raise ValueError("a group_id or user_id is required")


def group_scope(group_id: object) -> str:
    return f"group:{group_id}"


def private_scope(user_id: object) -> str:
    return f"private:{user_id}"


def scope_kind(scope: object) -> str:
    """Return ``group``/``private`` for a scope key, ``unknown`` otherwise."""

    if not isinstance(scope, str):
        return "unknown"
    kind, separator, identifier = scope.partition(":")
    if not separator or not identifier or kind not in SCOPE_KINDS:
        return "unknown"
    return kind


def scope_identifier(scope: str) -> str | None:
    """Return the raw id inside a scope key, or ``None`` when malformed."""

    if scope_kind(scope) == "unknown":
        return None
    return scope.partition(":")[2]


def parse_target_scope(value: str | None) -> str | None:
    """Normalize a user supplied target such as ``group:123``.

    Only fully qualified scopes are accepted: an unqualified number is
    ambiguous (group id or private id) and guessing would widen the blast
    radius of an announcement.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if any(character.isspace() for character in text):
        return None
    kind, separator, identifier = text.partition(":")
    if not separator or not identifier or kind not in SCOPE_KINDS:
        return None
    if ":" in identifier:
        return None
    return f"{kind}:{identifier}"


def logical_delivery_key(
    *,
    owner_scope: str,
    source_scope: str,
    target_scope: str,
    notice_id: str,
    revision: int,
) -> str:
    """Return the stable delivery key for one notice revision and target.

    The key must not be a bare group id, a text hash or a business operation
    id: two different revisions of the same notice are different deliveries,
    and the same revision to two groups is two deliveries.
    """

    return (
        "notice:"
        f"{notice_id}@{revision}"
        f"|owner={owner_scope}"
        f"|source={source_scope}"
        f"|target={target_scope}"
    )


def parse_notice_command(text: str) -> NoticeCommand | None:
    """Parse the public announcement command without importing NoneBot."""

    if not isinstance(text, str):
        return None
    parts = text.strip().split(maxsplit=2)
    if not parts or parts[0] != "公告":
        return None
    operation = parts[1] if len(parts) > 1 else "查看"
    argument = parts[2] if len(parts) > 2 else None
    return NoticeCommand(operation=operation, argument=argument)


def valid_notice_content(content: str | None) -> bool:
    return bool(
        isinstance(content, str)
        and content.strip()
        and len(content) <= MAX_NOTICE_LENGTH
    )


def parse_notice_index(value: str | None, notice_count: int) -> int | None:
    """Convert a one-based user index to a zero-based list index.

    ``None`` is returned for non-decimal input and for indexes outside the
    current notice list.  The explicit decimal check avoids surprising
    ``int`` behavior for values such as underscores or superscript digits.
    """

    if not isinstance(value, str) or not value or not value.isdecimal():
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    if not 1 <= number <= notice_count:
        return None
    return number - 1


def render_plain_notice(content: str) -> str:
    """Render the delivery body.

    Capability is unknown by default, so the plugin only ever produces plain
    text.  Markdown or buttons are never attempted as a probe: an unknown
    capability degrades to text instead of being discovered by sending.
    """

    return f"【Mizuki 公告】\n{content}"


def verifiable_message_id(value: object) -> bool:
    """Return whether a provider message id is real evidence of acceptance."""

    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text or text in {"0", "None", "null"}:
        return False
    digits = text.removeprefix("-")
    if digits.isdigit() and int(text) <= 0:
        return False
    return True


def classify_provider_receipt(*, accepted: bool, message_id: object) -> str:
    """Map one upstream send result to an attempt status.

    A zero retcode (``accepted=True``) is not delivery evidence: only a
    verifiable message id makes an attempt ``accepted``.  Everything else that
    looks successful but unverifiable stays ``unknown`` and is never retried
    automatically.
    """

    if not accepted:
        return "failed"
    if not verifiable_message_id(message_id):
        return "unknown"
    return "accepted"


def parse_schedule(value: str | None) -> datetime | None:
    """Parse an ISO-8601 schedule value into an aware UTC datetime.

    A value without an offset is interpreted in the host's local timezone: the
    operator writes the time they see on their own clock, not UTC.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        local_zone = datetime.now(timezone.utc).astimezone().tzinfo
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(timezone.utc)


def subscription_coverage(
    subscribed: Iterable[str], delivered: Iterable[str]
) -> tuple[int, int]:
    """Return ``(delivered_targets, subscribed_targets)``.

    The denominator is the subscription table, never ``get_group_list``: the
    group list is a snapshot of what the bot can see, not the set of groups
    that asked for announcements.
    """

    subscribed_set = {scope for scope in subscribed if isinstance(scope, str)}
    delivered_set = {
        scope
        for scope in delivered
        if isinstance(scope, str) and scope in subscribed_set
    }
    return len(delivered_set), len(subscribed_set)
