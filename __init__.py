# ruff: noqa: N999 - the repository name is intentionally hyphenated.

"""Mizuki 群公告插件。

公告只由显式投递动作触发：发布、管理员执行「公告 投递」、人工重投。
插件不注册 startup/shutdown 调度，也没有全局 postprocessor：别的命令跑完
不会顺手补发公告，查询类操作一律不发送。
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from nonebot import logger, on_message, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, get_plugin

from .delivery import (
    MANUAL_RETRY_WARNING,
    DeliveryResult,
    NoticeDeliveryService,
    NoticeSender,
    OneBotNoticeSender,
    describe,
    receipt_from_onebot_result,
)
from .ledger import (
    LedgerError,
    LedgerUnreadableError,
    NoticeLedger,
    NoticeLimitReachedError,
    NoticeMissingError,
    NoticeRecord,
    NoticeStateError,
)
from .logic import (
    MAX_NOTICE_LENGTH,
    MAX_NOTICES,
    group_scope,
    logical_delivery_key,
    parse_notice_command,
    parse_notice_index,
    parse_schedule,
    parse_target_scope,
    private_scope,
    scope_kind,
    subscription_coverage,
    valid_notice_content,
)
from .storage import load_notices as load_legacy_notices

MANAGEMENT_OPERATIONS = frozenset(
    {
        "增加",
        "修改",
        "删除",
        "发布",
        "计划",
        "投递",
        "撤回",
        "重投",
        "订阅",
        "退订",
        "导入",
    }
)
MIN_UPDATE_PARTS = 2

# QQ 官方 Bot 的事件 user_id 可能是 Gensokyo 会话 ID，而不是 QQ 号。
# Explicit environment selectors are supported, but no real account is
# hard-coded. The normal authorization path is the Core PermissionProvider.
NOTICE_ADMIN_QQ_IDS = {
    value.strip()
    for value in os.getenv("AMIA_NOTICE_ADMIN_QQ_IDS", "").split(",")
    if value.strip()
}
get_real_qq = None


def _core_contract():
    # Tests and lightweight deployments may load this plugin without loading
    # the optional core plugin. Avoid calling NoneBot's ``require`` in that
    # case: it emits a misleading failed-plugin stack trace even though the
    # permission/audit integration is intentionally optional and fail-closed.
    for module_name in ("amia_core", "src.plugins.amia_core"):
        module = sys.modules.get(module_name)
        if module is not None:
            return module
    # ``require`` logs an error when the optional plugin was never discovered.
    # Check NoneBot's registry first so an isolated plugin test remains quiet
    # while a configured deployment can still lazy-load the Core plugin.
    try:
        if get_plugin("amia_core") is None:
            from nonebot.plugin.manager import _managers

            available = {
                plugin_id
                for manager in _managers
                for plugin_id in manager.available_plugins
            }
            if "amia_core" not in available:
                return None
    except Exception:  # noqa: BLE001 - optional provider boundary
        return None
    try:
        return require("amia_core")
    except Exception:  # noqa: BLE001 - optional provider boundary
        return None


def _notice_identity_ids(event: MessageEvent) -> set[str]:
    user_id = str(getattr(event, "user_id", ""))
    identities = {user_id} if user_id else set()
    resolver = get_real_qq
    if resolver is None:
        for module_name in ("src.plugins.qbind", "qbind"):
            module = sys.modules.get(module_name)
            if module is not None:
                resolver = getattr(module, "get_real_qq", None)
                if resolver is not None:
                    break
    if resolver is not None and user_id:
        try:
            real_qq = resolver(user_id)
        except Exception:  # noqa: BLE001 - permission checks must fail closed
            real_qq = None
        if real_qq:
            identities.add(str(real_qq))
    return identities


def _event_scope(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return group_scope(event.group_id)
    return private_scope(event.user_id)


def _context_id(event: MessageEvent, scope: str) -> str:
    return f"onebot:{getattr(event, 'self_id', '')}:{scope}"


async def _has_scope_permission(
    bot: Bot, event: MessageEvent, scope: str | None = None
) -> bool:
    """Check management permission in one scope.

    Cross-group publishing asks the provider about the *target* scope, so a
    group administrator cannot reuse their own group's permission to push an
    announcement elsewhere.
    """

    target = scope or _event_scope(event)
    if await SUPERUSER(bot, event):
        return True
    if NOTICE_ADMIN_QQ_IDS and _notice_identity_ids(event) & NOTICE_ADMIN_QQ_IDS:
        return True

    core = _core_contract()
    if core is None:
        return False
    registry = getattr(core, "registry", None)
    provider = registry.get_permission_provider("static") if registry else None
    resolver = registry.get_identity_resolver() if registry else None
    if provider is None or resolver is None:
        return False
    key = core.UserIdentityKey(
        self_id=str(getattr(event, "self_id", "")),
        user_id=str(getattr(event, "user_id", "")),
    )
    resolved = await core.call_provider_safe(
        resolver.resolve_identity, key, timeout=0.5
    )
    if not resolved.success:
        return False
    decision = await core.call_provider_safe(
        provider.has_permission,
        resolved.value,
        "group.notice.manage",
        _context_id(event, target),
        timeout=0.5,
    )
    return bool(decision.success and decision.value)


async def _is_notice_admin(bot: Bot, event: MessageEvent) -> bool:
    return await _has_scope_permission(bot, event)


# --- 插件元数据 ---
__plugin_meta__ = PluginMetadata(
    name="公告",
    description="Mizuki 智能公告系统（公告发布与显式投递）",
    usage=(
        "查看：\n"
        "• 公告 查看 [序号] - 列表或全文\n"
        "• 公告 状态 [序号] - 发布状态与各目标投递状态\n"
        "• 公告 统计 - 已订阅目标的投递覆盖\n"
        "管理（管理员）：\n"
        "• 公告 增加/修改/删除 - 维护草稿\n"
        "• 公告 发布 <序号> [group:<群号>] - 发布并显式投递\n"
        "• 公告 计划 <序号> <ISO时间> - 记录计划时间，到点需执行「公告 投递」\n"
        "• 公告 投递 - 手工触发到期公告投递（无后台调度）\n"
        "• 公告 撤回 <序号> - 撤回并拦截未发送的投递\n"
        "• 公告 重投 <序号> [目标] - 人工重投（可能重复）\n"
        "• 公告 订阅 / 退订 - 维护本目标订阅\n"
        "• 公告 导入 - 把旧版 notices.json 导入为草稿"
    ),
)

# 路径设置
DATA_DIR = Path(os.getenv("AMIA_GROUP_DATA_DIR", "data/mizuki_notice"))
LEDGER_FILE = DATA_DIR / "notice_ledger.sqlite3"
LEGACY_NOTICES_FILE = DATA_DIR / "notices.json"

class _Runtime:
    """Lazy ledger/service handles.

    A plain holder instead of module globals: the delivery service must be
    created exactly once per process so its restart recovery runs once.
    """

    ledger: NoticeLedger | None = None
    service: NoticeDeliveryService | None = None


_runtime = _Runtime()


def reset_runtime_state() -> None:
    """Drop cached ledger handles (used by tests and reloads)."""

    if _runtime.ledger is not None:
        _runtime.ledger.close()
    _runtime.ledger = None
    _runtime.service = None


def _get_ledger() -> NoticeLedger:
    if _runtime.ledger is None:
        _runtime.ledger = NoticeLedger(LEDGER_FILE)
    return _runtime.ledger


def _get_service() -> NoticeDeliveryService:
    if _runtime.service is None:
        _runtime.service = NoticeDeliveryService(_get_ledger())
    return _runtime.service


async def run_due_deliveries(
    *, sender: NoticeSender, now: datetime | None = None
) -> list[DeliveryResult]:
    """Deliver every due, subscribed notice through the explicit trigger.

    There is intentionally no startup loop: a scheduled notice is only sent
    when an administrator runs ``公告 投递`` (or ``公告 发布`` for one target),
    so a restart never turns into an unsolicited broadcast.
    """

    return await _get_service().deliver_due(sender=sender, now=now)


def _state_label(state: str) -> str:
    return {"draft": "草稿", "published": "已发布", "withdrawn": "已撤回"}.get(
        state, state
    )


def _delivery_label(status: str) -> str:
    return {
        "in_flight": "进行中",
        "accepted": "已受理",
        "failed": "失败",
        "unknown": "未知",
    }.get(status, status)


def _persist_ledger_error(operation: str, exc: Exception) -> None:
    # Never log the ledger path or the notice body: both may identify a group.
    logger.warning(f"公告账本操作失败 operation={operation} error={type(exc).__name__}")


async def _audit_notice_action(
    event: MessageEvent, action: str, *, scope: str | None = None
) -> None:
    core = _core_contract()
    if core is None:
        return
    registry = getattr(core, "registry", None)
    audit = registry.get_audit_logger("sqlite") if registry else None
    resolver = registry.get_identity_resolver() if registry else None
    if audit is None or resolver is None:
        return
    key = core.UserIdentityKey(
        self_id=str(getattr(event, "self_id", "")),
        user_id=str(getattr(event, "user_id", "")),
    )
    resolved = await core.call_provider_safe(
        resolver.resolve_identity, key, timeout=0.5
    )
    if not resolved.success:
        return
    target_scope = scope or _event_scope(event)
    await core.call_provider_safe(
        audit.log_action,
        resolved.value,
        action,
        target_scope,
        {
            "scope": _context_id(event, target_scope),
            "target_type": scope_kind(target_scope),
            "result": "success",
        },
        timeout=0.5,
    )


def _select_notice(
    ledger: NoticeLedger, index_token: str | None
) -> tuple[NoticeRecord | None, int | None, str | None]:
    """Select a notice by one-based index, defaulting to the newest one."""

    notices = ledger.list_notices()
    if not notices:
        return None, None, "empty"
    index = (
        len(notices) - 1
        if index_token is None
        else parse_notice_index(index_token, len(notices))
    )
    if index is None:
        return None, None, "index"
    return notices[index], index, None


def _format_delivery(notice: NoticeRecord, result: DeliveryResult) -> str:
    return (
        f"公告 r{notice.revision} → {result.target_scope}\n"
        f"{describe(result)}"
    )


async def _target_block_reason(
    bot: Bot,
    event: MessageEvent,
    ledger: NoticeLedger,
    *,
    source_scope: str,
    target_scope: str,
) -> str | None:
    """Both sides must agree before anything leaves this group.

    The publishing administrator needs permission in the *target* scope, and
    the target must have subscribed.  ``get_group_list`` is never consulted:
    seeing a group is not the same as that group asking for announcements.
    """

    if target_scope != source_scope and not await _has_scope_permission(
        bot, event, target_scope
    ):
        return "target_not_authorized"
    if ledger.subscription_state(target_scope) != "subscribed":
        return "target_not_subscribed"
    return None


# --- 1. 公告管理指令 ---
# The matcher is broad only to parse the ``公告`` prefix; it must not prevent
# economy/PJSK/other plugins from seeing unrelated messages, and it never
# sends anything on behalf of another matcher.
notice_manage = on_message(priority=5, block=False)


@notice_manage.handle()
async def manage_notice(bot: Bot, event: MessageEvent) -> None:  # noqa: C901, PLR0911, PLR0912
    """Route one ``公告`` command.

    This is a command router: only the delivery commands below reach a send,
    and every query branch returns without touching the sender.
    """
    parsed = parse_notice_command(event.get_plaintext())
    if parsed is None:
        await notice_manage.skip()
        return

    try:
        ledger = _get_ledger()
    except LedgerUnreadableError:
        logger.warning("公告账本不可读，自动投递已关闭")
        await notice_manage.finish("公告账本不可读，投递已关闭，请检查数据目录。")
        return

    operation = parsed.operation
    argument = parsed.argument

    if operation == "查看":
        await _show_notices(ledger, argument)
        return

    if operation == "统计":
        await _show_statistics(bot, event, ledger)
        return

    if operation == "状态":
        await _show_status(ledger, argument)
        return

    if operation not in MANAGEMENT_OPERATIONS:
        await notice_manage.finish(
            "未知公告操作，请使用：查看、状态、统计、增加、修改、删除、"
            "发布、计划、投递、撤回、重投、订阅、退订或导入。"
        )
        return

    if not await _is_notice_admin(bot, event):
        await notice_manage.finish("你没有权限管理公告。")
        return

    try:
        if operation == "增加":
            await _add_notice(ledger, argument, event)
        elif operation == "修改":
            await _update_notice(ledger, argument, event)
        elif operation == "删除":
            await _delete_notice(ledger, argument, event)
        elif operation == "发布":
            await _publish_notice(bot, event, ledger, argument)
        elif operation == "计划":
            await _schedule_notice(bot, event, ledger, argument)
        elif operation == "投递":
            await _deliver_due_notices(bot, event, argument)
        elif operation == "撤回":
            await _withdraw_notice(ledger, argument, event)
        elif operation == "重投":
            await _redeliver_notice(bot, event, ledger, argument)
        elif operation == "导入":
            await _import_legacy_notices(ledger, event)
        elif operation == "订阅":
            await _set_subscription(ledger, event, subscribed=True)
        else:
            await _set_subscription(ledger, event, subscribed=False)
    except LedgerError as exc:
        _persist_ledger_error(operation, exc)
        await notice_manage.finish("公告账本操作失败，本次未发送。")
    except OSError as exc:
        _persist_ledger_error(operation, exc)
        await notice_manage.finish("公告存储不可用，本次未发送。")


# --- 2. 查询（永不发送） ---
async def _show_notices(ledger: NoticeLedger, argument: str | None) -> None:
    notices = ledger.list_notices()
    if not notices:
        await notice_manage.finish("当前暂无公告。")
        return
    if argument is not None:
        index = parse_notice_index(argument, len(notices))
        if index is None:
            await notice_manage.finish("序号错误。")
            return
        notice = notices[index]
        lines = [
            f"--- 公告 {index + 1} ---",
            f"状态：{_state_label(notice.state)}",
            f"版本：r{notice.revision}",
        ]
        if notice.publish_at is not None:
            lines.append(f"计划发布：{notice.publish_at}")
        lines.append(notice.content)
        await notice_manage.finish("\n".join(lines))
        return
    preview = "\n".join(
        f"{i + 1}. [{_state_label(notice.state)}] {notice.content[:20]}..."
        for i, notice in enumerate(notices)
    )
    await notice_manage.finish(
        f"--- 公告列表 ---\n{preview}\n发送 [公告 查看 序号] 看全文"
    )


async def _show_status(ledger: NoticeLedger, argument: str | None) -> None:
    notice, index, problem = _select_notice(ledger, argument)
    if problem == "empty":
        await notice_manage.finish("当前暂无公告。")
        return
    if problem == "index" or notice is None or index is None:
        await notice_manage.finish("序号错误。")
        return
    attempts = ledger.attempts_for_notice(notice.notice_id, notice.revision)
    header = (
        f"公告 {index + 1} 发布状态：{_state_label(notice.state)}（r{notice.revision}）"
    )
    if not attempts:
        await notice_manage.finish(f"{header}\n尚无投递记录。")
        return
    lines = [header, "投递状态："]
    for attempt in attempts:
        lines.append(
            f"• {attempt.target_scope}: {_delivery_label(attempt.status)}"
            f"（第 {attempt.attempt_no + 1} 次）"
        )
    await notice_manage.finish("\n".join(lines))


async def _show_statistics(
    bot: Bot, event: MessageEvent, ledger: NoticeLedger
) -> None:
    if not await _is_notice_admin(bot, event):
        await notice_manage.finish("你没有权限查看公告统计。")
        return
    published = [n for n in ledger.list_notices() if n.state == "published"]
    if not published:
        await notice_manage.finish("目前没有已发布的公告，无法统计。")
        return
    latest = published[-1]
    subscribed = ledger.subscribed_scopes()
    delivered = ledger.accepted_scopes(latest.notice_id, latest.revision)
    delivered_count, subscribed_count = subscription_coverage(subscribed, delivered)
    await notice_manage.finish(
        f"最新公告投递: {delivered_count} / {subscribed_count} 个已订阅目标"
    )


# --- 3. 草稿维护 ---
async def _add_notice(
    ledger: NoticeLedger, content: str | None, event: MessageEvent
) -> None:
    if not valid_notice_content(content):
        if content is not None and len(content) > MAX_NOTICE_LENGTH:
            await notice_manage.finish(f"内容过长，最多 {MAX_NOTICE_LENGTH} 字符。")
            return
        await notice_manage.finish("内容不能为空")
        return
    assert content is not None
    try:
        ledger.create_notice(content, source_scope=_event_scope(event))
    except NoticeLimitReachedError:
        await notice_manage.finish(f"位置已满({MAX_NOTICES}/{MAX_NOTICES})")
        return
    await _audit_notice_action(event, "group.notice.add")
    await notice_manage.finish("写入成功。")


async def _update_notice(
    ledger: NoticeLedger, argument: str | None, event: MessageEvent
) -> None:
    sub_parts = argument.split(maxsplit=1) if argument else []
    if len(sub_parts) < MIN_UPDATE_PARTS or not valid_notice_content(sub_parts[1]):
        if len(sub_parts) >= MIN_UPDATE_PARTS and len(sub_parts[1]) > MAX_NOTICE_LENGTH:
            await notice_manage.finish(f"内容过长，最多 {MAX_NOTICE_LENGTH} 字符。")
            return
        await notice_manage.finish("格式错误")
        return
    notices = ledger.list_notices()
    index = parse_notice_index(sub_parts[0], len(notices))
    if index is None:
        await notice_manage.finish("序号错误")
        return
    updated = ledger.update_notice(notices[index].notice_id, sub_parts[1])
    await _audit_notice_action(event, "group.notice.update")
    await notice_manage.finish(f"公告 {index + 1} 已修改（r{updated.revision}）。")


async def _delete_notice(
    ledger: NoticeLedger, argument: str | None, event: MessageEvent
) -> None:
    if argument is None:
        await notice_manage.finish("格式错误")
        return
    notices = ledger.list_notices()
    index = parse_notice_index(argument, len(notices))
    if index is None:
        await notice_manage.finish("序号错误")
        return
    try:
        ledger.delete_notice(notices[index].notice_id)
    except NoticeStateError:
        await notice_manage.finish("公告已发布，请先撤回再删除。")
        return
    await _audit_notice_action(event, "group.notice.delete")
    await notice_manage.finish("已删除。")


# --- 4. 显式投递 ---
async def _publish_notice(
    bot: Bot, event: MessageEvent, ledger: NoticeLedger, argument: str | None
) -> None:
    source_scope = _event_scope(event)
    tokens = (argument or "").split()
    target_token = tokens[1] if len(tokens) > 1 else None
    notice, index, problem = _select_notice(ledger, tokens[0] if tokens else None)
    if problem == "empty":
        await notice_manage.finish("当前暂无公告。")
        return
    if problem == "index" or notice is None or index is None:
        await notice_manage.finish("序号错误。")
        return
    target_scope = (
        parse_target_scope(target_token) if target_token else source_scope
    )
    if target_scope is None:
        await notice_manage.finish(
            "目标格式应为 group:<群号> 或 private:<账号>。"
        )
        return

    reason = await _target_block_reason(
        bot,
        event,
        ledger,
        source_scope=source_scope,
        target_scope=target_scope,
    )
    if reason == "target_not_authorized":
        await notice_manage.finish("你没有目标群的公告管理权限。")
        return
    if reason == "target_not_subscribed":
        await notice_manage.finish("目标未订阅公告，未发送。")
        return

    if notice.state == "withdrawn":
        await notice_manage.finish("公告已撤回，不能发布。")
        return
    if notice.state == "draft":
        notice = ledger.publish(notice.notice_id, source_scope=source_scope)

    result = await _get_service().deliver(
        notice_id=notice.notice_id,
        revision=notice.revision,
        target_scope=target_scope,
        # The owner is the notice's origin scope, not the caller's.  An
        # already-published notice republished from another scope must reuse
        # the original logical key, otherwise the ledger sees a fresh key and
        # sends the same revision to the same target a second time.
        owner_scope=notice.source_scope,
        sender=OneBotNoticeSender(bot),
    )
    await _audit_notice_action(event, "group.notice.publish", scope=target_scope)
    await notice_manage.finish(
        f"公告 {index + 1} 已发布。\n{_format_delivery(notice, result)}"
    )


async def _schedule_notice(
    bot: Bot, event: MessageEvent, ledger: NoticeLedger, argument: str | None
) -> None:
    source_scope = _event_scope(event)
    tokens = (argument or "").split()
    if len(tokens) < 2:
        await notice_manage.finish("格式：公告 计划 <序号> <ISO时间> [目标]")
        return
    when = parse_schedule(tokens[1])
    if when is None:
        await notice_manage.finish(
            "时间格式错误，请使用 ISO 8601，例如 2026-10-25T09:00。"
        )
        return
    target_token = tokens[2] if len(tokens) > 2 else None
    target_scope = (
        parse_target_scope(target_token) if target_token else source_scope
    )
    if target_scope is None:
        await notice_manage.finish("目标格式应为 group:<群号> 或 private:<账号>。")
        return
    reason = await _target_block_reason(
        bot,
        event,
        ledger,
        source_scope=source_scope,
        target_scope=target_scope,
    )
    if reason == "target_not_authorized":
        await notice_manage.finish("你没有目标群的公告管理权限。")
        return
    if reason == "target_not_subscribed":
        await notice_manage.finish("目标未订阅公告，未计划投递。")
        return
    notices = ledger.list_notices()
    index = parse_notice_index(tokens[0], len(notices))
    if index is None:
        await notice_manage.finish("序号错误。")
        return
    notice = notices[index]
    if notice.state != "draft":
        await notice_manage.finish("只有草稿可以计划发布。")
        return
    published = ledger.publish(
        notice.notice_id, source_scope=source_scope, publish_at=when
    )
    await _audit_notice_action(event, "group.notice.schedule", scope=target_scope)
    await notice_manage.finish(
        f"公告 {index + 1} 已记录计划时间 {published.publish_at}。\n"
        "到点不会自动发送：需要管理员执行「公告 投递」触发到期投递。"
        "计划时间到达不等于群内必达。"
    )


async def _deliver_due_notices(
    bot: Bot, event: MessageEvent, argument: str | None
) -> None:
    """Explicit trigger for scheduled notices.

    ``deliver_due`` is never started by NoneBot's startup/shutdown hooks: the
    operator asks for the sweep, and only due, subscribed targets are sent.
    """
    if argument:
        await notice_manage.finish("格式：公告 投递")
        return
    results = await run_due_deliveries(sender=OneBotNoticeSender(bot))
    await _audit_notice_action(event, "group.notice.deliver_due")
    if not results:
        await notice_manage.finish("当前没有到期且已订阅的公告需要投递。")
        return
    accepted = sum(1 for result in results if result.status == "accepted")
    failed = sum(1 for result in results if result.status == "failed")
    unknown = len(results) - accepted - failed
    lines = [
        f"到期投递完成：已受理 {accepted} / 失败 {failed} / 未知 {unknown}。",
        "已受理仅代表接口接受，不代表已显示。",
    ]
    # Name the target of every listed result: a bare count leaves the operator
    # unable to tell which groups were actually addressed.
    lines.extend(
        f"• {result.target_scope} {describe(result)}" for result in results[:5]
    )
    await notice_manage.finish("\n".join(lines))


async def _withdraw_notice(
    ledger: NoticeLedger, argument: str | None, event: MessageEvent
) -> None:
    notice, index, problem = _select_notice(ledger, argument)
    if problem == "empty":
        await notice_manage.finish("当前暂无公告。")
        return
    if problem == "index" or notice is None or index is None:
        await notice_manage.finish("序号错误。")
        return
    try:
        ledger.withdraw(notice.notice_id)
    except NoticeStateError:
        await notice_manage.finish("只有已发布的公告可以撤回。")
        return
    except NoticeMissingError:
        await notice_manage.finish("公告不存在。")
        return
    await _audit_notice_action(event, "group.notice.withdraw")
    await notice_manage.finish(
        f"公告 {index + 1} 已撤回，尚未发送的投递会被拦截。"
    )


async def _redeliver_notice(
    bot: Bot, event: MessageEvent, ledger: NoticeLedger, argument: str | None
) -> None:
    source_scope = _event_scope(event)
    tokens = (argument or "").split()
    target_token = tokens[1] if len(tokens) > 1 else None
    notice, index, problem = _select_notice(ledger, tokens[0] if tokens else None)
    if problem == "empty":
        await notice_manage.finish("当前暂无公告。")
        return
    if problem == "index" or notice is None or index is None:
        await notice_manage.finish("序号错误。")
        return
    if notice.state == "draft":
        # A draft was never published, so a "retry" would only record a
        # permanently blocking failed attempt (notice_not_published).
        await notice_manage.finish("公告还是草稿，请先发布再重投。")
        return
    target_scope = parse_target_scope(target_token) if target_token else source_scope
    if target_scope is None:
        await notice_manage.finish("目标格式应为 group:<群号> 或 private:<账号>。")
        return
    reason = await _target_block_reason(
        bot,
        event,
        ledger,
        source_scope=source_scope,
        target_scope=target_scope,
    )
    if reason == "target_not_authorized":
        await notice_manage.finish("你没有目标群的公告管理权限。")
        return
    if reason == "target_not_subscribed":
        await notice_manage.finish("目标未订阅公告，未发送。")
        return

    # Every manual retry is its own idempotency id: replaying the same command
    # twice reuses the attempt instead of sending twice.
    operation_id = f"redeliver:{uuid.uuid4().hex}"
    result = await _get_service().deliver(
        notice_id=notice.notice_id,
        revision=notice.revision,
        target_scope=target_scope,
        owner_scope=source_scope,
        sender=OneBotNoticeSender(bot),
        manual_operation_id=operation_id,
    )
    await _audit_notice_action(event, "group.notice.redeliver", scope=target_scope)
    warning = f"{MANUAL_RETRY_WARNING}\n" if result.sent else ""
    await notice_manage.finish(
        f"公告 {index + 1} 人工重投。\n{warning}{_format_delivery(notice, result)}"
    )


async def _set_subscription(
    ledger: NoticeLedger, event: MessageEvent, *, subscribed: bool
) -> None:
    scope = _event_scope(event)
    if scope_kind(scope) == "unknown":
        await notice_manage.finish("无法确认当前目标范围。")
        return
    ledger.set_subscription(scope, subscribed=subscribed)
    await _audit_notice_action(
        event,
        "group.notice.subscribe" if subscribed else "group.notice.unsubscribe",
    )
    if subscribed:
        await notice_manage.finish("本目标已订阅公告。")
    else:
        await notice_manage.finish("本目标已退订公告，未发送的投递会被拦截。")


async def _import_legacy_notices(
    ledger: NoticeLedger, event: MessageEvent
) -> None:
    """Import the legacy JSON list as drafts, never as delivered notices."""

    legacy = load_legacy_notices(LEGACY_NOTICES_FILE)
    if not legacy:
        await notice_manage.finish("没有可导入的旧版公告。")
        return
    imported = 0
    for content in legacy:
        if not valid_notice_content(content):
            continue
        try:
            ledger.create_notice(content, source_scope=_event_scope(event))
        except NoticeLimitReachedError:
            break
        imported += 1
    await _audit_notice_action(event, "group.notice.import")
    await notice_manage.finish(
        f"已导入 {imported} 条旧版公告为草稿，未自动发布或投递。"
    )


__all__ = [
    "LEDGER_FILE",
    "LEGACY_NOTICES_FILE",
    "MANAGEMENT_OPERATIONS",
    "NOTICE_ADMIN_QQ_IDS",
    "logical_delivery_key",
    "manage_notice",
    "notice_manage",
    "receipt_from_onebot_result",
    "reset_runtime_state",
    "run_due_deliveries",
]
