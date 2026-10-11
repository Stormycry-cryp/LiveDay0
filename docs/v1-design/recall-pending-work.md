# Pending 与查询追平的有限工作合同

本轮接续 `f8c6873` 来源引用工作门，只处理用户查询中 pending journal 与 read-before-use 的工作量，不增加模型调用、存储或后台框架。

## 入口与超限行为

`RecallOptions.pending_delta_limit` 默认128、最大4096；`catch_up_target_limit` 默认8、最大32，均为非布尔正整数。CLI 对应 `--pending-deltas`、`--catch-up-targets`。复用既有 ContextVar 查询周期：规范追平与有效事件 overlay 消耗**同一个剩余 pending 行预算**，不是每张卡重新获得128。两个读取入口的 SQL 都以剩余行数加一条哨兵为 LIMIT，先检查溢出再合成或写入；哨兵不进入正文。一次周期累计载荷行最多预算加一条溢出哨兵，零行查询不消耗预算。它约束行数，不约束单行 JSON 字节或数据库扫描工作。

追平先读取最多目标限额加一条的任务身份元数据。仅选择本租户 active/provisional 事件中有 `requires_restructure` pending 的 event_rewrite：pending 或已到期 retry；保留等待、退避和终止失败门禁。这里的“选择”是有限 unsafe 任务集合，尚不是按当前 query 相关性排序。超出目标门时在 ready marking/claim 前拒绝；因此一个租户大量不相关 unsafe backlog 也可能暂时阻止整次 recall。后台维护独立消化或调用者选择较大的有限预算后可重试，不能偷偷扩展本次工作。

未超出时只调整已选 pending 任务的到期时间，执行器再按**已选 job ID**和 event_rewrite 类型认领，最多原目标限额；没有任务则直接返回。不再顺带执行 candidate discovery、projection 或安全 quiet-time rewrite，也不处理门禁间隙新出现的其他任务。

超限 API 抛 `RecallMaintenanceRequired`（`RecallWorkLimitExceeded` 子类），reason 为 `pending_delta_limit` 或 `catch_up_target_limit`。CLI exit2 返回静态 `error=recall_maintenance_required`、`retryable=true`、`maintenance_required=true`、`context_available=false`，无记忆正文、原始 SQL 或半成品上下文。编译不发布新快照；当前追平 claim 和目标写入回滚，不增加 attempts/failure_count，不丢 pending，不把上限当作合成失败。已选任务的 ready 时间可能已独立提交；之前合法完成的另一事件追平也可能已提交，**不承诺整次 recall 零写或全局回滚**。

精确上限时安全 pending 编译为一张 coherent effective-event card，完整出处/限定保留，规范版本未改；unsafe 则原子替换规范版本并吸收整组 pending。已知不能安全 overlay 的事件在失败/退避时仍由既有 unsafe 排除门隔离，允许 unaffected context 或空历史，不返回旧事实冒充完整。更正和删除继续走原硬失效合同：更正可有意 invalidate pending，删除后不能凭追平恢复已删事件，旧 pinned snapshot 不得再展开。

上下文 finally 复位，新周期有新的有限额度；嵌套周期不能延长截止点或增加剩余行/目标 allowance。目标数量按预选任务累计扣减；任一工作门溢出会封闭整个周期，外层即使捕获异常也不能再次读取或认领，退出时仍明确拒绝。独立 `run-jobs` 没有本查询行预算，本轮不宣称其全工作量已封顶。已有800ms协作 deadline、16KiB完整返回及64KiB展开合同不变，也没有严格墙钟保证。

## 定向证据

[固定夹具](../../tests/fixtures/recall_pending_work_cases.json)首次执行前经独立预检，SHA256 `44b02937fdf3b12746b18f9df15b1aa05bfe0918ba3baa1f0f0253674e775ee2`。基线实际运行14检查：1条既有真实 SQL rollback 门通过，其余13条缺少新工作门或顺带维护，保留 RED；不是用不存在的 CLI 参数制造失败。

[单项验证](../../tests/test_recall_pending_work.py)14条主检查涵盖 safe3/cap2与3、两safe2共享cap3与4、unsafe3/cap2与3、两unsafe1目标cap1与2、无关到期任务不执行、真实 `SELECT 1/0` 在 rewrite 写入后触发保存点回滚且 failure_count=1、新有限 retry 恢复、更正/删除后旧快照拒绝及实际 CLI 静态拒绝。补充3检查记录真实目标元数据2行/LIMIT2、unsafe1追平和safe2 overlay共享cap2拒绝（早期合法提交保留）、新周期恢复。无 I/O 方法验证8个非法配置、嵌套 deadline/行预算不得重置、目标累计扣减与捕获超限后整个周期仍封闭。

主14项首次 GREEN `/private/tmp/ld0-pending-work-hodsv56i/manifest.json`；补3项 `/private/tmp/ld0-pending-work-4q7ifm5o/manifest.json`；job ID 筛选改动仅重验受影响补项，独立窄审发现并修复嵌套周期重复使用预算后，最终17检查与无I/O方法全过，补3项 `/private/tmp/ld0-pending-work-u_5ic8dn/manifest.json`、主14项 `/private/tmp/ld0-pending-work-nqr6_5do/manifest.json`；先前版本及完整验证清单也保留在任务目录。RED `/private/tmp/ld0-pending-work-3zu72cqb/manifest.json`。每次均为新空标记PG、私有0700 Unix socket/no TCP、受限应用角色；停止且 PID 不存在，保留全部目录/日志，不重启旧库。旧来源、字节/时间、生活输入质量及删除证据以原提交复用，不重跑全仓、heldout 或真实模型。

## 长历史工作量合同的有限闭合检查

这里只核对已有入口与本轮差异，列完即止，不把剩余表转成自动执行的新任务。

| 维度 | 已有上限/合同 | 仍缺的上限与准确范围 |
|---|---|---|
| 返回与显式输入 | context16KiB、展开64KiB、query2000字符、current refs48；整体语义单元或拒绝 | Python单步解码/序列化不可硬中断；CLI/transport不含在800ms工作deadline，严格墙钟未封顶 |
| 候选与出处 | 候选48、选中12、关系视图24/每类6；selected出处总256行；有SQL取消 | SQL扫描/排序/FTS/vector发现的CPU、内存和扫描行未有硬界；全局unsafe ID集合仍无行限额，含退避/终止未追平目标 |
| Pending 与查询追平 | 本轮共享128载荷行、8预选任务，加一哨兵拒绝；预选任务身份固定 | 单条canonical body、delta JSON、source_kind等传输/解码字节未封顶；独立后台rewrite的pending总量没有本查询行限额 |
| 邻域与派生处理 | 顶层返回数与整包预算已有界；已知失效不服务 | projection support聚合、mention candidates、trace聚合、追平触发dependent invalidation/排队等内部扇出仍无共享行/字节上限；顶层LIMIT不等于子集合封顶 |
| 持久增长与运营 | 显式snapshot release/保留dry-run、小规模合成恢复已有证据 | journal、来源、版本、快照与任务累计存储/保留容量、自动清理及生产恢复RPO/RTO仍未封顶或验收 |

结论是本轮 pending/read-before-use 缺口关闭，完整长历史资源合同仍部分开放。小样本 GREEN 不证明生产容量、中文同义泛化、人物/关系长期质量或真实模型提取质量；旧一次合成真实调用的 mismatch 标记不变。
