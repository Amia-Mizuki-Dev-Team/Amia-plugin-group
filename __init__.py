# ruff: noqa: N999 - the repository name is intentionally hyphenated.

import os
import sys
from pathlib import Path
from typing import Optional

from nonebot import logger, on_message, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, get_plugin

from .logic import (
    MAX_NOTICE_LENGTH,
    MAX_NOTICES,
    get_content_hash,
    group_coverage,
    parse_notice_command,
    parse_notice_index,
    target_key,
    valid_notice_content,
)
from .storage import load_history, load_notices, record_history, save_json

MANAGEMENT_COMMANDS = {"增加", "修改", "删除"}
COMMAND_PART_INDEX = 1
ARGUMENT_PART_INDEX = 2
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


async def _is_notice_admin(bot: Bot, event: MessageEvent) -> bool:
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
    resolved = await core.call_provider_safe(resolver.resolve_identity, key, timeout=0.5)
    if not resolved.success:
        return False
    context_id = f"onebot:{key.self_id}:group:{event.group_id}"
    decision = await core.call_provider_safe(
        provider.has_permission,
        resolved.value,
        "group.notice.manage",
        context_id,
        timeout=0.5,
    )
    return bool(decision.success and decision.value)

# --- 插件元数据 ---
__plugin_meta__ = PluginMetadata(
    name="公告",
    description="Mizuki 智能公告系统",
    usage=(
        "直接发送：\n"
        "• 公告 增加 [内容] - 新增一条\n"
        "• 公告 修改 [序号] [内容] - 修改某条\n"
        "• 公告 删除 [序号] - 删除某条\n"
        "• 公告 查看 - 查看列表\n"
        "• 公告 统计 - 查看公告发送进度"
    )
)

# 路径设置
DATA_DIR = Path(os.getenv("AMIA_GROUP_DATA_DIR", "data/mizuki_notice"))
NOTICES_FILE = DATA_DIR / "notices.json"
HISTORY_FILE = DATA_DIR / "sent_history.json"

# --- 全局内存锁 (修复并发重复发送的核心) ---
# 记录当前正在发送公告的目标 ID，防止多个插件同时触发时重复发送
_sending_lock: set[str] = set()


def _persist_notices(notices: list[str], operation: str) -> bool:
    try:
        save_json(NOTICES_FILE, notices)
    except OSError as exc:
        logger.warning(
            f"公告保存失败 operation={operation} error={type(exc).__name__}"
        )
        return False
    return True


async def _show_statistics(bot: Bot, event: MessageEvent) -> None:
    if not await _is_notice_admin(bot, event):
        await notice_manage.finish("你没有权限查看公告统计。")
        return
    notices = load_notices(NOTICES_FILE)
    if not notices:
        await notice_manage.finish("目前没有公告，无法统计。")
        return
    group_list = await bot.get_group_list()
    latest_hash = get_content_hash(notices[-1])
    history = load_history(HISTORY_FILE)
    sent_count, total_groups = group_coverage(group_list, history, latest_hash)
    await notice_manage.finish(f"最新公告覆盖: {sent_count} / {total_groups}")


async def _show_notices(notices: list[str], argument: str | None) -> None:
    if not notices:
        await notice_manage.finish("当前暂无公告。")
        return
    if argument is not None:
        idx = parse_notice_index(argument, len(notices))
        if idx is None:
            await notice_manage.finish("序号错误。")
            return
        await notice_manage.finish(f"--- 公告 {idx + 1} ---\n{notices[idx]}")
        return
    preview = "\n".join(
        f"{i + 1}. {msg[:20]}..." for i, msg in enumerate(notices)
    )
    await notice_manage.finish(
        f"--- 公告列表 ---\n{preview}\n发送 [公告 查看 序号] 看全文"
    )


async def _audit_notice_action(event: MessageEvent, action: str) -> None:
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
    resolved = await core.call_provider_safe(resolver.resolve_identity, key, timeout=0.5)
    if not resolved.success:
        return
    scope = f"onebot:{key.self_id}:group:{event.group_id}"
    await core.call_provider_safe(
        audit.log_action,
        resolved.value,
        action,
        f"group:{event.group_id}",
        {"scope": scope, "target_type": "group", "result": "success"},
        timeout=0.5,
    )


async def _add_notice(
    notices: list[str], content: str | None, event: MessageEvent
) -> None:
    if len(notices) >= MAX_NOTICES:
        await notice_manage.finish(f"位置已满({MAX_NOTICES}/{MAX_NOTICES})")
        return
    if not content or not content.strip():
        await notice_manage.finish("内容不能为空")
        return
    if len(content) > MAX_NOTICE_LENGTH:
        await notice_manage.finish(f"内容过长，最多 {MAX_NOTICE_LENGTH} 字符。")
        return
    candidate = [*notices, content]
    if not _persist_notices(candidate, "create"):
        await notice_manage.finish("公告保存失败，请稍后重试。")
        return
    notices[:] = candidate
    await _audit_notice_action(event, "group.notice.add")
    await notice_manage.finish("写入成功。")


async def _update_notice(
    notices: list[str], argument: str | None, event: MessageEvent
) -> None:
    sub_parts = argument.split(maxsplit=1) if argument else []
    if (
        len(sub_parts) < MIN_UPDATE_PARTS
        or not valid_notice_content(sub_parts[1])
    ):
        if len(sub_parts) >= MIN_UPDATE_PARTS and len(sub_parts[1]) > MAX_NOTICE_LENGTH:
            await notice_manage.finish(f"内容过长，最多 {MAX_NOTICE_LENGTH} 字符。")
            return
        await notice_manage.finish("格式错误")
        return
    idx = parse_notice_index(sub_parts[0], len(notices))
    if idx is None:
        await notice_manage.finish("序号错误")
        return
    candidate = list(notices)
    candidate[idx] = sub_parts[1]
    if not _persist_notices(candidate, "update"):
        await notice_manage.finish("公告保存失败，请稍后重试。")
        return
    notices[:] = candidate
    await _audit_notice_action(event, "group.notice.update")
    await notice_manage.finish(f"公告 {idx + 1} 已修改。")


async def _delete_notice(
    notices: list[str], argument: str | None, event: MessageEvent
) -> None:
    if argument is None:
        await notice_manage.finish("格式错误")
        return
    idx = parse_notice_index(argument, len(notices))
    if idx is None:
        await notice_manage.finish("序号错误")
        return
    candidate = list(notices)
    candidate.pop(idx)
    if not _persist_notices(candidate, "delete"):
        await notice_manage.finish("公告保存失败，请稍后重试。")
        return
    notices[:] = candidate
    await _audit_notice_action(event, "group.notice.delete")
    await notice_manage.finish("已删除。")

# --- 1. 公告管理指令 (管理员用) ---
# This matcher is intentionally broad so it can act as the announcement
# post-processor.  It must not prevent economy/PJSK/other plugins from seeing
# unrelated messages when ``parse_notice_command`` returns ``None``.
notice_manage = on_message(priority=5, block=False)

@notice_manage.handle()
async def manage_notice(bot: Bot, event: MessageEvent) -> None:
    parsed = parse_notice_command(event.get_plaintext())
    if parsed is None:
        await notice_manage.skip()
        return

    command = parsed.operation
    argument = parsed.argument

    # [统计]
    if command == "统计":
        await _show_statistics(bot, event)
        return

    # [查看]
    if command == "查看":
        await _show_notices(load_notices(NOTICES_FILE), argument)
        return

    if command not in MANAGEMENT_COMMANDS:
        await notice_manage.finish(
            "未知公告操作，请使用：查看、增加、修改、删除或统计。"
        )
        return

    # --- 管理员操作 ---
    if not await _is_notice_admin(bot, event):
        await notice_manage.finish("你没有权限管理公告。")
        return

    notices = load_notices(NOTICES_FILE)
    if command == "增加":
        await _add_notice(notices, argument, event)
    elif command == "修改":
        await _update_notice(notices, argument, event)
    else:
        await _delete_notice(notices, argument, event)

# --- 2. 核心逻辑：自动广播钩子 ---
@run_postprocessor
async def postprocess_notice(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    exception: Optional[Exception],
) -> None:
    if exception is not None:
        return
    # 排除自身
    if matcher == notice_manage:
        return
    if getattr(matcher, "plugin_name", None) in {
        __plugin_meta__.name,
        "notice",
        "Amia-plugin-group",
    }:
        return

    notices = load_notices(NOTICES_FILE)
    if not notices:
        return

    latest_notice = notices[-1]
    latest_hash = get_content_hash(latest_notice)

    # 确定 ID
    if isinstance(event, GroupMessageEvent):
        target_id = target_key(group_id=event.group_id)
    else:
        target_id = target_key(user_id=event.user_id)

    # ---------------------------------------------------
    # [关键修复] 内存锁检查：如果已经在发了，直接跳过
    # ---------------------------------------------------
    if target_id in _sending_lock:
        return

    # 文件检查：如果以前发过，跳过
    history = load_history(HISTORY_FILE)
    if history.get(target_id) == latest_hash:
        return

    # [加锁] 标记这个 ID 正在处理中
    _sending_lock.add(target_id)

    try:
        # 发送公告
        await bot.send(event, f"【Mizuki 公告】\n{latest_notice}")

        # 写入文件记录
        record_history(HISTORY_FILE, target_id, latest_hash)
    except Exception as exc:  # noqa: BLE001 - failed notices must not break matchers
        logger.warning(
            f"公告补发失败 target={target_id} error={type(exc).__name__}"
        )
    finally:
        # [解锁] 无论成功失败，处理完后移除锁
        # 即使文件保存失败，内存锁解除了，下次还会尝试发送（符合预期）
        # 如果文件保存成功，下次进文件检查就会被拦截
        _sending_lock.discard(target_id)
