import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from support import add_repo_paths

add_repo_paths()

from plugin_loader import load_plugin
from storage import save_json


class CommandFinishedError(Exception):
    def __init__(self, message: str | None) -> None:
        super().__init__(message)
        self.message = message


class FakeEvent:
    def __init__(self, text: str = "", user_id: int = 1) -> None:
        self.text = text
        self.user_id = user_id
        self.self_id = "bot-r010"
        self.group_id = "group-r010"

    def get_plaintext(self) -> str:
        return self.text


class FakeGroupEvent(FakeEvent):
    def __init__(self, group_id: int, user_id: int = 1) -> None:
        super().__init__(user_id=user_id)
        self.group_id = group_id


class FakeBot:
    """OneBot v11 double that records every attempt to send."""

    def __init__(self, *, fail_times: int = 0, message_id: object = 1001) -> None:
        self.fail_times = fail_times
        self.message_id = message_id
        self.sent: list[tuple[str, object, str]] = []

    async def send_group_msg(
        self, *, group_id: int, message: str
    ) -> dict[str, object]:
        self.sent.append(("group", group_id, message))
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError
        return {"status": "ok", "message_id": self.message_id}

    async def send_private_msg(
        self, *, user_id: int, message: str
    ) -> dict[str, object]:
        self.sent.append(("private", user_id, message))
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError
        return {"status": "ok", "message_id": self.message_id}

    async def get_group_list(self) -> list[dict[str, object]]:
        raise AssertionError("get_group_list must not be used as delivery truth")


class _Sender:
    """Minimal NoticeSender adapter over the fake bot."""

    def __init__(self, bot: FakeBot, plugin: Any) -> None:
        self._bot = bot
        self._plugin = plugin

    async def send(
        self, *, target_scope: str, content: str, notice_id: str, revision: int
    ) -> Any:
        kind, _, identifier = target_scope.partition(":")
        if kind == "group":
            result = await self._bot.send_group_msg(
                group_id=int(identifier), message=content
            )
        else:
            result = await self._bot.send_private_msg(
                user_id=int(identifier), message=content
            )
        return self._plugin.receipt_from_onebot_result(result)


class TestNoticeRuntime(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin: Any = load_plugin()
        cls.handler = staticmethod(cls.plugin.notice_manage.handlers[0].call)
        cls.original_group_message_event = cls.plugin.GroupMessageEvent

    @classmethod
    def tearDownClass(cls) -> None:
        cls.plugin.GroupMessageEvent = cls.original_group_message_event

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.plugin.LEDGER_FILE = root / "ledger.sqlite3"
        self.plugin.LEGACY_NOTICES_FILE = root / "notices.json"
        self.plugin.reset_runtime_state()
        self.plugin.GroupMessageEvent = FakeGroupEvent
        self.bot = FakeBot()
        self.event = FakeGroupEvent(101)

    def tearDown(self) -> None:
        self.plugin.reset_runtime_state()
        self.temp_dir.cleanup()

    def test_announcement_matcher_does_not_block_unrelated_plugins(self) -> None:
        self.assertFalse(self.plugin.notice_manage.block)

    async def run_command(
        self,
        text: str,
        *,
        superuser: bool = True,
        user_id: object = 1,
        bot: FakeBot | None = None,
        event: FakeEvent | None = None,
    ) -> str | None:
        original_superuser = self.plugin.SUPERUSER
        original_finish = self.plugin.notice_manage.finish

        async def permission(_bot: object, _event: object) -> bool:
            return superuser

        async def finish(message: str | None = None) -> None:
            raise CommandFinishedError(message)

        self.plugin.SUPERUSER = permission
        self.plugin.notice_manage.finish = finish
        target_event = event or FakeEvent(user_id=user_id)
        target_event.text = text
        try:
            await self.handler(bot or self.bot, target_event)
        except CommandFinishedError as exc:
            return exc.message
        finally:
            self.plugin.SUPERUSER = original_superuser
            self.plugin.notice_manage.finish = original_finish
        return None

    # --- management -----------------------------------------------------
    async def test_management_validation_permission_and_limit(self) -> None:
        self.assertEqual(await self.run_command("公告 查看"), "当前暂无公告。")
        self.assertEqual(await self.run_command("公告 增加 first"), "写入成功。")
        notices = self.plugin._get_ledger().list_notices()
        self.assertEqual([n.content for n in notices], ["first"])

        self.assertEqual(
            await self.run_command("公告 修改 invalid second"), "序号错误"
        )
        self.assertEqual(await self.run_command("公告 修改 99 second"), "序号错误")
        self.assertEqual(await self.run_command("公告 删除 invalid"), "序号错误")
        self.assertEqual(
            await self.run_command("公告 增加 denied", superuser=False),
            "你没有权限管理公告。",
        )
        for number in range(2, 6):
            await self.run_command(f"公告 增加 draft-{number}")
        self.assertEqual(
            await self.run_command("公告 增加 sixth"), "位置已满(5/5)"
        )

    async def test_configured_canonical_id_can_manage_virtual_session(self) -> None:
        original_resolver = self.plugin.get_real_qq
        original_admin_ids = self.plugin.NOTICE_ADMIN_QQ_IDS
        self.plugin.NOTICE_ADMIN_QQ_IDS = {"canonical-r010"}
        self.plugin.get_real_qq = lambda user_id: (
            "canonical-r010" if user_id == "user-openid-r010" else None
        )
        try:
            self.assertEqual(
                await self.run_command(
                    "公告 增加 from-real-qq",
                    superuser=False,
                    user_id="user-openid-r010",
                ),
                "写入成功。",
            )
        finally:
            self.plugin.get_real_qq = original_resolver
            self.plugin.NOTICE_ADMIN_QQ_IDS = original_admin_ids

    async def test_permission_provider_receives_scoped_release010_identity(
        self,
    ) -> None:
        calls: dict[str, object] = {}

        class PermissionProvider:
            async def has_permission(
                self, identity: object, permission: str, context_id: str
            ) -> bool:
                calls["identity"] = identity
                calls["permission"] = permission
                calls["context_id"] = context_id
                return True

        class IdentityResolver:
            async def resolve_identity(self, key: object) -> object:
                calls["key"] = key
                return SimpleNamespace(canonical_user_id="canonical-r010")

        class Registry:
            permission = PermissionProvider()
            resolver = IdentityResolver()

            def get_permission_provider(self, name: str) -> object | None:
                return self.permission if name == "static" else None

            def get_identity_resolver(self) -> object:
                return self.resolver

            def get_audit_logger(self, name: str) -> None:
                return None

        class Core:
            registry = Registry()

            @staticmethod
            def UserIdentityKey(**kwargs: str) -> object:
                return SimpleNamespace(**kwargs)

            @staticmethod
            async def call_provider_safe(
                func: object, *args: object, **_: object
            ) -> object:
                value = await func(*args)
                return SimpleNamespace(success=True, value=value)

        import sys as _sys

        original_core = _sys.modules.get("amia_core")
        original_admin_ids = self.plugin.NOTICE_ADMIN_QQ_IDS
        self.plugin.NOTICE_ADMIN_QQ_IDS = set()
        _sys.modules["amia_core"] = Core
        try:
            self.assertEqual(
                await self.run_command(
                    "公告 增加 provider-authorized",
                    superuser=False,
                    user_id="user-openid-r010",
                    event=FakeGroupEvent(101, user_id="user-openid-r010"),
                ),
                "写入成功。",
            )
        finally:
            self.plugin.NOTICE_ADMIN_QQ_IDS = original_admin_ids
            if original_core is None:
                _sys.modules.pop("amia_core", None)
            else:
                _sys.modules["amia_core"] = original_core

        key = calls["key"]
        assert isinstance(key, SimpleNamespace)
        self.assertEqual(key.self_id, "bot-r010")
        self.assertEqual(key.user_id, "user-openid-r010")
        self.assertEqual(calls["permission"], "group.notice.manage")
        self.assertEqual(calls["context_id"], "onebot:bot-r010:group:101")
        self.assertEqual(calls["identity"].canonical_user_id, "canonical-r010")

    async def test_view_edit_delete_and_unknown_command(self) -> None:
        event = self.event
        await self.run_command("公告 增加 first", event=event)
        await self.run_command("公告 增加 second", event=event)
        list_message = await self.run_command("公告 查看", event=event)
        full_message = await self.run_command("公告 查看 2", event=event)
        unknown_message = await self.run_command("公告 其他", event=event)
        assert list_message is not None
        assert full_message is not None
        assert unknown_message is not None
        self.assertIn("公告列表", list_message)
        self.assertIn("second", full_message)
        self.assertEqual(
            await self.run_command("公告 查看 text", event=event), "序号错误。"
        )
        self.assertEqual(
            await self.run_command("公告 修改 2 updated", event=event),
            "公告 2 已修改（r1）。",
        )
        self.assertEqual(
            [n.content for n in self.plugin._get_ledger().list_notices()],
            ["first", "updated"],
        )
        self.assertEqual(await self.run_command("公告 删除 1", event=event), "已删除。")
        self.assertIn("未知公告操作", unknown_message)

    async def test_stats_counts_subscribed_targets_and_requires_superuser(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        await self.run_command("公告 订阅", event=event)
        await self.run_command("公告 发布 1", event=event)
        self.assertEqual(
            await self.run_command("公告 统计", event=event),
            "最新公告投递: 1 / 1 个已订阅目标",
        )
        self.assertEqual(
            await self.run_command("公告 统计", superuser=False, event=event),
            "你没有权限查看公告统计。",
        )

    # --- explicit delivery ---------------------------------------------
    async def test_query_commands_never_send(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        await self.run_command("公告 订阅", event=event)
        await self.run_command("公告 发布 1", event=event)
        self.assertEqual(len(self.bot.sent), 1)

        await self.run_command("公告 查看", event=event)
        await self.run_command("公告 查看 1", event=event)
        await self.run_command("公告 状态", event=event)
        await self.run_command("公告 统计", event=event)
        self.assertEqual(len(self.bot.sent), 1, "查询类操作不得发送公告")

    async def test_publish_delivers_once_and_reports_accepted(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        await self.run_command("公告 订阅", event=event)
        first = await self.run_command("公告 发布 1", event=event)
        assert first is not None
        self.assertIn("已发布", first)
        self.assertIn("已受理", first)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("【Mizuki 公告】", self.bot.sent[0][2])

        second = await self.run_command("公告 发布 1", event=event)
        assert second is not None
        self.assertIn("此前已受理，本次不重发", second)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_publish_requires_target_subscription(self) -> None:
        await self.run_command("公告 增加 latest", event=self.event)
        message = await self.run_command("公告 发布 1", event=self.event)
        self.assertEqual(message, "目标未订阅公告，未发送。")
        self.assertEqual(self.bot.sent, [])

    async def test_republish_from_another_scope_does_not_send_twice(self) -> None:
        """Publishing an already published notice from group:B must not resend.

        The logical delivery key carries the owner scope, so the owner must be
        the notice's origin scope.  Using the caller's scope instead mints a
        second key for the same revision and target, which the ledger cannot
        recognise as an already accepted delivery.
        """

        await self.run_command("公告 增加 latest", event=self.event)
        await self.run_command("公告 订阅", event=self.event)
        await self.run_command("公告 发布 1", event=self.event)
        self.assertEqual(len(self.bot.sent), 1)

        other_scope_event = FakeGroupEvent(202)
        message = await self.run_command(
            "公告 发布 1 group:101", event=other_scope_event
        )
        assert message is not None
        self.assertIn("此前已受理，本次不重发", message)
        self.assertEqual(len(self.bot.sent), 1, "同 revision 同目标不得二次群发")
        ledger = self.plugin._get_ledger()
        notice = ledger.list_notices()[0]
        self.assertEqual(
            len(ledger.attempts_for_notice(notice.notice_id, notice.revision)),
            1,
            "二次发布不得新增投递尝试",
        )

    async def test_publish_blocks_cross_group_without_target_permission(
        self,
    ) -> None:
        await self.run_command("公告 增加 latest", event=self.event)
        self.plugin._get_ledger().set_subscription("group:202", subscribed=True)

        async def scoped_permission(
            _bot: object, _event: object, scope: str | None = None
        ) -> bool:
            return (scope or "group:101") == "group:101"

        original = self.plugin._has_scope_permission
        self.plugin._has_scope_permission = scoped_permission
        try:
            message = await self.run_command("公告 发布 1 group:202", event=self.event)
        finally:
            self.plugin._has_scope_permission = original
        self.assertEqual(message, "你没有目标群的公告管理权限。")
        self.assertEqual(self.bot.sent, [])

    async def test_superuser_cannot_push_to_unsubscribed_other_group(self) -> None:
        await self.run_command("公告 增加 latest", event=self.event)
        await self.run_command("公告 订阅", event=self.event)
        message = await self.run_command("公告 发布 1 group:202", event=self.event)
        self.assertEqual(message, "目标未订阅公告，未发送。")
        self.assertEqual(self.bot.sent, [])

    async def test_subscribe_unsubscribe_toggle_and_block_delivery(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        self.assertEqual(await self.run_command("公告 订阅", event=event), "本目标已订阅公告。")
        self.assertEqual(
            self.plugin._get_ledger().subscription_state("group:101"), "subscribed"
        )
        self.assertEqual(
            await self.run_command("公告 退订", event=event),
            "本目标已退订公告，未发送的投递会被拦截。",
        )
        self.assertEqual(
            self.plugin._get_ledger().subscription_state("group:101"), "unsubscribed"
        )

    async def test_withdraw_blocks_scheduled_delivery(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        await self.run_command("公告 订阅", event=event)
        await self.run_command("公告 计划 1 2020-01-01T00:00", event=event)
        self.assertEqual(
            await self.run_command("公告 撤回 1", event=event),
            "公告 1 已撤回，尚未发送的投递会被拦截。",
        )
        results = await self.plugin.run_due_deliveries(
            sender=_Sender(self.bot, self.plugin)
        )
        self.assertEqual(results, [])
        self.assertEqual(self.bot.sent, [])

    async def test_schedule_reply_states_external_trigger_is_required(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        await self.run_command("公告 订阅", event=event)
        message = await self.run_command("公告 计划 1 2030-01-01T00:00", event=event)
        assert message is not None
        self.assertIn("不会自动发送", message)
        self.assertIn("公告 投递", message)
        self.assertEqual(self.bot.sent, [])

    async def test_deliver_command_sends_only_due_notices(self) -> None:
        event = self.event
        await self.run_command("公告 增加 due-now", event=event)
        await self.run_command("公告 订阅", event=event)
        await self.run_command("公告 计划 1 2020-01-01T00:00", event=event)
        message = await self.run_command("公告 投递", event=event)
        assert message is not None
        self.assertIn("已受理 1", message)
        self.assertIn("• group:101", message, "汇总必须点名目标作用域")
        self.assertEqual(len(self.bot.sent), 1)

        await self.run_command("公告 增加 future", event=event)
        await self.run_command("公告 计划 2 2030-01-01T00:00", event=event)
        await self.run_command("公告 撤回 1", event=event)
        future_message = await self.run_command("公告 投递", event=event)
        assert future_message is not None
        self.assertIn("没有到期", future_message)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_import_legacy_notices_as_drafts_only(self) -> None:
        save_json(self.plugin.LEGACY_NOTICES_FILE, ["legacy-a", "legacy-b"])
        message = await self.run_command("公告 导入", event=self.event)
        assert message is not None
        self.assertIn("已导入 2 条", message)
        notices = self.plugin._get_ledger().list_notices()
        self.assertEqual([n.state for n in notices], ["draft", "draft"])
        self.assertEqual(self.bot.sent, [])

    async def test_redeliver_after_unknown_creates_new_attempt(self) -> None:
        event = self.event
        await self.run_command("公告 增加 latest", event=event)
        await self.run_command("公告 订阅", event=event)
        unverifiable = FakeBot(message_id="0")
        published = await self.run_command("公告 发布 1", event=event, bot=unverifiable)
        assert published is not None
        self.assertIn("结果未知", published)
        self.assertEqual(len(unverifiable.sent), 1)
        self.assertEqual(self.bot.sent, [])

        message = await self.run_command("公告 重投 1", event=event)
        assert message is not None
        self.assertIn("重投可能导致重复发送", message)
        ledger = self.plugin._get_ledger()
        notice = ledger.list_notices()[0]
        key = self.plugin.logical_delivery_key(
            owner_scope="group:101",
            source_scope="group:101",
            target_scope="group:101",
            notice_id=notice.notice_id,
            revision=notice.revision,
        )
        attempts = ledger.attempts(key)
        self.assertEqual([a.attempt_no for a in attempts], [0, 1])
        self.assertEqual([a.status for a in attempts], ["unknown", "accepted"])
        self.assertEqual(len(self.bot.sent), 1)

    async def test_redeliver_draft_is_refused_without_attempt(self) -> None:
        event = self.event
        await self.run_command("公告 增加 draft-only", event=event)
        message = await self.run_command("公告 重投 1", event=event)
        assert message is not None
        self.assertIn("草稿", message)
        ledger = self.plugin._get_ledger()
        notice = ledger.list_notices()[0]
        self.assertEqual(notice.state, "draft")
        self.assertEqual(
            ledger.attempts_for_notice(notice.notice_id, notice.revision), []
        )
        self.assertEqual(self.bot.sent, [])

    async def test_scheduled_notice_waits_for_the_explicit_command(self) -> None:
        event = self.event
        await self.run_command("公告 增加 scheduled", event=event)
        await self.run_command("公告 订阅", event=event)
        await self.run_command("公告 计划 1 2020-01-01T00:00", event=event)
        self.assertEqual(self.bot.sent, [])

        # A published, already-due notice stays unsent until the operator asks
        # for the sweep: no startup hook and no other command may deliver it.
        for text in ("公告 查看", "公告 状态", "公告 统计", "公告 增加 other"):
            await self.run_command(text, event=event)
        await self.run_command("公告 删除 2", event=event)
        self.assertEqual(self.bot.sent, [], "到期公告只能由显式指令投递")

        message = await self.run_command("公告 投递", event=event)
        assert message is not None
        self.assertIn("已受理 1", message)
        self.assertIn("• group:101", message, "汇总必须点名目标作用域")
        self.assertEqual(len(self.bot.sent), 1)


if __name__ == "__main__":
    unittest.main()
