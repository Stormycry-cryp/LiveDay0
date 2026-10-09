# 来源回执、独立解释与撤销

本合同面向内部 MemoryService。当前无真实提取器、聊天入口或用户身份验证适配器；CLI 接收调用方给定的 tenant、证据及语义 JSON，不是已鉴权产品入口。secret/never-store、主体/引语、敏感内容持久化许可必须由接入层在调用 core 前验证；这里的结构校验不能替代这些判断。

## 写入和重试

`observe` 首次写入在同一事务保存 `observation_receipts`，包括 evidence_id、可空 trace_id、原顺序 card_ids。精确重试返回原身份，只有 created=false；不从可变的 card_sources 重建，不新增行或推进 revision。回执仅证明首次结果身份，不能证明卡现在仍有效。无可信旧回执或旧冻结摘要时明确冲突，迁移不回填猜测结果。

`read_evidence_interpretation` 读取当前来源真实字段；`commit_evidence_interpretation` 接受冻结读集、opaque UUID intent、有序 semantic proposals/trace 和最小 provenance。生成在锁外；提交在租户写门禁内重读并比较来源、状态、内容版本、撤销 epoch 及实际输入。新增解释、关联、intent 回执、局部任务通知和 revision 一起提交。它不再插入 evidence，不修改原 observe 回执。相同 intent/请求返回原结果；更换内容、来源、顺序或 provenance 明确冲突。已有 canonical key 或已有 source trace 明确冲突，不覆盖、自动合并或悄悄改名。每个来源仍至多一个 life_trace。

输入和输出分别限制为规范序列化后的 64,000 UTF-8 字节；输出至多 16 张卡，超限拒绝而不截断。provenance 仅接受 producer_id/model_id/extractor_version/policy_version 的有界 ASCII 标识；不保存 prompt、模型原始输出或正文。来源字段的真实性和模型解释正确性不由此 API 证明。

## 删除与来源解释权

删除卡时，对当前来源边和原回执能证明的来源设置 interpretation_revoked；保留原文/status 和其他有效卡。撤销的是未来自动解释及相关重放能力，不是把同源所有卡删除。现有 canonical 卡仍可作为有效投影支持，也可用新来源明确纠正。所有以旧来源新增 delta、correct/close、mention/bind、evidence/trace 关系或 discovery 的入口经过统一检查。

`source_interpretation_revocations` 只保存 tenant/evidence 和被删对象的 opaque UUID。每个不同来源＋删除对象首次推进 epoch 一次；重试同一删除只补清摘要，不再次推进。epoch 是撤销事件计数，不是用户点击次数：一次 source 删除可以同时记录 source 和其卡的删除。标志不会因一次恢复清除，也不会降低 epoch。它不改变 evidence 内容版本。

清理包括 evidence.request_fingerprint/model_interpretation、该源 event_deltas.request_fingerprint、普通回执状态、interpretation intent 摘要/provenance、reobservation intent 摘要/状态。原回执 opaque IDs 保留作重放拒绝与删除追溯。card 删除不清其他有效卡正文；source 删除继续擦除来源、trace、卡及既有直接派生/快照。旧 source key 的身份摘要用于阻止自动重放，不得由生活正文 hash 充当来源标识。任意新 key 或改写复制文本的语义重复检测未实现。

缺少可信 span 时撤销整个来源的未来自动解释。迁移只能从仍在库中的 deleted evidence 和 deleted card_sources 证明历史撤销；无法从已丢失的历史边恢复范围，不宣称精确句级撤销。

## 一次明确恢复

普通 commit 没有 allow_revoked 开关。`read_explicit_reinterpretation` / `commit_explicit_reinterpretation` 默认拒绝；须由可信宿主注入只读 authorizer，验证真实人类的新明确保存动作。模型自己传 explicit_save、UUID 或 provenance 字段都不是授权。

宿主分别检查读取范围和本次完整冻结请求（来源读集、intent、trace、有序卡及 provenance）。若原指令未明确内容，宿主先取得对具体结果的确认；不得把长期泛化许可当作恢复任意内容的授权。检查发生在数据库锁外，不能在 callback 内消费授权或执行有副作用的批准操作；只有最终数据库 intent 与结果一起落库。SQL 失败仍可用相同冻结请求重试。真实宿主适配器尚未接通，测试仅使用精确请求 allowlist。

明确恢复只生成新卡，不复活已删 ID、不激活旧回执。source 保持 revoked，下一次自动解释仍拒绝。新的删除推进 epoch，使旧 prepared/未提交授权失效，并擦除相关 intent 摘要和 provenance。source 已 deleted 时此入口拒绝，继续使用现有 reobserve_deleted 的新来源与新明确意图合同。

## 005 迁移与运行边界

迁移要求停掉所有旧写进程，使用离线迁移身份。005 增加三张 tenant-scoped FORCE RLS 表、evidence 撤销字段及 reobservation revoked 状态，清理能证实已删除来源的旧摘要；不补造 request fingerprint 或 observe receipt。旧摘要一经擦除不可恢复。

Down 仅在全库不存在新回执、intent、撤销事件/状态时允许执行。任何租户有这些状态都会在事务内拒绝；回退不能静默丢弃删除保护。测试为验证更早的 003/004 guard 会显式清除合成 005 fixture；该辅助函数不是生产降级方案。真实存量迁移、混合版本运行、断电/WAL 恢复和提交确认丢失演练未因此获得验证。

新增解释会发出局部 candidate_discovery 通知，现有 worker 仍明确未实现语义发现；本批没有接入 LE、真实模型、多 provider 或后台自动重新提取。旧排队通知不构成绕过撤销门禁的许可，未来 worker 必须重新读取并使用同一提交合同。
