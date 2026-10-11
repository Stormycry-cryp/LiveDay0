# 维护事务、持久等待与失败预算

维护仍使用同一个 PostgreSQL、租户事务门禁和 `maintenance_jobs`。保存点区分目标写入与失败记录；持久 `waiting` 区分业务前提不足与真正执行失败。不增加服务、队列、模型调用或通用调度器。

## 状态与事务

| 情形 | 结果 | 预算 |
|---|---|---|
| 到期 pending/retry | 在租户写门禁内领取为 running | attempts 增加，保留历史领取/检查次数语义 |
| 正常完成 | succeeded；目标写入和成功标记一起提交 | failure_count 不增加 |
| 可用依赖有 pending delta | waiting / dependency_pending | 不增加失败次数 |
| 缺少普通完整语义输出 | waiting / semantic_output_required | 不增加失败次数 |
| 删除来源后必须绑定实际读集合 | waiting / version_bound_rebuild_required | 不接受旧裸输出，不增加失败次数 |
| handler 内 SQL/Python 异常 | 保存点回滚目标；外层保存 retry/dead | failure_count 增加；第1、2次分别等待1、2秒，第3次 dead |
| candidate_discovery 未实现 | dead，固定原因 candidate_discovery_not_implemented | 不伪报成功；无需无意义重试 |

`attempts` 在保存点外记录，真实异常不会把领取次数一并抹掉。`failure_count` 是独立的真实失败预算，业务等待不消耗它。错误诊断只保留异常类型及 SQLSTATE，不保存数据库 detail、SQL 文本或任意异常正文；last_error 是最近一次诊断，等待期间保留，成功时清空，不是完整错误审计。

事件任务的 baseline 落后时，在相同租户写锁内更新 baseline，再读取当前 canonical 和 pending 来计算；此处不存在外部模型输出，不把无害的重新读取算成失败或停成等待。

连接丢失、进程崩溃或外层 commit 失败仍可能整体回滚。未提交的尝试不能保证计入预算或留下诊断；本合同不声称崩溃次数也有上限。

## 等待与唤醒

waiting 不在 worker 的到期查询中，反复轮询、`make_pending_ready` 或 `make_retries_ready` 不会消耗它。数据库保存等待原因和内容无关依赖指纹，指纹包含目标/支持/反证身份、版本、生命周期、来源状态和 pending ID 集合，不包含记忆正文。

直接依赖的增量、纠正或 canonical 追赶触发局部通知。只有指纹改变才将同一个 waiting job 唤醒为 pending；重复通知不会唤醒、清空计数或缩短已有 retry 的 backoff。成功重写再次局部失效，以覆盖在等待期间出现的投影。普通安全变化保持已固定 recall cycle，新 cycle 排除 invalidated 视图；删除、纠正、unsafe 沿用硬失效合同。

普通输出现在通过 [完整投影输入绑定](projection-input-binding.md) 的 read_projection_update / commit_projection 到达。非空 run_ready(projection_outputs=...) 已退役并在领取任务前拒绝，不能将旧正文补盖新版本。worker 缺输出时持久等待，合法绑定提交将原任务原子完成，保留 attempts/failure_count；不存在先唤醒后再提交输出的窗口。实际语义内容仍由可信内部调用者负责。

删除后的输出必须通过 `read_projection_rebuild` / `commit_projection_rebuild`；合法绑定提交会将相关 waiting job 一并完成，旧裸输出不能唤醒它。该直接提交 API 保持自身原子事务和异常返回合同；本次 worker 的 failure_count 不扩展成所有内部 API 的统一调用计数。

受信任内部操作员可调用 `resume_waiting(job_id)` 显式检查一次前提，保留原失败预算；跨租户、非 waiting 或 dead 返回 False。它不是公开客户端调度权限。真实失败的 dead 任务不会因新输入、重复通知、普通输出或绑定提交自动创建新预算/复活；本批没有重置 dead 的入口。需要恢复时必须先独立确认错误已修复和恢复操作。删除清理不清除既有真实失败计数。

## 003 升级与回退

003 增加 failure_count、wait_reason、wait_input_fingerprint 和 waiting 状态；原 live-target 部分唯一索引纳入 waiting。001/002 原文不变。升级须停止旧 worker，不支持新旧版本混跑；迁移使用 `row_security=off`，无全表迁移权限时失败，避免因 RLS 静默漏行。

旧 attempts 混合了等待与异常，无法逆推准确真实失败次数。因此 failure_count 从003开始计数，默认0；旧 attempts、last_error、dead 状态保留。这个明确的一次性计数起点不是“历史真实错误已精确迁移”。已知前提等待原因的旧 retry 转为 waiting；初始依赖指纹为空，在第一次明确通知/处理时建立，可能发生一次额外前提检查。旧 candidate succeeded 和旧残留 active 派生不自动回填。

003 down 若发现 waiting 或非零 failure_count 会拒绝并回滚迁移，要求明确核对待处理工作和预算，不能静默改成待运行或删除诊断。空合成库可 down/up。生产回滚需停 worker 并单独拟定数据处理方案；不能直接下调 schema 后继续新代码。002 down 会丢失上一批来源删除摘要/重存意图，不能拿它代替本批代码回退。

## 验证和剩余范围

真实 PostgreSQL 反例覆盖：除零/CHECK 后目标回滚且错误持久；三次真实异常终止；无新输入时等待后补输出/追赶/绑定重建；重复通知与跨等待保存失败预算；错误终态不被增量、删除清理或输出绕过；正常进程退出后另一个进程接续；迁移保留历史并拒绝丢弃等待/预算。保留先前删除、重存、重建、租户锁序与 acceptance 回归，旧状态断言按等待合同机械更新。

这些是隔离合成测试，不是生产容量、崩溃恢复指标或真实模型质量验收。候选发现语义仍未实现。普通 observe/复合写合同见 [冻结写入](frozen-writes.md)，新建和普通投影版本绑定见 [投影输入绑定](projection-input-binding.md)。来源提取、生产与模型质量仍是独立范围。

## 可运行入口：看到失败而不误判恢复

`run-jobs` 保持原 JSON 结果；本批有 `retry` 或 `dead` 时，打印结果后以退出码 **1** 结束，脚本不能再把它当执行成功。业务 `waiting` 不属于执行失败。`[]` 和退出码 0 只表示本次没有领取到期任务，不能证明失败任务已恢复。

```bash
uv run liveday0 run-jobs "$TENANT_ID" --limit 10
uv run liveday0 run-jobs "$TENANT_ID" --status --limit 20
```

`--status` 从同一租户读取持久未完成/失败任务，每页 1–100 项，默认 10 项；`has_more=true` 表示列表不完整。结果只含任务与目标 opaque ID、类型、状态、次数、时间、白名单等待原因、错误存在标志及安全分类。任意历史 `last_error` 正文不会返回。删除或失效清理产生、没有错误的 `dead` 不冒充执行失败；成功任务不进入该列表。状态按更新时间排序，这个有界窗口不是全库健康报告；查询退出码 0 仅表示只读查询成功。

查询不领取任务、不修改预算、不唤醒 waiting 或复活 dead。出现 retry 时按已存退避再次执行；waiting 按上面的绑定输出/依赖政策继续；有错误的 dead 需先核实原因和恢复方案，本入口没有“清空次数重试”按钮。CLI tenant 参数仍是受信本地开发入口，不是服务器客户端鉴权。

已有维护 SQL 回滚、有限重试和正常跨进程接续证据复用，不重跑。本轮定向 `tests/test_maintenance_cli_failures.py` 在新标记、无 TCP、受限应用角色的合成库中，写入目标版本后注入真实除零：retry/dead 非零退出；新进程空轮询后仍可查询失败；版本和 pending journal 保留，状态查询不改预算，跨租户及已删除任务不返回。末次失败预算由合成夹具预置到 2，未重复三次耗尽测试。这不是断电或提交确认丢失验收。

本机已有 Python 3.13、psycopg 和 `/opt/homebrew/opt/postgresql@17/bin` 时可直接运行该单项验收；退出停止新实例并保留 `/tmp/ld0-job-cli-*` 中的 manifest 与日志：

```bash
uv run --python 3.13 --no-sync python tests/test_maintenance_cli_failures.py
```
