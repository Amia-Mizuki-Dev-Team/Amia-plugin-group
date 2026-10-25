"""Delivery semantics tests: claim durability, crash windows and receipts.

Each case is written so that a specific wrong implementation turns it red:

* claim tests            -> dropping the claim commit, or sending before the
                            claim is durable;
* recovery tests         -> turning a crashed in-flight claim into an
                            automatic resend;
* preflight tests        -> delivering a withdrawn / unsubscribed / expired
                            notice because only the first read was checked;
* receipt tests          -> treating ``retcode=0`` as delivery evidence;
* postprocessor test     -> re-adding a global ``run_postprocessor`` that sends
                            on behalf of other matchers.
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import add_repo_paths

add_repo_paths()

from plugin_loader import load_plugin

PLUGIN = load_plugin()
DELIVERY = sys.modules[f"{PLUGIN.__name__}.delivery"]
LEDGER = sys.modules[f"{PLUGIN.__name__}.ledger"]
LOGIC = sys.modules[f"{PLUGIN.__name__}.logic"]

SendReceipt = DELIVERY.SendReceipt
NoticeDeliveryService = DELIVERY.NoticeDeliveryService
NoticeLedger = LEDGER.NoticeLedger


def _attempt_rows(path: Path) -> list[tuple[object, str, str | None]]:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(
            "SELECT attempt_no, status, reason FROM delivery_attempts"
            " ORDER BY attempt_no"
        ).fetchall()
    finally:
        connection.close()


def _latest_status(path: Path) -> str | None:
    connection = sqlite3.connect(str(path))
    try:
        row = connection.execute(
            "SELECT status FROM delivery_attempts ORDER BY attempt_no DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


class RecordingSender:
    def __init__(self, receipt: object | None = None, error: Exception | None = None):
        self.receipt = receipt or SendReceipt(
            accepted=True, provider_code="ok", message_id="1001"
        )
        self.error = error
        self.calls: list[tuple[str, str, str, int]] = []

    async def send(
        self, *, target_scope: str, content: str, notice_id: str, revision: int
    ) -> object:
        self.calls.append((target_scope, content, notice_id, revision))
        if self.error is not None:
            raise self.error
        return self.receipt


class ProbeSender(RecordingSender):
    """Records what a *separate* connection sees at send time."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.observed: list[str | None] = []

    async def send(self, **kwargs: object) -> object:
        self.observed.append(_latest_status(self.path))
        return await super().send(**kwargs)  # type: ignore[arg-type]


class DeliveryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        self.ledgers = []
        self.ledger = self._new_ledger("boot-a")
        self.service = self._new_service()
        self.sender = RecordingSender()
        self.notice = self.ledger.create_notice(
            "第一版", source_scope="group:101", now=self.now
        )
        self.ledger.set_subscription("group:101", subscribed=True, now=self.now)

    def tearDown(self) -> None:
        for ledger in self.ledgers:
            ledger.close()
        self.temp_dir.cleanup()

    # --- helpers -------------------------------------------------------
    def _new_ledger(self, boot_id: str) -> object:
        ledger = NoticeLedger(self.path, boot_id=boot_id)
        self.ledgers.append(ledger)
        return ledger

    def _new_service(self) -> object:
        return NoticeDeliveryService(self.ledger, clock=lambda: self.now)

    def publish(self, *, publish_at=None, expires_at=None) -> None:
        self.ledger.publish(
            self.notice.notice_id,
            source_scope="group:101",
            publish_at=publish_at if publish_at is not None else self.now,
            expires_at=expires_at,
            now=self.now,
        )

    def key(self) -> str:
        return LOGIC.logical_delivery_key(
            owner_scope="group:101",
            source_scope="group:101",
            target_scope="group:101",
            notice_id=self.notice.notice_id,
            revision=1,
        )

    def claim(self, *, ledger=None, manual=None):  # type: ignore[no-untyped-def]
        target = ledger or self.ledger
        return target.claim(
            logical_key=self.key(),
            notice_id=self.notice.notice_id,
            revision=1,
            owner_scope="group:101",
            source_scope="group:101",
            target_scope="group:101",
            payload="第一版",
            manual_operation_id=manual,
            now=self.now,
        )

    async def deliver(self, *, sender=None, manual=None):  # type: ignore[no-untyped-def]
        return await self.service.deliver(
            notice_id=self.notice.notice_id,
            revision=1,
            target_scope="group:101",
            owner_scope="group:101",
            sender=sender or self.sender,
            manual_operation_id=manual,
        )

    def restart(self) -> list:
        self.ledger = self._new_ledger("boot-b")
        self.service = self._new_service()
        return self.service.recovered

    # --- 1. claim durability ------------------------------------------
    async def test_claim_is_committed_before_the_network_call(self) -> None:
        self.publish()
        probe = ProbeSender(self.path)
        result = await self.deliver(sender=probe)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(
            probe.observed,
            ["in_flight"],
            "the claim must be visible to other connections before send",
        )
        self.assertEqual(len(probe.calls), 1)

    def test_claim_is_unique_across_connections(self) -> None:
        self.publish()
        first = self.claim()
        self.assertTrue(first.granted)
        self.assertEqual(_attempt_rows(self.path), [(0, "in_flight", None)])

        second_worker = self._new_ledger("boot-b")
        second = self.claim(ledger=second_worker)
        self.assertFalse(second.granted)
        self.assertEqual(second.status, "in_flight")
        self.assertEqual(second.reason, "in_flight")
        # Nothing was sent by either worker yet.
        self.assertEqual(self.sender.calls, [])

    # --- 2. crash windows ---------------------------------------------
    async def test_unknown_claim_blocks_automatic_and_sweep_resend(self) -> None:
        self.publish()
        self.assertTrue(self.claim().granted)
        recovered = self.restart()
        self.assertEqual([a.status for a in recovered], ["unknown"])
        self.assertEqual(recovered[0].reason, LEDGER.CRASH_REASON)

        result = await self.deliver()
        self.assertEqual((result.status, result.reason), ("unknown", "unknown_requires_manual_retry"))
        self.assertFalse(result.sent)

        sweep = await self.service.deliver_due(sender=self.sender)
        self.assertEqual([r.status for r in sweep], ["unknown"])
        self.assertEqual(self.sender.calls, [])

    async def test_crash_before_claim_leaves_no_record_and_allows_retry(self) -> None:
        self.publish()
        original = self.ledger.claim

        def boom(**kwargs: object) -> object:
            raise sqlite3.OperationalError("database is locked")

        self.ledger.claim = boom
        try:
            result = await self.deliver()
        finally:
            self.ledger.claim = original

        self.assertEqual((result.status, result.reason), ("failed", "claim_failed"))
        self.assertFalse(result.sent)
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(_attempt_rows(self.path), [])

        retried = await self.deliver()
        self.assertEqual(retried.status, "accepted")
        self.assertEqual(retried.attempt_no, 0)
        self.assertEqual(len(self.sender.calls), 1)

    async def test_crash_after_send_before_write_back_stays_unknown(self) -> None:
        self.publish()
        original = self.ledger.finish

        def boom(*args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("disk I/O error")

        self.ledger.finish = boom
        try:
            result = await self.deliver()
        finally:
            self.ledger.finish = original

        self.assertEqual(
            (result.status, result.reason), ("unknown", "terminal_write_failed")
        )
        self.assertTrue(result.sent)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual([row[1] for row in _attempt_rows(self.path)], ["in_flight"])

        recovered = self.restart()
        self.assertEqual([a.status for a in recovered], ["unknown"])
        again = await self.deliver()
        self.assertEqual(again.reason, "unknown_requires_manual_retry")
        self.assertEqual(len(self.sender.calls), 1)

    async def test_crash_after_write_back_does_not_resend(self) -> None:
        self.publish()
        first = await self.deliver()
        self.assertEqual(first.status, "accepted")
        self.assertEqual(self.restart(), [])

        again = await self.deliver()
        self.assertEqual((again.status, again.reason), ("accepted", "already_accepted"))
        self.assertEqual(len(self.sender.calls), 1)

    # --- 3. preflight gates -------------------------------------------
    async def test_withdrawn_notice_is_not_sent(self) -> None:
        self.publish()
        self.ledger.withdraw(self.notice.notice_id, now=self.now)
        result = await self.deliver()
        self.assertEqual((result.status, result.reason), ("failed", "notice_withdrawn"))
        self.assertFalse(result.sent)
        self.assertEqual(self.sender.calls, [])

    async def test_unsubscribed_target_is_not_sent(self) -> None:
        self.publish()
        self.ledger.set_subscription("group:101", subscribed=False, now=self.now)
        result = await self.deliver()
        self.assertEqual(
            (result.status, result.reason), ("failed", "subscription_inactive")
        )
        self.assertFalse(result.sent)
        self.assertEqual(self.sender.calls, [])

    async def test_expired_notice_is_not_sent(self) -> None:
        self.publish(
            publish_at=self.now - timedelta(hours=2),
            expires_at=self.now - timedelta(hours=1),
        )
        result = await self.deliver()
        self.assertEqual((result.status, result.reason), ("failed", "notice_expired"))
        self.assertFalse(result.sent)
        self.assertEqual(self.sender.calls, [])

    # --- 4. receipts ---------------------------------------------------
    async def test_zero_message_id_is_unknown_not_delivered(self) -> None:
        self.publish()
        sender = RecordingSender(
            SendReceipt(accepted=True, provider_code="ok", message_id="0")
        )
        result = await self.deliver(sender=sender)
        self.assertEqual((result.status, result.reason), ("unknown", "receipt_unverified"))
        self.assertTrue(result.sent)
        latest = self.ledger.latest_attempt(self.key())
        self.assertEqual(latest.status, "unknown")
        self.assertEqual(len(sender.calls), 1)

    async def test_accepted_is_delivered_exactly_once(self) -> None:
        self.publish()
        first = await self.deliver()
        second = await self.deliver()
        self.assertEqual(first.status, "accepted")
        self.assertEqual((second.status, second.reason), ("accepted", "already_accepted"))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(len(self.ledger.attempts(self.key())), 1)

    async def test_not_started_failure_is_retry_safe_but_not_automatic(self) -> None:
        self.publish()
        sender = RecordingSender(error=DELIVERY.DeliveryNotStartedError("no_adapter"))
        result = await self.deliver(sender=sender)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.reason.startswith("not_started"))
        self.assertTrue(result.retry_safe)

        again = await self.deliver(sender=sender)
        self.assertEqual((again.status, again.reason), ("failed", "provider_failed"))
        self.assertEqual(len(sender.calls), 1)

    async def test_manual_retry_after_unknown_creates_new_attempt(self) -> None:
        self.publish()
        first = await self.deliver(
            sender=RecordingSender(
                SendReceipt(accepted=True, provider_code="ok", message_id="0")
            )
        )
        self.assertEqual(first.status, "unknown")

        second_sender = RecordingSender(
            SendReceipt(accepted=True, provider_code="ok", message_id="2002")
        )
        retried = await self.deliver(sender=second_sender, manual="op-1")
        self.assertEqual(retried.status, "accepted")
        self.assertEqual(retried.attempt_no, 1)
        self.assertEqual(len(second_sender.calls), 1)
        self.assertEqual([a.attempt_no for a in self.ledger.attempts(self.key())], [0, 1])

    # --- 4b. transient gate failures -----------------------------------
    async def test_not_yet_due_is_retryable_once_the_window_opens(self) -> None:
        """A not-yet-due block proves nothing was sent, so it must not burn the slot."""
        self.publish(publish_at=self.now + timedelta(hours=1))
        first = await self.deliver()
        self.assertEqual((first.status, first.reason), ("failed", "not_yet_due"))
        self.assertTrue(first.retry_safe)
        self.assertFalse(first.sent)
        self.assertEqual(self.sender.calls, [])

        self.now += timedelta(hours=2)
        retried = await self.deliver()
        self.assertEqual(retried.status, "accepted")
        self.assertEqual(retried.attempt_no, 1)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(
            [a.status for a in self.ledger.attempts(self.key())],
            ["failed", "accepted"],
        )

    async def test_permanent_gate_failures_still_refuse_retry(self) -> None:
        self.publish()
        self.ledger.withdraw(self.notice.notice_id, now=self.now)
        first = await self.deliver()
        self.assertEqual((first.status, first.reason), ("failed", "notice_withdrawn"))
        self.assertFalse(first.retry_safe)

        again = await self.deliver(manual="op-withdrawn")
        self.assertEqual((again.status, again.reason), ("failed", "retry_denied"))
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(len(self.ledger.attempts(self.key())), 1)

    # --- 5. due sweep --------------------------------------------------
    async def test_due_sweep_sends_due_notice_only(self) -> None:
        self.publish(publish_at=self.now + timedelta(hours=1))
        self.assertEqual(
            await self.service.deliver_due(sender=self.sender, now=self.now), []
        )
        self.assertEqual(self.sender.calls, [])

        self.now += timedelta(hours=2)
        results = await self.service.deliver_due(sender=self.sender, now=self.now)
        self.assertEqual([r.status for r in results], ["accepted"])
        self.assertEqual(len(self.sender.calls), 1)

    # --- 6. no global postprocessor ------------------------------------
    async def test_no_global_postprocessor_sends_notices(self) -> None:
        self.publish()
        import nonebot.message as message_module

        def offenders() -> list[object]:
            return [
                processor
                for processor in message_module._run_postprocessors
                if str(
                    getattr(getattr(processor, "call", None), "__module__", "")
                ).startswith(PLUGIN.__name__)
            ]

        self.assertEqual(
            offenders(), [], "the plugin must not register a run_postprocessor"
        )
        self.assertFalse(hasattr(PLUGIN, "postprocess_notice"))

        # Positive control: the detector has to recognise a processor that
        # carries this plugin's module, otherwise the empty list above would be
        # green even for a filter that can never match anything.
        async def fake_postprocessor(
            _bot: object, _event: object, _state: object
        ) -> None:
            return None

        fake_postprocessor.__module__ = PLUGIN.__name__

        class FakeProcessor:
            # ``_run_postprocessors`` is a set, so the stand-in must be
            # hashable; ``SimpleNamespace`` is not.
            def __init__(self, call: object) -> None:
                self.call = call

        processor = FakeProcessor(fake_postprocessor)
        message_module._run_postprocessors.add(processor)
        try:
            self.assertEqual(
                offenders(), [processor], "检测器必须能命中本插件的 processor"
            )
        finally:
            message_module._run_postprocessors.discard(processor)
        self.assertEqual(offenders(), [])


if __name__ == "__main__":
    unittest.main()
