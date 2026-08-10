# v1 公开真人语料召回基线

测试日期：2026-08-10（Asia/Shanghai）。本基线验证“稳定上下文 + 正在发生的现在 + 未完成连续性”，不是人物传记、公开账号画像或帖子归档。

## 数据与边界

- 38 条最小证据，来自 Stack Exchange 13 个公开线程、24 个唯一问题或回答；The Workplace 20 条、Bicycles 9 条、Seasoned Advice 9 条。
- 每条保留独立 `source_id`、原链接、平台/站点、来源和采集时间、CC BY-SA 4.0 使用边界、API/robots 边界、去标识说明和不超过约 25 词的最小片段。
- train/dev/test 为 5/15/18；同一问答线程的所有片段固定在同一 split，审计未发现跨 split source group。
- `confirmed` / `assumption` / `to_validate` 为 24/3/11。回答者建议始终属于另一个文档内主体，不会变成提问者事实。
- 官方 Stack Exchange API 在线复核为 24/24 条目可用；联系方式/PII 命中 0，高敏内容命中 0，重复/近重复 0。
- 没有读取私人内容、登录态内容、已删除内容，也没有保存用户名、公开用户 ID、联系方式、精确位置、雇主、薪酬或健康/金融敏感内容。数据说明见 `benchmarks/public_human_recall/README.md`。

## Benchmark 与门槛

八类场景分别覆盖稳定上下文、变化中的现在、未完成连续性、纠正覆盖、删除传播、时间相关性、噪声抑制和跨人隔离。召回主指标按一次有界召回包计算：直接进入上下文的完整卡和可展开引用都算进入候选包，但两者分别报告。

通过门槛：必要项包内召回率至少 0.90；八类全部通过；噪声、旧纠正解释、删除复活和跨人泄漏均必须为 0。失败分类另含认识状态坍塌与来源边界违规。

## 原始 v1 结果与最小修复

未修改检索代码的首次运行仅 2/8 类通过，必要项 7/15，漏召 8，噪声侵入 2；删除复活和跨人泄漏均为 0。

证据定位出两个实现缺口：

1. 相关性把 JSON schema 字段名也拼进搜索文本，英文长词的分散二元片段会在一整张长卡里错误拼成相似命中。
2. 完整卡压缩仍重复携带身份和来源元数据，使语义主体已经足够短时仍可能不必要地落入 expansion handle。

最终修复只搜索语义值；非中文词使用精确包含，中文保留至少两个局部二元词组命中；短表示保留 ID、类型、生命周期/认识状态、时间、版本、来源引用和必要语义字段，移除重复身份元数据。样本、期望项和通过门槛没有因失败而放宽。

## 最终结果

- 8/8 类通过；必要项包内召回 15/15（1.00）。
- 11/15 直接进入模型上下文（0.7333），4/15 作为可展开引用；该差异单独保留，不拿包内召回冒充直接上下文覆盖。
- 噪声侵入 0，纠正后旧解释 0，时间过期状态 0，删除复活 0，跨人泄漏 0。
- 删除场景从两条来源传播到两张规范卡及其派生投影；删除后证据正文/对象引用残留 0，只保留 2 个证据与 2 个卡片的无内容 marker。
- 数据集审计、在线来源审计和确定性 benchmark 均通过；没有模型调用、向量 embedding 或付费 Provider。
- 最终 PostgreSQL 集成测试为 `22 passed`、`0 skipped`。

机器可读结果：`benchmarks/results/public_human_recall_v1.json`。

## 现有 160 卡检索基线复跑

同一 PostgreSQL 17.10 隔离环境复跑 `benchmarks/benchmark_recall.py`。默认 180/1600 token、48/24/12 候选配置的 30 次召回中位/P95/最大为 55.69/89.29/93.03 ms，11 张卡直接入包、1 个省略引用，仍低于 800 ms 本地超时门槛。环境没有 pgvector，因此不是向量质量或生产延迟证据。

完整输出：`benchmarks/results/existing_recall_baseline_2026-08-10.json`。

## 复跑

```bash
export LIVEDAY0_DATABASE_URL="postgresql://postgres:<local-password>@127.0.0.1:55433/liveday0"

uv run python benchmarks/public_human_recall_benchmark.py --audit-only
uv run python benchmarks/benchmark_recall.py
uv run pytest -q tests/test_public_human_recall_benchmark.py
uv run pytest -q
```

在线 API 复核是 2026-08-10 的历史验收证据，不是当前建议复跑项；后续自动化访问需先重新审核 Stack Exchange 的现行政策。

## 限制与下一步

- v1 只有一个公开许可网络、13 个线程和英文片段；它不能代表真实人群、长期关系自然度或多语言总体质量。
- 为避免公开账号画像，`subject_id` 只在单个问答线程内有效；本轮没有跨站或跨帖拼接同一个真人的生活轨迹。
- 4/15 必要项需要 expansion handle。后续应在不增加敏感采集的前提下扩充同一公开线程内的时间更新，并单独观察直接上下文覆盖率，而不是调低安全门槛。
- 没有验证图片、pgvector、长期图规模、生产服务、常驻 worker 或最终回复质量。
