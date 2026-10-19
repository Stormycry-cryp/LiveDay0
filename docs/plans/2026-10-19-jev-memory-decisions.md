# Jev 记忆判断接入

目标：将分类、事件归属、来源支持和召回相关性作为显式模型判断。代码构造问题、选项和有限候选；生成式提取器继续提供完整的来源绑定候选。Jev 不直接写库，不生成事件身份，不承担授权、删除、版本和预算检查。

## 实施范围

1. `judgments.py` 与 `jev.py`：窄判断接口、代码生成的 Choice/Score 问题、严格响应解析、显式凭据及发送授权的 HTTP transport。按用户要求使用 BeatAPI `jev-1.14-free`，不自动读取环境或重试、不切换付费模型。低置信度保留为无法确定；阈值为可配置策略，不声称已校准。
2. `observation_pipeline.py` 与核心事件写入：已有提取合同先做结构/隐私检查，然后分类、事件归属、原文支持；已有事件追加有版本约束的增量，新事件正常保存，普通痕迹单独保存，范围外/无法确定不执行规范写入。明确事实和前瞻不强行事件化。模型调用在数据库事务之外；结果提交前重核来源与候选快照。
3. `recall.py`：在有限候选内可选地使用语义评分，支持多选和空历史。必要边界与显式当前约束继续由代码保护；来源和层次编译仍复用原有逻辑。调用模型不持有数据库事务，选择结果使用前重核读取版本，未知候选拒绝。
4. 离线/loopback 测试和隔离 PostgreSQL 集成回归，补充接入示例与边界。使用新隔离数据库，现有 pytest 自动清理器绝不指向用户数据库。

## 接口约定

`MemoryJudgments(evaluator, min_confidence=0.8)`；evaluator 提供 `evaluate(state, questions)`，返回 BeatAPI `{id, model, answers, usage}`。

- `classify_observation(request, proposal)` 返回 `Classification(disposition, speech_mode, subject, persistence, category)`。
- `judge_observation(request, proposal, candidates)` 返回 `ObservationJudgment(classification, assignment, source_support)`；capture 使用一次独立问题批次，避免同一份输入连续占用三次免费调用。
- `assign_event(request, proposal, candidates)` 返回 `EventAssignment(kind, candidate_id=None, candidate_version=None)`；候选使用含 `id,version,type,body,valid_at,lifecycle,epistemic_state` 的字典。kind 为 `existing_event/new_event/trace_only/outside_scope/uncertain`。
- `check_source_support(request, proposal)` 返回 `supported/contradicted/unsupported/uncertain`。
- `rank_recall_candidates(query, candidates)` 返回保留的候选 ID 字符串列表，按相关性排序。候选同上；不修改内容、状态或置信度。

## 验收

覆盖新事件、同一事件进展、相似主题不同经历、普通痕迹、范围外、无法确定、事实和前瞻；未知/重复候选、无效概率、低置信度、服务错误、来源删除、跨租户和迟到版本均不得导致错误写入；模拟 HTTP 覆盖协议和敏感材料发送门。复用现有纠正/删除和召回回归。

用户已指定 BeatAPI；复用官方 CLI 的系统凭据存储，仅执行新的虚构材料免费小试，不保存或显示密钥。模拟成功或小试成功不能代表泛化语义质量。不得读取旧生活数据、旧 heldout 或恢复历史模型评测。

## GitHub

功能分支 `codex/jev-memory-decisions` 基于已有 `codex/trustworthy-memory-phase1`（a93b363）。PR 以该分支为基线，避免混入前面未合入 main 的 32 个提交。不合并、不部署。
