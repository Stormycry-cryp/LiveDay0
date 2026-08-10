# Dataset + Benchmark v2 公开真人召回基线

验收日期：2026-08-10（Asia/Shanghai）。起点为分支 `codex/public-human-recall-benchmark` 的提交 `488fdb2a791e678caa87983dc3a4e9342aa590e8`；未修改代码的冻结基线先运行，之后只修复候选合并和明确 applicability 排除。没有改样本、放宽标签、跨 split 调参或在 held-out 结果后继续改代码。

## 数据集与来源边界

- 10,032 条公开真人最小证据，5,587 个 source groups，4 个独立来源族。
- 来源：Wikimedia 6,656、Fedora Discussion 2,043、Zcash Community Forum 1,295、冻结的 Stack Exchange v1 38。
- 语言：英语 3,700、西班牙语 5,658、中文 674。
- 领域：变化 257、创作 608、决策/偏好 57、家庭/食物 59、学习 54、维护/修复 1,450、计划/未来 275、社区/关系 5,781、日常习惯 36、任务/项目 961、旅行/地点 145、工作 349。
- train/test/held-out 为 6,783/1,532/1,717；按来源族、线程/讨论页、页内说话人和时间整体切分，source group 与 source-local person 泄漏均为 0。
- 联系方式、精确定位、高敏、未成年人、精确重复和 0.90+ token-Jaccard 近重复命中均为 0。

数据卡、许可/归因、robots/API/dump 边界、去标识方式和删除传播入口见 `benchmarks/public_human_recall_v2/README.md` 与 `source_audit.json`。这不是公开账号画像：用户名/公开用户 ID 不落盘，subject 只在一个 source group 内编号。

## Benchmark 设计与 case 数量依据

360 个确定性 case 使用 360 个互不重叠的 source groups；test 与 held-out 各 180。18 类各 20 例（每个 split 各 10），覆盖稳定事实、当前变化、未完成承诺、纠正/矛盾、否定、删除传播、时间衰减、噪声抑制、跨人隔离、跨帖隔离、多语言/混合语言、关系变化、重复事件、代词/隐式引用、近重复干扰、过期解释、开放未来和长期连续性。

每个 split 的语言构成为中文 36、英文 72、西班牙文 72。180 个独立 case 的二项比例在观测 1.0 时 Wilson 95% 下界为 0.9791；因此整体 Recall@10 能排除约 2% 以上的常见漏召，但单个 20-case 类别仍只适合发现明显失败，不适合宣称很窄的类别级误差界。

真人证据与生成内容严格分层：case 查询、标签、纠正/删除操作、重复事件和近重复扰动都是 `generated_benchmark_annotation`，不冒充来源作者的真实经历。

## 冻结基线与修复

最终数据和 case 冻结后，通过从提交 `488fdb2` 导出的原始 `src/liveday0` 运行 1k-card test：Recall@10 0.0667（95% CI 0.0385–0.1129），MRR 0.0182，nDCG@10 0.0283，直接上下文覆盖 0.0111，168 个必要项漏召；中位/P95/最大延迟 30.32/34.75/46.06 ms。

根因是原实现虽然查到了 FTS card IDs，随后却只加载“最近 `candidate_limit` 张卡”打分，规模化后旧卡系统性丢失。修复后：

1. 按 FTS rank、CJK/混合语言 lexical match、vector lane 顺序合并有界 target IDs，再用最近卡补足候选预算。
2. lexical lane 只读取同租户 active evidence，按实际命中 fragment 数排序；没有 provider、embedding 或自由全库模型遍历。
3. `applicability=excluded_from_personal_continuity` 的明确边界不进入生活连续性上下文。

## Test 结果

| 卡规模 | Recall@10 (95% CI) | MRR | nDCG@10 | 直接上下文 | Expansion 依赖 | 中位 / P95 / 最大 ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.0000 (0.9791–1.0000) | 0.3645 | 0.5082 | 0.3833 | 0.6167 | 36.33 / 47.63 / 55.20 |
| 5,000 | 1.0000 (0.9791–1.0000) | 0.3843 | 0.5093 | 0.3667 | 0.6333 | 55.57 / 78.81 / 199.84 |
| 10,000 | 1.0000 (0.9791–1.0000) | 0.3807 | 0.5044 | 0.3667 | 0.6333 | 78.61 / 106.31 / 238.73 |

纠正/过期解释、删除复活、跨人/跨帖泄漏、噪声/近重复侵入均为 0。

## Held-out 结果

| 卡规模 | Recall@10 (95% CI) | MRR | nDCG@10 | 直接上下文 | Expansion 依赖 | 中位 / P95 / 最大 ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 0.9889 (0.9604–0.9969) | 0.4181 | 0.5484 | 0.3722 | 0.6167 | 37.92 / 48.41 / 183.40 |
| 5,000 | 1.0000 (0.9791–1.0000) | 0.3791 | 0.5073 | 0.3667 | 0.6333 | 54.84 / 66.20 / 98.85 |
| 10,000 | 1.0000 (0.9791–1.0000) | 0.3708 | 0.4991 | 0.3611 | 0.6389 | 75.64 / 98.58 / 195.79 |

1k held-out 的两次漏召都来自中文 `repeated_event`：每例两项应召回项只命中一项，共漏 2 项；5k/10k 同一 held-out 集未复现。结果解封后没有继续调代码，因此保留为“重复事件在候选并列/预算下不稳定”的真实风险。其他泄漏指标仍全部为 0。

## 延迟口径与限制

每个规模先在本地 PostgreSQL bulk load 后执行 `ANALYZE` 和 6 次不计时索引预热，再统计 180 次正式查询；test 预热最大 35.04/57.52/188.51 ms，held-out 为 41.53/57.15/83.09 ms。load 与预热不混入稳态中位/P95/最大值，但单独保留在机器结果中。

这些数字只证明本机 PostgreSQL、无 pgvector、单进程、无生产网络的确定性路径。未验证 pgvector 质量、生产服务、常驻 worker、维护积压、真实长周期同一人物连续性、图片或最终回复自然度。数据分布也偏向西班牙语和 Wikimedia 社区讨论，12 个领域的长尾不均衡；直接上下文覆盖仅约 0.36–0.38，约 0.62–0.64 的必要项仍依赖 expansion handle，不能用 envelope Recall@10 代替模型首屏可见率。

机器结果：

- `benchmarks/results/public_human_recall_v2_frozen_baseline.json`
- `benchmarks/results/public_human_recall_v2_test.json`
- `benchmarks/results/public_human_recall_v2_heldout.json`

复跑：

```bash
export LIVEDAY0_DATABASE_URL="postgresql://postgres:<local-password>@127.0.0.1:<local-port>/liveday0"
uv run python benchmarks/collect_public_human_v2.py --audit-only
uv run python benchmarks/public_human_recall_v2_benchmark.py --audit-only
uv run python benchmarks/public_human_recall_v2_benchmark.py --split test
uv run python benchmarks/public_human_recall_v2_benchmark.py --split heldout
uv run pytest -q
```
