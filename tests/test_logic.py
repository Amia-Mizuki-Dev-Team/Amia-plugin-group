import unittest
from datetime import datetime, timezone

from support import add_repo_paths

add_repo_paths()

from logic import (
    MAX_NOTICE_LENGTH,
    MAX_NOTICES,
    classify_provider_receipt,
    get_content_hash,
    logical_delivery_key,
    parse_notice_command,
    parse_notice_index,
    parse_schedule,
    parse_target_scope,
    payload_digest,
    render_plain_notice,
    subscription_coverage,
    target_key,
    valid_notice_content,
    verifiable_message_id,
)


class TestNoticeLogic(unittest.TestCase):
    def test_parse_notice_index_is_one_based_and_bounded(self) -> None:
        self.assertEqual(parse_notice_index("1", 3), 0)
        self.assertEqual(parse_notice_index(chr(0xFF13), 3), 2)
        self.assertIsNone(parse_notice_index("0", 3))
        self.assertIsNone(parse_notice_index("4", 3))
        self.assertIsNone(parse_notice_index("1_0", 3))
        self.assertIsNone(parse_notice_index("²", 3))
        self.assertIsNone(parse_notice_index("text", 3))

    def test_subscription_coverage_counts_subscribed_targets_only(self) -> None:
        # The denominator is the subscription table, never the bot's group
        # list: a group the bot can see is not a group that asked for notices.
        sent, total = subscription_coverage(
            ["group:101", "group:102", "private:1"],
            {
                "group:101",
                "group:999",
                "private:1",
                "not-a-scope",
            },
        )
        self.assertEqual((sent, total), (2, 3))

    def test_coverage_ignores_malformed_subscription_entries(self) -> None:
        self.assertEqual(
            subscription_coverage(
                [None, 5, "group:1"],
                {"group:1", "group:2"},
            ),
            (1, 1),
        )

    def test_notice_limit_matches_runtime_contract(self) -> None:
        self.assertEqual(MAX_NOTICES, 5)

    def test_command_parser_defaults_to_view_and_preserves_content(self) -> None:
        self.assertEqual(
            parse_notice_command("  公告   "),
            parse_notice_command("公告 查看"),
        )
        self.assertEqual(
            parse_notice_command("公告 修改 2  含有  多个空格的内容").argument,
            "2  含有  多个空格的内容",
        )
        self.assertIsNone(parse_notice_command("不是公告"))

    def test_target_keys_are_namespaced(self) -> None:
        self.assertEqual(target_key(group_id=42), "group:42")
        self.assertEqual(target_key(user_id=42), "private:42")
        self.assertNotEqual(target_key(group_id=42), target_key(user_id=42))
        with self.assertRaises(ValueError):
            target_key()

    def test_content_validation_rejects_blank_and_oversized_values(self) -> None:
        self.assertFalse(valid_notice_content(None))
        self.assertFalse(valid_notice_content("   "))
        self.assertTrue(valid_notice_content("ok"))
        self.assertFalse(valid_notice_content("x" * (MAX_NOTICE_LENGTH + 1)))

    def test_target_scope_parser_requires_qualified_scope(self) -> None:
        self.assertEqual(parse_target_scope("group:101"), "group:101")
        self.assertEqual(parse_target_scope(" private:7 "), "private:7")
        self.assertIsNone(parse_target_scope("101"))
        self.assertIsNone(parse_target_scope("group:"))
        self.assertIsNone(parse_target_scope("group:1:2"))
        self.assertIsNone(parse_target_scope("channel:1"))
        self.assertIsNone(parse_target_scope("group:1 2"))
        self.assertIsNone(parse_target_scope(None))

    def test_logical_delivery_key_is_deterministic_and_target_scoped(self) -> None:
        first = logical_delivery_key(
            owner_scope="group:1",
            source_scope="group:1",
            target_scope="group:2",
            notice_id="n1",
            revision=3,
        )
        self.assertEqual(
            first,
            logical_delivery_key(
                owner_scope="group:1",
                source_scope="group:1",
                target_scope="group:2",
                notice_id="n1",
                revision=3,
            ),
        )
        self.assertNotEqual(
            first,
            logical_delivery_key(
                owner_scope="group:1",
                source_scope="group:1",
                target_scope="group:3",
                notice_id="n1",
                revision=3,
            ),
        )
        self.assertIn("n1@3", first)
        self.assertIn("target=group:2", first)

    def test_verifiable_message_id_rejects_zero_and_empty_values(self) -> None:
        for value in (None, "", "  ", "0", 0, -1, True, False):
            self.assertFalse(verifiable_message_id(value), value)
        for value in ("1001", 42, " msg-1 "):
            self.assertTrue(verifiable_message_id(value), value)

    def test_classify_provider_receipt_requires_verifiable_id(self) -> None:
        self.assertEqual(
            classify_provider_receipt(accepted=True, message_id="1001"), "accepted"
        )
        self.assertEqual(
            classify_provider_receipt(accepted=True, message_id="0"), "unknown"
        )
        self.assertEqual(
            classify_provider_receipt(accepted=True, message_id=None), "unknown"
        )
        self.assertEqual(
            classify_provider_receipt(accepted=False, message_id="1001"), "failed"
        )

    def test_parse_schedule_returns_aware_utc(self) -> None:
        parsed = parse_schedule("2030-01-01T00:00")
        assert parsed is not None
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(parsed.astimezone().hour, 0)
        explicit = parse_schedule("2030-01-01T00:00+08:00")
        self.assertEqual(explicit, datetime(2029, 12, 31, 16, tzinfo=timezone.utc))
        self.assertIsNone(parse_schedule("not-a-time"))
        self.assertIsNone(parse_schedule(None))

    def test_payload_digest_is_stable_and_content_sensitive(self) -> None:
        self.assertEqual(payload_digest("body"), payload_digest("body"))
        self.assertNotEqual(payload_digest("body"), payload_digest("body "))
        self.assertNotEqual(payload_digest("body"), get_content_hash("body"))
        self.assertEqual(len(payload_digest("body")), 64)

    def test_render_plain_notice_is_plain_text_only(self) -> None:
        rendered = render_plain_notice("第一行\n第二行")
        self.assertTrue(rendered.startswith("【Mizuki 公告】"))
        self.assertIn("第一行\n第二行", rendered)
        self.assertNotIn("<", rendered)


if __name__ == "__main__":
    unittest.main()
