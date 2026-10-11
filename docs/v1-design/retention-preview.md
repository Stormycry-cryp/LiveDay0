# 任务与快照的只读保留预览

任务历史和召回快照目前没有自动保留上限；`expired` 只是已有快照状态，不代表自动过期已实现。本入口先让操作者看到增长、候选和保护原因，不执行清理，也不把统计告警当作硬容量治理。

```bash
# 未设置年龄：只统计，零候选
uv run liveday0 retention "$TENANT_ID" --dry-run

# 显式提出操作记录的年龄窗口，仍然只读
uv run liveday0 retention "$TENANT_ID" --dry-run \
  --snapshot-days 30 --job-days 30 --row-warning 10000 --sample-limit 20
```

天数可为 1–36500，省略即禁用对应候选策略；没有默认 TTL。`row-warning` 为正整数，仅在对应表的总行数达到阈值时标注，不阻断写入。`sample-limit` 为 1–100、默认 20。年龄配置只是本次预览参数，不保存为线上保留政策。CLI tenant 参数仍属于受信本地开发入口。

同一 SQL 读取快照返回本租户两张操作表的总量、候选量、保护量、保护原因，以及有限候选 ID/时间样例。`samples_truncated` 明确表示样例不是全集。查询有 2 秒 SQL 执行时限，统计是显式操作员检查，不在每轮会话执行；未验证大规模扫描的性能。查询不读取记忆或依赖正文；coalesce_key 和错误字段仅用于判断保护边界，不返回这些字段的正文。

| 记录 | 候选条件与保护边界 |
|---|---|
| 召回快照 | 仅 inactive 且有已知 `invalidated_at`，从失效时间起超过显式窗口；active 即使很老也保护，失效时间未知也保护 |
| 完成任务 | 仅 event_rewrite/projection_resynthesis 的旧 succeeded，失败计数 0、无错误、等待或锁定痕迹，且同 coalesce_key 已有更新任务 |
| 最新任务 | 每个租户/coalesce_key 保留 `created_at DESC,id DESC` 的最新锚点，与调度器一致；UUID 同时序决胜也一致 |
| 未完与失败任务 | pending/running/retry/waiting、所有 dead 和任何失败历史均保护，不重置次数、不复活任务 |
| 来源与生活记忆 | evidence、普通痕迹、规范卡片与版本、pending delta journal、派生支持、来源回执和内容无关删除标记均不参与此策略 |

调用者可在推理与展开周期确实结束后显式释放对应快照，见下文。尚未释放的 active 快照继续被保护；没有默认超时、自动清理或硬上限。来源删除和纠正已经使旧快照失效/擦除的规则保持不变，不能以年龄窗口代替删除语义；防复活标记也不能因变老而清除。候选只是一次观察，未来若实现清理，必须在写门禁内重新校验当前状态、时间和调度锚点，不能盲用这里保存的 ID。

`retention` 命令强制 `--dry-run`，没有 apply/delete/expire 入口。这次没有对真实或已有库执行清理，没有改变用户记忆保留期，也没有引入新表、迁移、调度框架或存储。历史任务即使满足候选条件也不会在本次预览中删除。

本机已有 Python 3.13、psycopg 和 `/opt/homebrew/opt/postgresql@17/bin` 时，可运行单项合成验收：

```bash
uv run --python 3.13 --no-sync python tests/test_retention_preview.py
```

验收新建标记空库、私有 Unix socket、无 TCP；owner 只初始化/迁移，固定合成夹具及预览使用非超管、无 BYPASSRLS/建库/建角色/继承权限的应用登录。覆盖无配置零候选、已知/未知失效时间、活跃状态、失败历史、锁/等待痕迹、最新任务与同时间 UUID 排序、年龄窗口、样例上限、两租户隔离、没有 apply 入口。两租户相关 17 张表在预览前后摘要一致；退出停止新实例，保留 `/tmp/ld0-retention-*` 中的 manifest 和日志。既有删除/幂等/重启测试未重复运行。


## 显式结束一个召回周期

```bash
uv run liveday0 release-snapshot "$TENANT_ID" "$SNAPSHOT_ID"
```

内部调用为 `MemoryService.release_snapshot(snapshot_id)`。调用者只在该 pinned reasoning-and-expansion cycle 已完成后调用；不能用批量年龄规则结束仍需展开的周期。它将本租户指定 active 快照原子转为 `expired`，记录结束时间（复用 `invalidated_at`），清空 context、expansion_store、referenced_ids 和 degraded_reasons。保留快照行和 ID，便于状态追踪与后续只读保留预览。

成功首次返回 `released=true`；重复或已 invalidated/expired 的调用返回 `released=false`，原状态与结束时间不变。跨租户和不存在 ID 均为 NotFound。其他 active 快照、租户生活版本、来源、规范记忆、待吸收 journal、回执和删除标记不变。释放与展开复用租户读写门禁：先展开则可完成此次读取；先释放则后续展开要求新快照。事务失败不留下半清空副本。

该入口释放的是服务端操作缓存，不能撤回已交给调用者的上下文，也不删除生活记忆。下一周期重新 recall 可继续获取有效记忆。来源删除、纠正和边界变更的强制失效优先于正常结束；释放不会将 invalidated 改写为 expired。

新增单项 `tests/test_release_snapshot.py` 在新标记空库、私有 socket、受限应用角色中验证真实 CLI、新周期保留、两种展开/释放锁序（通过 PostgreSQL blockers 证明）、提交前 SQL 故障回滚、幂等和租户隔离。未重复既有删除或重启验收。释放可回收快照中的缓存内容，但行仍保留；未调用释放的活跃副本、操作行数和任务历史仍没有自动增长上限，大规模统计性能也尚未验收。


## 有界规模观测

2026-10-11 的 365/3650 事件两档基线在较大档 10950 个任务时复现保留统计 2 秒超时：逐任务相关子查询重复搜索更新任务。最小修复用本租户 `coalesce_key` 分区的 `row_number()`、`created_at DESC,id DESC` 一次排序确定最新锚点，保护规则和返回字段不变。受影响保护边界单项重新验收；未重跑无关入口。规模和采样合同、耗时及内存口径见 [有界本机基线](../../benchmarks/bounded_local_scale/README.md)。这不增加自动清理、硬上限或记忆年龄政策。
