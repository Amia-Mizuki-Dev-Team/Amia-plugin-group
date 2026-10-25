"""Notice delivery orchestration.

The order of operations is the whole point of this module:

1. read the current notice revision and refuse if a previous attempt already
   owns the delivery;
2. persist an ``in_flight`` claim and commit it **before** calling the sender;
3. re-check authorisation, subscription, withdrawal and TTL after the claim;
4. call the provider exactly once;
5. persist ``accepted`` / ``failed`` / ``unknown``.

A crash anywhere after step 2 leaves a durable claim.  The next process marks
it ``unknown`` and never resends on its own: "no success record" is not proof
that nothing was sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from .ledger import AttemptRecord, NoticeLedger, to_iso
from .logic import (
    classify_provider_receipt,
    logical_delivery_key,
    render_plain_notice,
    scope_identifier,
    scope_kind,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot

logger = logging.getLogger(__name__)

MANUAL_RETRY_WARNING = "提示：上次投递结果未知，重投可能导致重复发送。"

_REASON_TEXT = {
    "accepted": "已受理（仅代表接口接受，不代表已显示）",
    "already_accepted": "此前已受理，本次不重发",
    "in_flight": "另有投递正在进行，本次不重发",
    "manual_replay": "同一人工重投操作已执行过，本次不重复发送",
    "unknown_requires_manual_retry": "上次投递结果未知（可能已发送），不自动重发",
    "payload_conflict": "同一投递键的内容不一致，拒绝发送",
    "retry_denied": "上次结果不允许重投（已撤回/已退订/已过期）",
    "notice_missing": "公告不存在",
    "notice_withdrawn": "公告已撤回",
    "notice_not_published": "公告尚未发布",
    "revision_mismatch": "公告内容版本已变化",
    "not_yet_due": "未到计划发布时间",
    "notice_expired": "公告已过期",
    "subscription_inactive": "目标未订阅公告",
    "receipt_unverified": "上游未给出可核实的受理凭据，结果未知",
    "send_timeout": "发送超时，结果未知",
    "cancelled": "发送被取消，结果未知",
    "terminal_write_failed": "发送结果写入失败，结果未知（已保留占位记录，不会自动重发）",
    "provider_failed": "上游拒绝受理",
    "not_started": "发送未开始（无副作用）",
    "claim_denied": "未取得投递占位，未发送",
    "claim_failed": "投递占位写入失败，未发送",
}


_STATUS_TEXT = {
    "accepted": "已受理（仅代表接口接受，不代表已显示）",
    "failed": "投递失败",
    "unknown": "结果未知",
}

# Gate blocks that prove nothing was sent yet may still become deliverable
# later (the window opened, the notice got published).  They must not consume
# the single automatic retry the way a provider failure does.
TRANSIENT_GATE_REASONS = frozenset({"not_yet_due", "notice_not_published"})


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """One upstream send result.

    ``accepted`` mirrors the provider verdict; the service still downgrades it
    to ``unknown`` when there is no verifiable message id.
    """

    accepted: bool
    provider_code: str = "ok"
    message_id: str | None = None
    retry_safe: bool = False


class DeliveryNotStartedError(RuntimeError):
    """The provider failed before any send side effect.

    Raising this is the only way to declare a failure safely retryable.  Any
    other exception is treated as ``unknown`` because a timeout or a dropped
    connection cannot prove the platform did not accept the message.
    """


class NoticeSender(Protocol):
    async def send(
        self,
        *,
        target_scope: str,
        content: str,
        notice_id: str,
        revision: int,
    ) -> SendReceipt: ...


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str
    reason: str | None
    notice_id: str
    revision: int
    target_scope: str
    attempt_no: int | None
    retry_safe: bool
    sent: bool

    @property
    def message(self) -> str:
        text = _REASON_TEXT.get(self.reason or "") or _STATUS_TEXT.get(
            self.status, "投递结束"
        )
        prefix = "已发送" if self.sent else "未发送"
        attempt = "" if self.attempt_no is None else f"（第 {self.attempt_no + 1} 次尝试）"
        return f"{prefix}：{text}{attempt}"


def describe(result: DeliveryResult) -> str:
    """Return the user visible line for one delivery result."""

    return result.message


def receipt_from_onebot_result(result: object) -> SendReceipt:
    """Map an OneBot v11 action result to a receipt.

    A ``retcode == 0`` answer with an empty or zero ``message_id`` is not
    delivery evidence; the caller keeps such an attempt ``unknown``.
    """

    payload: Mapping[str, object] = result if isinstance(result, Mapping) else {}
    provider_code = str(payload.get("status", "ok"))
    message_id = payload.get("message_id")
    return SendReceipt(
        accepted=provider_code != "failed",
        provider_code=provider_code,
        message_id=None if message_id is None else str(message_id),
    )


class OneBotNoticeSender:
    """OneBot v11 sender.

    Capability is treated as unknown: the body is always plain text, and no
    probe message is ever sent to discover Markdown or button support.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(
        self,
        *,
        target_scope: str,
        content: str,
        notice_id: str,
        revision: int,
    ) -> SendReceipt:
        kind = scope_kind(target_scope)
        identifier = scope_identifier(target_scope)
        if identifier is None or kind == "unknown":
            raise DeliveryNotStartedError("unsupported_target_scope")
        try:
            numeric_id = int(identifier)
        except ValueError as exc:
            raise DeliveryNotStartedError("non_numeric_target") from exc
        if kind == "group":
            result = await self._bot.send_group_msg(
                group_id=numeric_id, message=content
            )
        else:
            result = await self._bot.send_private_msg(
                user_id=numeric_id, message=content
            )
        return receipt_from_onebot_result(result)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NoticeDeliveryService:
    """Delivery entry point; there is no other path that sends a notice."""

    def __init__(
        self,
        ledger: NoticeLedger,
        *,
        capability: str = "unknown",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.ledger = ledger
        self.capability = capability
        self._clock = clock
        # A claim from a previous process run may or may not have been sent.
        self.recovered: list[AttemptRecord] = ledger.recover_in_flight(now=clock())

    async def deliver(
        self,
        *,
        notice_id: str,
        revision: int,
        target_scope: str,
        owner_scope: str,
        sender: NoticeSender,
        source_scope: str | None = None,
        manual_operation_id: str | None = None,
    ) -> DeliveryResult:
        now = self._clock()
        notice = self.ledger.get_notice(notice_id, revision)
        if notice is None:
            return self._result(
                "failed", "notice_missing", notice_id, revision, target_scope
            )
        logical_key = logical_delivery_key(
            owner_scope=owner_scope,
            source_scope=source_scope or notice.source_scope,
            target_scope=target_scope,
            notice_id=notice_id,
            revision=revision,
        )
        latest = self.ledger.latest_attempt(logical_key)
        if latest is not None:
            blocked = self._blocked_by(latest, manual_operation_id)
            if blocked is not None:
                status, reason = blocked
                return self._result(
                    status,
                    reason,
                    notice_id,
                    revision,
                    target_scope,
                    attempt_no=latest.attempt_no,
                )
        try:
            outcome = self.ledger.claim(
                logical_key=logical_key,
                notice_id=notice_id,
                revision=revision,
                owner_scope=owner_scope,
                source_scope=source_scope or notice.source_scope,
                target_scope=target_scope,
                payload=notice.content,
                manual_operation_id=manual_operation_id,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - no claim, so nothing may be sent
            # The claim transaction rolled back, which proves no send side
            # effect exists yet.  Never fall through to the network call: an
            # unpersisted claim is exactly the duplicate-send window.
            logger.warning(
                "notice ledger claim failed target=%s error=%s",
                target_scope,
                type(exc).__name__,
            )
            return self._result(
                "failed",
                "claim_failed",
                notice_id,
                revision,
                target_scope,
                retry_safe=True,
            )
        if not outcome.granted or outcome.attempt is None:
            reason = outcome.reason or "claim_denied"
            terminal = {"accepted", "failed", "unknown"}
            status = outcome.status if outcome.status in terminal else "unknown"
            attempt_no = (
                outcome.attempt.attempt_no if outcome.attempt is not None else None
            )
            return self._result(
                status,
                reason,
                notice_id,
                revision,
                target_scope,
                attempt_no=attempt_no,
            )
        attempt = outcome.attempt

        blocked_reason = self._recheck(
            notice_id=notice_id,
            revision=revision,
            payload=attempt.payload_digest,
            target_scope=target_scope,
            now=now,
        )
        if blocked_reason is not None:
            # not_yet_due / notice_not_published are provably unsent and can
            # become deliverable again; withdrawn / expired / unsubscribed are
            # permanent for this revision.
            return self._finish(
                attempt,
                "failed",
                reason=blocked_reason,
                sent=False,
                retry_safe=blocked_reason in TRANSIENT_GATE_REASONS,
            )

        content = render_plain_notice(notice.content)
        try:
            receipt = await sender.send(
                target_scope=target_scope,
                content=content,
                notice_id=notice_id,
                revision=revision,
            )
        except DeliveryNotStartedError as exc:
            return self._finish(
                attempt, "failed", reason=f"not_started:{exc}", sent=False,
                retry_safe=True,
            )
        except asyncio.CancelledError:
            self._finish(attempt, "unknown", reason="cancelled", sent=True)
            raise
        except TimeoutError:
            # ``asyncio.TimeoutError`` is the same class from 3.11 on; naming
            # both keeps 3.9/3.10 behaviour explicit.
            return self._finish(
                attempt, "unknown", reason="send_timeout", sent=True
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure is unknown
            return self._finish(
                attempt,
                "unknown",
                reason=f"send_error:{type(exc).__name__}",
                sent=True,
            )

        status = classify_provider_receipt(
            accepted=receipt.accepted, message_id=receipt.message_id
        )
        reason = None
        if status == "unknown":
            reason = "receipt_unverified"
        elif status == "failed":
            reason = receipt.provider_code or "provider_failed"
        return self._finish(
            attempt,
            status,
            reason=reason,
            provider_code=receipt.provider_code,
            message_id=receipt.message_id,
            sent=True,
            retry_safe=status == "failed" and receipt.retry_safe,
        )

    async def deliver_due(
        self, *, sender: NoticeSender, now: datetime | None = None
    ) -> list[DeliveryResult]:
        """Deliver every scheduled notice whose window is open.

        Only an explicit call brings us here.  Nothing in this plugin wires
        this method to NoneBot's startup/shutdown hooks, so a published
        schedule never sends by itself: an operator must run the delivery
        command.  It also never runs because some unrelated command finished.
        """

        moment = now if now is not None else self._clock()
        results: list[DeliveryResult] = []
        for notice in self.ledger.due_notices(now=moment):
            for scope in self.ledger.subscribed_scopes():
                results.append(
                    await self.deliver(
                        notice_id=notice.notice_id,
                        revision=notice.revision,
                        target_scope=scope,
                        owner_scope=notice.source_scope,
                        sender=sender,
                    )
                )
        return results

    # --- internals -----------------------------------------------------
    def _blocked_by(
        self, latest: AttemptRecord, manual_operation_id: str | None
    ) -> tuple[str, str] | None:
        if latest.status == "accepted":
            return ("accepted", "already_accepted")
        if latest.status == "in_flight":
            # Another worker owns the claim; read the state and leave without
            # sending instead of bumping the attempt number.
            return ("unknown", "in_flight")
        if latest.status == "unknown":
            # An unproven outcome may only be retried by an explicit manual
            # operation that carries its own idempotency id.
            if manual_operation_id:
                return None
            return ("unknown", "unknown_requires_manual_retry")
        if latest.status == "failed":
            if latest.reason in TRANSIENT_GATE_REASONS:
                # Provably unsent and only waiting for its window: the next
                # explicit sweep may retry without an operator id.
                return None
            if latest.retry_safe:
                return None if manual_operation_id else ("failed", "provider_failed")
            # Blocking failures (withdrawn/unsubscribed/expired) never retry.
            return ("failed", "retry_denied")
        return ("unknown", "claim_denied")

    def _recheck(
        self,
        *,
        notice_id: str,
        revision: int,
        payload: str,
        target_scope: str,
        now: datetime,
    ) -> str | None:
        """Re-read the business state after the claim, before the network call."""

        notice = self.ledger.get_notice(notice_id, revision)
        if notice is None:
            return "notice_missing"
        if notice.state == "withdrawn":
            return "notice_withdrawn"
        if notice.state != "published":
            return "notice_not_published"
        if notice.payload_digest != payload:
            return "revision_mismatch"
        stamp = to_iso(now)
        if notice.publish_at is not None and notice.publish_at > stamp:
            return "not_yet_due"
        if notice.expires_at is not None and notice.expires_at <= stamp:
            return "notice_expired"
        if self.ledger.subscription_state(target_scope) != "subscribed":
            return "subscription_inactive"
        return None

    def _finish(
        self,
        attempt: AttemptRecord,
        status: str,
        *,
        sent: bool,
        reason: str | None,
        retry_safe: bool = False,
        provider_code: str | None = None,
        message_id: str | None = None,
    ) -> DeliveryResult:
        try:
            record = self.ledger.finish(
                attempt.attempt_id,
                status,
                retry_safe=retry_safe,
                reason=reason,
                provider_code=provider_code,
                message_id=message_id,
                now=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - a failed write must not resend
            logger.warning(
                "notice ledger write-back failed attempt=%s error=%s",
                attempt.attempt_id,
                type(exc).__name__,
            )
            record = None
        if record is None:
            # The durable claim stays in place, so nothing can resend this
            # delivery automatically; report the unconfirmed outcome.
            return DeliveryResult(
                status="unknown",
                reason="terminal_write_failed",
                notice_id=attempt.notice_id,
                revision=attempt.revision,
                target_scope=attempt.target_scope,
                attempt_no=attempt.attempt_no,
                retry_safe=False,
                sent=sent,
            )
        return DeliveryResult(
            status=record.status,
            reason=record.reason,
            notice_id=record.notice_id,
            revision=record.revision,
            target_scope=record.target_scope,
            attempt_no=record.attempt_no,
            retry_safe=record.retry_safe,
            sent=sent,
        )

    @staticmethod
    def _result(
        status: str,
        reason: str | None,
        notice_id: str,
        revision: int,
        target_scope: str,
        *,
        attempt_no: int | None = None,
        retry_safe: bool = False,
    ) -> DeliveryResult:
        return DeliveryResult(
            status=status,
            reason=reason,
            notice_id=notice_id,
            revision=revision,
            target_scope=target_scope,
            attempt_no=attempt_no,
            retry_safe=retry_safe,
            sent=False,
        )


__all__ = [
    "MANUAL_RETRY_WARNING",
    "TRANSIENT_GATE_REASONS",
    "DeliveryNotStartedError",
    "DeliveryResult",
    "NoticeDeliveryService",
    "NoticeSender",
    "OneBotNoticeSender",
    "SendReceipt",
    "describe",
    "receipt_from_onebot_result",
]
