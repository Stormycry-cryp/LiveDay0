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

active 快照的退出机制仍未实现，因此活跃快照增长会继续被保护并触发行数提示；本预览没有解决这部分增长。来源删除和纠正已经使旧快照失效/擦除的规则保持不变，不能以年龄窗口代替删除语义；防复活标记也不能因变老而清除。候选只是一次观察，未来若实现清理，必须在写门禁内重新校验当前状态、时间和调度锚点，不能盲用这里保存的 ID。

命令强制 `--dry-run`，没有 apply/delete/expire 入口。这次没有对真实或已有库执行清理，没有改变用户记忆保留期，也没有引入新表、迁移、调度框架或存储。历史任务即使满足候选条件也不会在本次预览中删除。

本机已有 Python 3.13、psycopg 和 `/opt/homebrew/opt/postgresql@17/bin` 时，可运行单项合成验收：

```bash
uv run --python 3.13 --no-sync python tests/test_retention_preview.py
```

验收新建标记空库、私有 Unix socket、无 TCP；owner 只初始化/迁移，固定合成夹具及预览使用非超管、无 BYPASSRLS/建库/建角色/继承权限的应用登录。覆盖无配置零候选、已知/未知失效时间、活跃状态、失败历史、锁/等待痕迹、最新任务与同时间 UUID 排序、年龄窗口、样例上限、两租户隔离、没有 apply 入口。两租户相关 17 张表在预览前后摘要一致；退出停止新实例，保留 `/tmp/ld0-retention-*` 中的 manifest 和日志。既有删除/幂等/重启测试未重复运行。
