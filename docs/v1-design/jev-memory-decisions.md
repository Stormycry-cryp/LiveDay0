# Jev 记忆判断接入

2026-10-19。本次新增通过 BeatAPI 调用 Jev 的可选接口和判断编排，数据库验证使用虚构生活材料、本机 HTTP 夹具和隔离 PostgreSQL。真实调用的范围与结果见本文验收记录，不据此宣称泛化语义质量通过。默认 CLI 与合成提取器保持原有行为。

## 分工与顺序

1. 宿主提供来源身份及生成式提取器，产生原合同要求的完整 `SemanticProposal`，代码检查结构、原文跨度、来源摘要和隐私条件。
2. Jev 判断是否属于生活记忆，以及主体、陈述方式、持续性和类别。主体或陈述方式不确定不会自动保存普通痕迹。
3. 对事件提案，代码查出有限的已有事件，并构造已有事件别名、新事件、普通痕迹、范围外、无法确定等选项；Jev 判断归属。相同主题不足以证明同一段经历。明确事实和前瞻事项进入各自类型。
4. Jev 检查完整提案是否得到原文支持，包括条件、时间和不确定性；不支持、矛盾或无法确定均返回待审阅。
5. 代码重新检查来源和候选快照。已有事件追加带版本约束的增量，新事件保存为新规范卡片，普通痕迹保存原文及观察边界。模型不生成数据库身份，不修改原始证据，不获得删除或授权能力。
6. 召回先由代码发现有限候选，再用 Jev 的独立相关性评分选择零个、一个或多个。当前明确状态和安全事实优先进入候选并保留编译资格；必要候选过多时明确报超限。后续来源、派生层、整卡省略和展开继续由原编译器完成。

问题及 Choice/Score 选项均在 [`judgments.py`](../../src/liveday0/judgments.py) 中构造。写入使用 `judge_observation` 将分类五题、事件归属和来源支持合成一个请求；事实/前瞻省略事件题，普通痕迹或范围外分类不消费无用的事件答案。以上是代码消费答案的逻辑顺序，各题独立判断，不能读取别题答案。数据库 ID 只在宿主侧映射为 `candidate_0` 等临时别名，模型不能借返回任意 ID 指定写入目标。

## 宿主接入

```python
from liveday0.core import MemoryService
from liveday0.jev import JevHTTP
from liveday0.judgments import MemoryJudgments


def configure_memory(tenant_id, *, api_key, authorize_send):
    transport = JevHTTP(api_key=api_key, authorize_send=authorize_send, timeout=15)
    return MemoryService(tenant_id, judgments=MemoryJudgments(transport))


# tenant_id 来自已认证的服务端上下文。
# api_key 是宿主提供的 BeatAPI key；authorize_send(payload) 检查此次完整载荷是否允许外发，
# 只有明确返回 True 才发送。模型结果不构成这项授权。
# service = configure_memory(tenant_id, api_key=api_key, authorize_send=host_policy)
# result = service.capture(request, extractor=source_bound_extractor)
# context = service.recall(query)
```

`request` 使用既有 `ExtractionRequest`；`source_id` 必须是宿主提供的 UUID，同一来源/版本重试保持同一身份。提取器实现 `extract(request)`，返回 `ProposalBatch` 或其合同字典。当前入口每个来源/版本只接受一个通过校验的原文跨度；多个通过项返回 `needs_review/one_source_span_required`。现有 `DeterministicLocalAdapter` 仅匹配三句虚构材料，不能当成通用提取器。

敏感材料使用公开的 `ObservationPipeline(service, extractor, judgments, authorize_sensitive=host_confirmation)`，再调用 `pipeline.capture(request)`。该宿主回调与 HTTP 的 `authorize_send` 分别控制敏感保存和外发。secret 来源在提取前拒绝；普通 `MemoryService.capture` 没有隐式敏感授权。

接入固定的 BeatAPI `jev-1.14-free` 和 `POST https://api.beatapi.io/v1/systemone`，显式传入 BeatAPI key，不读取环境凭据、不自动重试、不跟随重定向、不切换付费模型。协议依据：[BeatAPI Decisions](https://docs.beatapi.io/decisions)。响应严格要求 `id/model/answers/usage`，校验每题答案、候选选项、概率和 Score 标尺；格式正确仍不保证判断正确。

宿主可按 BeatAPI 官方 CLI 的配置使用系统凭据存储，或读取本进程的 `BEATAPI_API_KEY` 后显式传入；内核不管理或持久化密钥。免费模型有独立限流，未充值账号每分钟一次成功调用，首次充值后每分钟十次。宿主应在独立请求之间安排调用时机；当前客户端只返回固定错误码，不自动等待、重发或切换付费模型。连续 capture 和 recall 仍是两个请求，可能触发限流。

## 失败、重放与并发

`capture` 返回 `outcomes`。`stored` 表示已提交；`duplicate` 表示复用原收据且不重新提取或调用判断；`rejected`、`outside_scope`、`needs_review` 均不新增规范记忆。服务错误、低置信度、提取与分类不一致、不安全增量、候选过大和等待期间发生变化均保守停止。授权缺失、来源身份冲突和合同错误可能直接抛出异常，宿主应按错误类型处理，不能自动视为保存成功。

模型调用不持有数据库事务或租户锁。每个外部阶段之间重新检查来源及租户 revision，提交时检查完整冻结候选与待处理增量。删除发生在已经发送的请求中，无法撤回该请求；变化会阻止下一次模型发送和迟到结果写入。相同来源被删除后不会通过分段或改写重新分配身份。

语义召回在模型返回后再次检查 revision，发生更正或删除则抛出 `SnapshotInvalidated`，由宿主决定是否发起一次全新的查询。模型失败不静默改用词面历史。可选判断不会改变记忆的事实置信度、重要性或保存权限。

`min_confidence=0.8` 是可配置且未经校准的采用阈值，不能解释为 80% 正确率。低置信度的召回项被省略，低置信度的写入判断返回无法确定。

## 范围和验收

写入事件候选默认 48 项、最多 64 项，总冻结输入最多 64,000 bytes，待处理增量最多 128 行；旧候选使用已有来源全文索引，另保留近期候选。召回候选最多 48 项、完整候选最多 65,536 bytes。任一完整输入超限均停止，不截断语义正文。

数据库阶段继续使用原有 `RecallOptions.timeout_ms`；模型阶段单独使用 transport timeout。启用 Jev 后的总召回耗时包含多个数据库阶段及一次网络等待，不能再把默认 800ms 当作总请求墙钟上限。HTTP timeout 也不保证操作系统 DNS 阶段的严格墙钟限制。

新增测试覆盖分类/选项绑定、一次请求批量判断、已有/新事件、普通痕迹、范围外、无法确定、事实/前瞻、空/多项召回、必要约束、来源支持、幂等重放、跨租户、迟到更正/删除、阶段间删除、超预算、HTTP 错误和显式外发门。`test_jev_memory_integration.py` 串联真实 HTTP 客户端、响应解析、数据库追加和召回编译，两次 capture 加一次 recall 共三个 HTTP 请求，响应仍由本机夹具生成。

真实返回的概率和 Score 使用两位小数。解析器容许有限的量化误差，不归一化或改写原值：概率和误差最多 `min(0.05, 0.005 * 选项数) + 1e-6`，Score 与量化概率期望的误差最多 `0.005 * (1 + 索引之和) + 1e-6`；超出仍拒绝。定向测试保留了实际遇到的 `score=0.08`、量化期望 `0.07` 等情况。

### 本次真实小试

只使用新增虚构生活材料和本机隔离库，密钥通过官方 CLI 的系统凭据存储在进程中读取，未写入仓库或报告。未恢复历史试验或使用旧留出数据。

| 实际路径 | 结果 | 单次响应记录 |
|---|---|---|
| 搬家提案的七题批量判断 → capture | 协议解析通过，保留价值置信度 0.60 低于采用阈值，返回 `needs_review/classification_uncertain`，未保存 | 2,499 input tokens / 390 output tokens，约 1.17 秒 |
| 搬家完成情况查询 → 两个候选的召回评分 | 选中搬家候选，排除无关阅读候选；相关候选 Score 2.83、置信度 0.84 | 823 input tokens / 34 output tokens，约 1.16 秒 |

这些记录证明当前 BeatAPI 通路、响应兼容和有限分支的实际行为。真实模型自动保存后再召回的完整成功路径尚未通过；阈值偏保守、语义分类和来源支持的采用率需要后续独立评测，不能为了让这一个样例保存而降低阈值。

运行 pytest 前必须准备独立的空 UTF-8 PostgreSQL 测试库并显式设置 `LIVEDAY0_DATABASE_URL`；现有 `tests/conftest.py` 会逐例清空业务表。安装包测试前先运行 `uv build`。`test_synthetic_trial_entry.py` 是另一个历史试验的机器绑定测试，依赖其冻结脚本及原合同哈希，不能通过更新旧哈希来复用过去的模型试验授权。

2026-10-19 最终验证：`uv build` 成功；`python -m pytest -q --ignore=tests/test_synthetic_trial_entry.py` 在独立 UTF-8 PostgreSQL 17 测试库中 **607 passed**，含发行包迁移、原有更正/删除回归和新增判断测试。被排除的 14 项历史测试在未修改基线同样出现 `contract_source_changed`，不计入通过数。S1 晚餐场景改为按现有整卡预算规则通过展开引用检验完整 20 条痕迹及边界，没有放宽运行预算。

中文判断准确率、阈值校准、长历史候选发现和生产延迟均未验证。此次没有读取真实生活库、使用历史留出数据、恢复旧模型试验、修改数据库结构或部署服务。
