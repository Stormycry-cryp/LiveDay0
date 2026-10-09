# 投影的实际输入绑定

新建、普通更新和删除后重建都先读取不可变输入，锁外合成，提交时在租户写事务内重读并比较。不能将旧正文重新标上当前支持版本。此合同服务可信内部合成器，不证明模型真的使用过输入，也不保证输出语义正确；摘要不是签名、授权票据或外部客户端能力。

## 三个入口

```python
# 新建：先声明目标元数据和完整支持/反证集合。
prepared = service.maintenance.read_projection_creation(
    projection_type="relationship", projection_key="relationship:family",
    scope="family", support_card_ids=support_ids, counter_card_ids=counter_ids,
)
# 到这里读事务已结束。合成在锁外，只依赖 prepared.payload。
body = synthesize(prepared.payload)
projection_id = service.materialize_projection(prepared=prepared, body=body)

# 普通更新：由数据库读取目标当前的完整关联集合。
prepared = service.maintenance.read_projection_update(projection_id)
body = synthesize(prepared.payload)
result = service.maintenance.commit_projection(prepared, replacement_body=body)
```

删除来源后的 invalidated 投影继续使用 `read_projection_rebuild` / `commit_projection_rebuild`，明确提供新的 scope；它们现在共用同一读集/提交校验。普通 update 不接受有内容删除标记的对象，不把该标记清掉。带标记的 active 对象保持现有结果；下一次依赖失效后才能走明确 rebuild。deleted 目标不复活。

沿用不可变类型名 `ProjectionRebuildInput`，其 canonical_input 升级为 `liveday0:projection-input:v2`，包含 mode=create/update/rebuild。旧版本进程中持有的 v1 prepared 必须重新读取和合成，不能升级字符串后重用旧输出。payload 每次访问返回脱离原对象的结构。

## 冻结范围与提交

- 目标：tenant、ID、type、key、scope、版本、生命周期、epistemic_state、内容删除标记是否存在。新建先生成未落库的 UUID，版本0/lifecycle=absent，并检查 key 空闲；读取不预留 key，提交再验证，竞争者占用则冲突。
- 依赖：完整 support/counterevidence 集合与角色，各 canonical 身份/type/版本/生命周期；可用项还包含 canonical_key、正文、valid_at、epistemic_state。闭合、失效、已删等不可用项只保留身份及状态，不将其旧正文交给合成器。
- 来源：每项 canonical 的完整 evidence ID/source_role/version/status 集合。此阶段不读取原始来源正文；原始来源的变更必须遵守其版本/删除写入合同。相关来源增删、角色或状态改变都会使读集不同。
- pending：可用 support 或 counter 有任何 pending delta 时拒绝读取/提交，先 canonical catch-up。已擦除或其他不可用依赖的残余 delta 不进入语义输入，不阻止剩余有效支持的删除后重建。至少需要一个可用 support；新建声明的每个依赖都须可用。
- 不冻结 tenant 全局 revision、无关对象或更新时间，避免无关变化造成冲突。当前投影旧正文不提供给合成器；本合同输入是 canonical 依赖和目标元数据，不复制已失效解释。

提交先脱离可变输出对象，在租户门禁内检查真实 dead 任务，再重读并完整比较 prepared。变化即抛 VersionConflict/NotFound，目标、版本、关联、revision 和任务状态均不写。成功时从该读集写入 support_versions、counterevidence_versions 和输入摘要，不能由输出伪造这些字段。普通更新可显式指定 replacement_scope；比较的仍是读取时的原 scope，输出新 scope 随原子提交写入。新建不允许提交时换 scope。现有 dormant 投影保持 dormant，invalidated 合法重建后 active。

新建的依赖边与正文、后续版本替换、支持关系及完成任务标记一起提交。直接 API 遇 SQL 错误整个事务回滚；不把其调用次数加入 worker.failure_count，沿用既有直接 rebuild 合同。未做进程强杀/提交确认丢失演练。

## 旧入口与维护

裸 `materialize_projection(projection_type=..., body=..., support_card_ids=...)` 显式拒绝；必须先 read_projection_creation，再传 prepared。非空 `run_ready(projection_outputs=...)` 在领取或唤醒任何任务前报错。CLI 旧参数也不能绕过这一检查；本批不新增 JSON prepared 的外部授权接口。没有自动给旧正文补读集的兼容 wrapper。

worker 在缺少绑定输出时持久 waiting，不再从当前版本复制正文并标注新 support_versions。pending、来源擦除分别保留 dependency_pending / version_bound_rebuild_required，其余为 semantic_output_required。依赖变化通知与显式 resume_waiting 保留，空轮询不重复领取。有效绑定提交原子完成原 live/waiting 任务，保留 attempts/failure_count；真正 dead 不被新输入或绑定提交复活。普通输出现在通过显式 bound commit 完成等待，不另设输出队列或唤醒后再交付窗口。

## 兼容与验收范围

无新数据库迁移，001–004 原文不变。切换前停止旧写入进程/worker；旧程序依然会通过旧逻辑发布裸输出，因此不能混跑。已持久化的旧投影不自动重写、补摘要或证明其内容有效；下次目标维护使用新合同。历史数据修复仍需独立方案。

真实隔离 PostgreSQL 用例覆盖新建/更新的过期 canonical、来源、支持/反证集合、目标元数据、pending、key 竞争、双提交、删除锁序、SQL 整体回滚、输出对象变更、等待与失败预算，以及无关变化不误冲突。既有删除后重建与来源原子性继续回归。测试 helper 只供合成静态夹具使用，显式读后提交，不是生产兼容入口；冻结历史原件保留，调用适配副本及差异独立归档。

独立开放项仍包括：普通 observe 完整结果 receipt；按已保存 evidence 身份安全发起新解释的明确 API；删除各入口对旧 reobservation intent 内容摘要的统一擦除审计。来源提取/provider/真实模型质量、旧 heldout/v3c、生产迁移与完整运维不在本批。
