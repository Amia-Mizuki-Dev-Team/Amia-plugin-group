# Amia-plugin-group

`Amia-plugin-group` 当前是 Mizuki Bot 的群公告管理与显式投递插件。

仓库名虽然是 `group`，但当前代码并不提供完整的群管理能力；它只负责公告草稿维护、显式投递、订阅管理、投递状态与统计。

认领状态：公告主体已实现（SQLite claim 账本 + 显式投递），不再有全局 postprocessor 自动补发。

## 插件作用

```text
超级用户维护公告（草稿 / 发布 / 计划 / 撤回）
        ↓
notice_ledger.sqlite3（通知修订 / 订阅 / 投递尝试）
        ↓
管理员执行「公告 发布」或「公告 投递」
        ↓
ledger.claim 先落盘（跨连接唯一，发送前提交）
        ↓
发送前复查（撤回 / 退订 / 过期 / 是否到期）
        ↓
调用 OneBot 发送一次，并把回执写回账本
```

当前不包含：

- 禁言；
- 踢人；
- 群成员权限管理；
- 入群审批；
- 完整群配置中心。

## 当前指令

```text
公告 查看 [序号]
公告 状态 [序号]
公告 统计
公告 增加 <内容>
公告 修改 <序号> <内容>
公告 删除 <序号>
公告 发布 <序号> [group:<群号> | private:<账号>]
公告 计划 <序号> <ISO时间> [目标]
公告 投递
公告 撤回 <序号>
公告 重投 <序号> [目标]
公告 订阅
公告 退订
公告 导入
```

当前权限：

- 查看、状态：所有用户；
- 统计、增加、修改、删除、发布、计划、投递、撤回、重投、订阅、退订、导入：管理员；
- 管理员 = NoneBot `SUPERUSER` ∪ `AMIA_NOTICE_ADMIN_QQ_IDS` ∪ `amia_core` 的 `PermissionProvider`（`group.notice.manage`，在**目标**作用域检查）；
- 跨群发布必须在目标群也具备权限，且目标群已订阅，否则不发送。

最多保存：

```text
5 条公告（按 notice_id 计）
```

单条公告最多 2000 个字符。空白内容、超长内容和越界序号会在命令层拒绝。

「公告 计划」只记录计划时间，**不会自动发送**：到期后需要管理员执行「公告 投递」触发。后台周期投递未实现，启动流程不会自动执行业务投递。

## 显式投递机制

插件不再注册 `run_postprocessor`：**没有任何 matcher 会在别的指令执行完后顺带补发公告**。唯一的发送入口是公告指令本身（`公告 发布` / `公告 投递` / `公告 重投`），且发送点位于 `ledger.claim()` 之后。

投递顺序：

```text
管理员显式触发
      ↓
检查目标权限 + 订阅（跨群需双方都成立）
      ↓
claim：在 SQLite 中以 BEGIN IMMEDIATE 写入 in_flight 并提交
      ↓
发送前复查：已撤回 / 未发布 / 内容版本变化 / 未到期 / 已过期 / 已退订
      ↓
调用 OneBot 发送一次
      ↓
把 accepted / failed / unknown 写回同一条 attempt
```

目标标识：

```text
group:<group_id>
private:<user_id>
```

同一 `(通知, 版本, owner, source, target)` 只会产生一条 `accepted`。回执 `retcode=0` 但没有可核实 `message_id`（例如 `"0"`）时记为 `unknown`，不得当作已送达；`unknown` 只能由管理员执行「公告 重投」产生新 attempt。

进程在 claim 之后、写回之前退出时，下一次启动会把旧 `boot_id` 的 `in_flight` 一律标记为 `unknown`，**从不自动重发**。

公告发送失败不应影响用户原本执行的指令。

## 数据文件

默认目录：

```text
data/mizuki_notice/
```

文件：

```text
notice_ledger.sqlite3
notices.json            # 仅旧版导入源
```

用途：

- `notice_ledger.sqlite3`：三张表 —— `notice_revisions`（通知修订与发布态）、`subscriptions`（目标订阅）、`delivery_attempts`（投递 claim 与终态）；
- `notices.json`：旧版公告列表，只能通过「公告 导入」变成草稿，不会自动发布或投递。

这些文件属于运行数据，不应提交到 Git。

## 当前并发模型

`delivery_attempts` 用 `BEGIN IMMEDIATE` + `UNIQUE(logical_key, attempt_no)` + 部分唯一索引（`WHERE status='in_flight'`）保证同一投递键在**跨连接、跨进程**范围内最多一个进行中的 claim；claim 在返回前提交，因此第二个 worker 会读到 `in_flight` 并放弃发送。

崩溃恢复按 `boot_id` 判定：旧进程留下的 `in_flight` 一律改判 `unknown`，既不自动重发，也不会在无证据时声称送达。

仍未覆盖的边界：

- 同一 SQLite 文件位于网络共享盘时，文件锁语义取决于共享实现；
- `switch`/备份类的外部文件操作不属于本插件职责。

## Permission 对接

公告增删改优先检查以下权限：

```text
group.notice.view
group.notice.manage
group.notice.stats
```

推荐调用：

```python
provider = registry.get_permission_provider("static")

if provider is None:
    allowed = False
else:
    result = await call_provider_safe(
        provider.has_permission,
        identity,
        "group.notice.manage",
        f"group:{event.group_id}",
        timeout=0.5,
    )
    allowed = bool(result.success and result.value)
```

Provider 缺失、超时或异常时必须默认拒绝。

当前代码已经通过 `amia-core` 接入 `static` PermissionProvider。Provider 缺失、IdentityResolver 缺失、超时或异常时默认拒绝；NoneBot `SUPERUSER` 仅保留兼容管理路径。

## Audit 对接

公告管理操作应写入：

```text
group.notice.create
group.notice.update
group.notice.delete
```

示例：

```python
audit = registry.get_audit_logger("sqlite")
if audit is not None:
    await audit.log_action(
        actor=identity,
        action="group.notice.update",
        target="notice:<index-or-hash>",
        details={
            "result": "success",
            "before": old_summary,
            "after": new_summary,
        },
    )
```

不应把完整无关聊天内容写入审计。

当前代码已经对公告新增、修改和删除调用 `sqlite` AuditLogger。Audit 缺失或失败不会伪造成功记录，也不会把完整聊天正文写入审计。

## 本轮最终审查结论

- 账本不可读、结构损坏时命令层返回 `HX-PHI` 风格的失败提示并拒绝发送，不会假装成功；
- 公告序号在转换前校验，非法输入返回提示，不让 matcher 抛出转换异常；
- 统计分母是订阅表，不再使用 `get_group_list`，并要求管理员权限；
- 投递失败会记录目标、状态与异常类型，不影响原 matcher；
- claim 写回失败时保留 `in_flight` 占位并报 `unknown`，不会自动重发；
- claim 唯一性由 SQLite 事务与唯一索引保证，跨连接/跨进程成立；
- 全局 postprocessor 已移除，任何其他指令都不会触发公告发送。

后续若需要继续演进，只限于共享事务存储和多进程一致性；这不代表本仓库应扩展为完整群管框架。

## 离线测试

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

## 离线 Benchmark

```bash
python benchmarks/benchmark_storage.py --iterations 100 --targets 32
```

Benchmark 使用临时目录，只测公告发送成功后的原子 JSON 历史合并写入，不修改 `data/mizuki_notice/`，结果用于本机回归比较，不作为部署性能 SLA。

## 依赖

当前依赖：

```text
nonebot2
nonebot-adapter-onebot
```

后续对接：

```text
amia_core
Amia-plugin-permission
Amia-plugin-audit
```

## 测试

当前离线测试覆盖（`tests/`，共 57 例）：

- `test_logic`：序号解析、命令解析、目标作用域、投递键、回执分类、计划时间、内容校验；
- `test_storage`：旧版 JSON 读取/写入的安全降级；
- `test_runtime`：权限与跨群拦截、订阅开关、查询指令零发送、发布恰好一次、已发布公告从另一作用域再次发布不二次群发、撤回拦截、计划文案、到期公告只由显式指令投递、`公告 投递` 只送到期公告、草稿重投被拒且零 attempt、旧公告导入为草稿、`unknown` 后人工重投；
- `test_notice_delivery`：claim 发送前已提交且跨连接唯一、四种崩溃窗口、撤回/退订/过期在网络前拦截、`not_yet_due` 归暂态（窗口打开后可重投成功）、永久项仍 `retry_denied`、`message_id="0"` 判 `unknown`、accepted 恰好一次、无全局 postprocessor（检测器带正向对照，不是恒空过滤器）。

PermissionProvider 与 AuditLogger 的失败分支有独立用例；真实 QQ 客户端显示仍属 `UI NOT VERIFIED`。

## 已知限制

- 「公告 计划」不会自动投递，需要管理员执行「公告 投递」（后台周期任务未实现，启动不自动执行业务动作）；
- 成绩/订阅数据与 QQ 身份无绑定，订阅以目标作用域（群或私聊）为单位；
- PermissionProvider 缺失或异常时管理操作默认拒绝；
- Audit 失败不会阻断已获授权的公告主操作，但会记录失败；
- 不具备完整群管理能力。

## 维护边界

- 不提交公告运行数据；
- 不在其他 matcher 的执行路径里发送公告（无全局 postprocessor）；
- 不因公告发送失败影响原 matcher；
- 不把本插件宣传成完整群管理框架；
- 当前仓库尚未确定公开许可证。

## Release010 发布说明

Release010 测试确认 PermissionProvider 收到字符串形式的 `self_id`、`user_id` 和 `group:<group_id>` 作用域。真实群管理员身份、公告发送、Audit 数据库和多进程部署仍需实机验证。

## 代码核验与十月状态（2026-10-24）

该插件当前实现公告草稿维护、显式投递、订阅、投递状态与统计，不是通用群管理机器人。发送目标按作用域键（`group:<id>` / `private:<id>`）区分；投递唯一性由 SQLite claim 账本保证。离线测试位于 `tests/`，本轮实测 `Ran 57 tests` / `OK`。真实 QQ 客户端内的图片/文本渲染仍未验证。
