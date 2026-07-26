import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from support import add_repo_paths

add_repo_paths()

from logic import get_content_hash
from plugin_loader import load_plugin
from storage import load_history, load_notices, save_json


class CommandFinishedError(Exception):
    def __init__(self, message: str | None) -> None:
        super().__init__(message)
        self.message = message


class FakeEvent:
    def __init__(self, text: str = "", user_id: int = 1) -> None:
        self.text = text
        self.user_id = user_id
        self.self_id = "bot-r009"
        self.group_id = "group-r009"

    def get_plaintext(self) -> str:
        return self.text


class FakeGroupEvent(FakeEvent):
    def __init__(self, group_id: int, user_id: int = 1) -> None:
        super().__init__(user_id=user_id)
        self.group_id = group_id


class FakeBot:
    def __init__(
        self,
        *,
        fail_times: int = 0,
        group_list: list[dict[str, object]] | None = None,
        yield_on_send: bool = False,
    ) -> None:
        self.fail_times = fail_times
        self.group_list = group_list or []
        self.yield_on_send = yield_on_send
        self.sent: list[tuple[object, str]] = []

    async def send(self, event: object, message: str) -> None:
        self.sent.append((event, message))
        if self.yield_on_send:
            await asyncio.sleep(0)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError

    async def get_group_list(self) -> list[dict[str, object]]:
        return self.group_list


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
        self.notices_file = root / "notices.json"
        self.history_file = root / "sent_history.json"
        self.plugin.NOTICES_FILE = self.notices_file
        self.plugin.HISTORY_FILE = self.history_file
        self.plugin._sending_lock.clear()
        self.plugin.GroupMessageEvent = FakeGroupEvent

    def tearDown(self) -> None:
        self.plugin._sending_lock.clear()
        self.temp_dir.cleanup()

    def test_announcement_matcher_does_not_block_unrelated_plugins(self) -> None:
        self.assertFalse(self.plugin.notice_manage.block)

    async def run_command(
        self,
        text: str,
        *,
        superuser: bool = True,
        user_id: int = 1,
        bot: FakeBot | None = None,
    ) -> str | None:
        original_superuser = self.plugin.SUPERUSER
        original_finish = self.plugin.notice_manage.finish

        async def permission(_bot: object, _event: object) -> bool:
            return superuser

        async def finish(message: str | None = None) -> None:
            raise CommandFinishedError(message)

        self.plugin.SUPERUSER = permission
        self.plugin.notice_manage.finish = finish
        try:
            await self.handler(bot or FakeBot(), FakeEvent(text, user_id=user_id))
        except CommandFinishedError as exc:
            return exc.message
        finally:
            self.plugin.SUPERUSER = original_superuser
            self.plugin.notice_manage.finish = original_finish
        return None

    async def test_management_validation_permission_and_limit(self) -> None:
        self.assertEqual(
            await self.run_command("公告 查看"), "当前暂无公告。"
        )
        self.assertEqual(await self.run_command("公告 增加 first"), "写入成功。")
        self.assertEqual(load_notices(self.notices_file), ["first"])

        self.assertEqual(
            await self.run_command("公告 修改 invalid second"), "序号错误"
        )
        self.assertEqual(
            await self.run_command("公告 修改 99 second"), "序号错误"
        )
        self.assertEqual(
            await self.run_command("公告 删除 invalid"), "序号错误"
        )
        self.assertEqual(
            await self.run_command("公告 增加 denied", superuser=False),
            "你没有权限管理公告。",
        )

        save_json(self.notices_file, ["one", "two", "three", "four", "five"])
        self.assertEqual(
            await self.run_command("公告 增加 sixth"), "位置已满(5/5)"
        )

    async def test_configured_canonical_id_can_manage_virtual_session(self) -> None:
        original_resolver = self.plugin.get_real_qq
        original_admin_ids = self.plugin.NOTICE_ADMIN_QQ_IDS
        self.plugin.NOTICE_ADMIN_QQ_IDS = {"canonical-r009"}
        self.plugin.get_real_qq = lambda user_id: (
            "canonical-r009" if user_id == "user-openid-r009" else None
        )
        try:
            self.assertEqual(
                await self.run_command(
                    "公告 增加 from-real-qq",
                    superuser=False,
                    user_id="user-openid-r009",
                ),
                "写入成功。",
            )
        finally:
            self.plugin.get_real_qq = original_resolver
            self.plugin.NOTICE_ADMIN_QQ_IDS = original_admin_ids

    async def test_permission_provider_receives_scoped_release009_identity(self) -> None:
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
                return SimpleNamespace(canonical_user_id="canonical-r009")

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
            async def call_provider_safe(func: object, *args: object, **_: object) -> object:
                value = await func(*args)
                return SimpleNamespace(success=True, value=value)

        original_core = sys.modules.get("amia_core")
        original_admin_ids = self.plugin.NOTICE_ADMIN_QQ_IDS
        self.plugin.NOTICE_ADMIN_QQ_IDS = set()
        sys.modules["amia_core"] = Core
        try:
            self.assertEqual(
                await self.run_command(
                    "公告 增加 provider-authorized",
                    superuser=False,
                    user_id="user-openid-r009",
                ),
                "写入成功。",
            )
        finally:
            self.plugin.NOTICE_ADMIN_QQ_IDS = original_admin_ids
            if original_core is None:
                sys.modules.pop("amia_core", None)
            else:
                sys.modules["amia_core"] = original_core

        key = calls["key"]
        assert isinstance(key, SimpleNamespace)
        self.assertEqual(key.self_id, "bot-r009")
        self.assertEqual(key.user_id, "user-openid-r009")
        self.assertEqual(calls["permission"], "group.notice.manage")
        self.assertEqual(calls["context_id"], "onebot:bot-r009:group:group-r009")
        self.assertEqual(calls["identity"].canonical_user_id, "canonical-r009")

    async def test_view_edit_delete_and_unknown_command(self) -> None:
        save_json(self.notices_file, ["first", "second"])
        list_message = await self.run_command("公告 查看")
        full_message = await self.run_command("公告 查看 2")
        unknown_message = await self.run_command("公告 其他")
        self.assertIsNotNone(list_message)
        self.assertIsNotNone(full_message)
        self.assertIsNotNone(unknown_message)
        assert list_message is not None
        assert full_message is not None
        assert unknown_message is not None
        self.assertIn("公告列表", list_message)
        self.assertIn("second", full_message)
        self.assertEqual(
            await self.run_command("公告 查看 text"), "序号错误。"
        )
        self.assertEqual(
            await self.run_command("公告 修改 2 updated"), "公告 2 已修改。"
        )
        self.assertEqual(load_notices(self.notices_file), ["first", "updated"])
        self.assertEqual(await self.run_command("公告 删除 1"), "已删除。")
        self.assertEqual(load_notices(self.notices_file), ["updated"])
        self.assertIn("未知公告操作", unknown_message)

    async def test_stats_counts_current_groups_and_requires_superuser(self) -> None:
        latest_hash = get_content_hash("latest")
        save_json(self.notices_file, ["latest"])
        save_json(
            self.history_file,
            {
                "group:101": latest_hash,
                "group:999": latest_hash,
                "private:1": latest_hash,
            },
        )
        bot = FakeBot(group_list=[{"group_id": 101}, {"group_id": 102}])
        self.assertEqual(
            await self.run_command("公告 统计", bot=bot), "最新公告覆盖: 1 / 2"
        )
        self.assertEqual(
            await self.run_command("公告 统计", superuser=False),
            "你没有权限查看公告统计。",
        )

    async def test_postprocessor_sends_once_and_resends_new_version(self) -> None:
        save_json(self.notices_file, ["v1"])
        bot = FakeBot()
        matcher = SimpleNamespace(plugin_name="other")
        event = FakeGroupEvent(101)

        await self.plugin.postprocess_notice(bot, event, matcher, None)
        await self.plugin.postprocess_notice(bot, event, matcher, None)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(
            load_history(self.history_file),
            {"group:101": get_content_hash("v1")},
        )

        save_json(self.notices_file, ["v2"])
        await self.plugin.postprocess_notice(bot, event, matcher, None)
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(
            load_history(self.history_file),
            {"group:101": get_content_hash("v2")},
        )

    async def test_postprocessor_keeps_group_and_private_history_separate(self) -> None:
        save_json(self.notices_file, ["latest"])
        bot = FakeBot()
        matcher = SimpleNamespace(plugin_name="other")
        await self.plugin.postprocess_notice(bot, FakeGroupEvent(101), matcher, None)
        await self.plugin.postprocess_notice(bot, FakeEvent(user_id=101), matcher, None)

        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(
            set(load_history(self.history_file)),
            {"group:101", "private:101"},
        )

    async def test_postprocessor_releases_lock_after_failure(self) -> None:
        save_json(self.notices_file, ["latest"])
        bot = FakeBot(fail_times=1)
        matcher = SimpleNamespace(plugin_name="other")
        event = FakeGroupEvent(101)

        await self.plugin.postprocess_notice(bot, event, matcher, None)
        self.assertNotIn("group:101", self.plugin._sending_lock)
        await self.plugin.postprocess_notice(bot, event, matcher, None)
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(
            load_history(self.history_file),
            {"group:101": get_content_hash("latest")},
        )

    async def test_postprocessor_merges_history_for_concurrent_targets(self) -> None:
        save_json(self.notices_file, ["latest"])
        bot = FakeBot(yield_on_send=True)
        matcher = SimpleNamespace(plugin_name="other")

        await asyncio.gather(
            self.plugin.postprocess_notice(bot, FakeGroupEvent(101), matcher, None),
            self.plugin.postprocess_notice(bot, FakeGroupEvent(102), matcher, None),
        )

        self.assertEqual(
            set(load_history(self.history_file)),
            {"group:101", "group:102"},
        )

    async def test_postprocessor_skips_failed_and_self_matchers(self) -> None:
        save_json(self.notices_file, ["latest"])
        bot = FakeBot()
        event = FakeGroupEvent(101)

        await self.plugin.postprocess_notice(
            bot, event, SimpleNamespace(plugin_name="other"), RuntimeError()
        )
        await self.plugin.postprocess_notice(
            bot, event, self.plugin.notice_manage, None
        )
        await self.plugin.postprocess_notice(
            bot, event, SimpleNamespace(plugin_name="公告"), None
        )
        self.assertEqual(bot.sent, [])

    async def test_same_target_is_deduplicated_while_send_is_in_flight(self) -> None:
        save_json(self.notices_file, ["latest"])
        bot = FakeBot(yield_on_send=True)
        matcher = SimpleNamespace(plugin_name="other")
        event = FakeGroupEvent(101)

        await asyncio.gather(
            self.plugin.postprocess_notice(bot, event, matcher, None),
            self.plugin.postprocess_notice(bot, event, matcher, None),
        )

        self.assertEqual(len(bot.sent), 1)

    async def test_save_failure_does_not_report_success_or_keep_lock(self) -> None:
        save_json(self.notices_file, ["latest"])
        bot = FakeBot()
        matcher = SimpleNamespace(plugin_name="other")
        original_record = self.plugin.record_history

        def fail_record(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        self.plugin.record_history = fail_record
        try:
            await self.plugin.postprocess_notice(
                bot, FakeGroupEvent(101), matcher, None
            )
        finally:
            self.plugin.record_history = original_record

        self.assertEqual(len(bot.sent), 1)
        self.assertNotIn("group:101", self.plugin._sending_lock)
        self.assertEqual(load_history(self.history_file), {})


if __name__ == "__main__":
    unittest.main()
