# Amia-plugin-group

`Amia-plugin-group` 当前是 Mizuki Bot 的群公告管理与自动分发插件。

仓库名虽然是 `group`，但当前代码并不提供完整的群管理能力；它只负责公告查看、增删改、统计、补发和每个目标的最新发送历史。

认领状态：公告主体已实现，权限、审计和存储仍需完善。

## 插件作用

```text
超级用户维护公告
        ↓
notices.json
        ↓
其他 matcher 成功处理消息
        ↓
run_postprocessor 检查发送历史
        ↓
未收到最新公告的目标补发一次
```

当前不包含：

- 禁言；
- 踢人；
- 群成员权限管理；
- 入群审批；
- 完整群配置中心。

## 当前指令

```text
公告 查看
公告 查看 <序号>
公告 增加 <内容>
公告 修改 <序号> <内容>
公告 删除 <序号>
公告 统计
```

当前权限：

- 查看公告：所有用户；
- 增加、修改、删除：NoneBot `SUPERUSER`；
- 公告统计：NoneBot `SUPERUSER`。

最多保存：

```text
5 条公告
```

单条公告最多 2000 个字符。空白内容、超长内容和越界序号会在命令层拒绝。

## 自动分发机制

插件通过 `run_postprocessor` 在其他 matcher 成功处理后检查最新公告：

```text
任意指令成功执行
      ↓
读取最新公告
      ↓
计算公告内容哈希
      ↓
检查当前目标发送历史
      ↓
未发送则补发一次
```

目标标识：

```text
group:<group_id>
private:<user_id>
```

同一个目标只会收到同一版本公告一次。公告内容发生变化后，哈希变化，目标可以再次收到新版本。

公告发送失败不应影响用户原本执行的指令。

## 数据文件

默认目录：

```text
data/mizuki_notice/
```

文件：

```text
notices.json
sent_history.json
```

用途：

- `notices.json`：公告列表；
- `sent_history.json`：各目标最后收到的公告哈希。

这些文件属于运行数据，不应提交到 Git。

## 当前并发模型

当前使用进程内 `_sending_lock` 防止同一目标被多个 matcher 同时触发重复发送；JSON 文件通过同目录临时文件原子替换，成功发送后会合并最新历史文件内容再写回。

该方案只适用于单进程运行：

- 多 Worker 之间不能共享锁；
- 多实例之间不能保证唯一发送；
- 原子替换不等同于跨进程锁；
- 进程异常退出时可能留下不完整文件。

需要多进程部署时，应改用 SQLite 或共享存储，并在存储层增加事务、唯一约束和原子更新。

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

- JSON 读取对损坏内容安全降级，写入使用同目录临时文件和原子替换；
- 公告序号在转换前校验，非法输入返回提示，不让 matcher 抛出转换异常；
- 统计只计算当前 Bot 群列表中的唯一群，并要求 `SUPERUSER`；
- 自动补发失败会记录目标和异常类型，释放目标锁，不影响原 matcher；
- 保存历史失败不会伪造“已送达”，且目标锁始终释放，下一次仍会重试；
- 同一进程内不同目标并发补发时，历史写入会合并最新文件内容，避免互相覆盖；
- postprocessor 通过 matcher 对象和稳定插件名排除公告插件自身。

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

当前离线测试覆盖：

- 空公告列表；
- 新增达到 5 条上限；
- 非超管不能增删改；
- 非法序号不会崩溃；
- 同一目标同一公告只发送一次；
- 公告修改后重新发送；
- 群聊和私聊历史隔离；
- 发送失败后锁正常释放；
- JSON 损坏或结构错误时安全降级；
- 当前群列表统计与过期/私聊历史隔离；
- 不同目标并发写历史时不互相覆盖；
- 同目标并发补发去重；
- 原子写入失败后不留下临时文件或半写入 JSON；
- 公告插件自身和失败 matcher 不触发补发。

PermissionProvider 与 AuditLogger 尚未接入，因此不在本轮运行测试范围内。

## 已知限制

- 当前仍使用 JSON 存储；
- 只保证单进程内的发送去重；
- PermissionProvider 缺失或异常时管理操作默认拒绝；
- Audit 失败不会阻断已获授权的公告主操作，但会记录失败；
- 不具备完整群管理能力。

## 维护边界

- 不提交公告运行数据；
- 不在 Postprocessor 中执行耗时操作；
- 不因公告发送失败影响原 matcher；
- 不把本插件宣传成完整群管理框架；
- 当前仓库尚未确定公开许可证。

## Release010 发布说明

Release010 测试确认 PermissionProvider 收到字符串形式的 `self_id`、`user_id` 和 `group:<group_id>` 作用域。真实群管理员身份、公告发送、Audit 数据库和多进程部署仍需实机验证。
