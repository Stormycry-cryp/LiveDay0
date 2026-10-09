# 完整请求幂等与复合写入原子性

同一来源 key 的重放必须等价于首次接收的完整请求。复合命令失败时，其新证据和目标修改一起回滚，调用者不会收到失败却留下不知情的孤立来源。

## 来源请求

`observe` 在租户门禁前深拷贝请求，在事务内先检查来源删除摘要，再插入或比较 `evidence.request_fingerprint`。摘要域为 `liveday0:observe:v1`，包含 tenant_id、EvidenceInput 全部字段、完整 trace 和有序 SemanticInput 列表。

- EvidenceInput 包含 modality、source_kind、content、object_ref、occurred_at、image_observation、sending_context、model_interpretation、embedding、idempotency_key。
- 每项 SemanticInput 包含 card_type、完整 body、lifecycle、epistemic_state、canonical_key、valid_at。trace 的额外字段也参与，不从实际落库的子集反推原请求。
- 沿用 canonical_json：字典键排序、时间转 UTC 微秒表示、列表保序；不清洗字符串、不合并 None/空字符串、bool/int/float。NaN/Infinity 拒绝。时间必须是带时区的 datetime，不能依赖连接的默认时区解释 naive 时间。
- 请求 key 为 None 时每次新建，不存幂等摘要；空字符串是有效 key。重试必须保留第一次的 occurred_at/valid_at，重新构造默认当前时间是不同请求。
- 同 key 同请求返回已有 evidence_id、created=False，不写新 trace/card/revision。保持旧返回形状：重复请求的 trace_id=None、card_ids=[]；这不是完整结果回执重放接口。已有 semantic 被纠正后也比较原请求摘要，不比较 current body。
- 异请求或旧无摘要 key 抛 `IdempotencyConflict`（VersionConflict 子类）；错误不包含来源正文或 key。无摘要不能证明等价，禁止从现有来源和派生记录逆造摘要。

## 复合命令

`add_event_delta`、`correct_card`、`close_card`、`create_unbound_mention` 在同一个租户写事务内调用 `_observe_conn`。目标校验、版本检查、来源写入、关联、版本、delta、失效、维护任务和 revision 同生共死。失败不删除此前独立成功的来源。门禁内不运行模型或网络请求；可变正文和 candidates 在等门禁前脱离调用者对象。

这些入口内部观察来源时 trace=None、semantics=[]，因此复用来源必须与这个完整观察请求一致；先前带 trace/semantic proposal 的 observe 请求不能通过省略这些字段伪装成同一次观察。旧来源如果需要新解释，应使用明确的新输入意图，不自动换 key 重试。

delta 摘要域为 `liveday0:event-delta:v1`，包含 tenant_id、event_id、evidence_id、delta key 和冻结正文的 canonical JSON 字符串。摘要保留 JSONB 可能归一化掉的 int/float 区别。同 event/key 的同来源同正文返回原 delta_id、created=False，不重排队、失效或增 revision；异来源/异正文/legacy 无摘要明确冲突，新来源随事务回滚。匿名来源每次是新 ID，不能冒充旧 delta 重放。已 absorbed 或因纠正 invalidated 的同请求重放不重新激活 delta。

纠正/关闭仍检查 expected_version，不把已成功的旧版本纠正当成功重放。mention 没有新增操作 ID，也不新增命令级去重；同来源再次创建 mention 仍是新操作。本批只保证它的失败原子性。

## 删除、升级和回退

删除来源时清除其请求摘要及相关 delta 摘要，沿用内容无关的来源删除身份摘要。删除 card 时同时清除它所关联来源的观察摘要，因为原观察可能包含被删除的 semantic proposal；也清除目标 delta 摘要。来源还在、摘要已擦除时，旧 key 明确冲突。这是保守的可用性代价，不能恢复或推测被擦除的摘要。原有 reobservation intent 合同不变，来源删除仍清除 intent 摘要。

004 只添加两个 nullable、64 位十六进制摘要列，不回填旧来源或旧 delta；现有行 null 即 legacy。必须先停止所有旧版写入进程/worker，再迁移并启动同版本代码；旧代码不能与新代码混写。测试未对用户现有数据库执行迁移。

004 down 使用 row_security=off 做全表检查；任何请求摘要尚存即拒绝，以免静默丢失幂等证据。只有显式另行协调后才能回退，不能为回退自动清空用户摘要。空库 up/down/up 可重复；003 down 对 waiting/失败预算的保护保持不变。

## 验证和未完成项

合成验证覆盖逐字段冲突、UTC 等价、深拷贝、并发同 key、旧摘要缺失、SQL 中途失败、四类复合命令、删除前后锁序、delta 重放及无副作用。数据库事务保证原子提交；本批没有做进程强杀或提交确认丢失的故障演练。

普通投影的完整实际读集合绑定是下一窄批，旧裸 projection_outputs 仍依赖可信调用者；本批没有将其升级为版本绑定。来源提取/provider/真实模型质量、旧 heldout/v3c、生产迁移、普通维护 dead 的恢复政策与长期运维仍是独立范围。
