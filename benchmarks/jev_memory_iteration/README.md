# Jev 有限自迭代评测

本目录包含新写的虚构中文材料：8 条 dev、两版各 6 条 challenge。工作经历属于生活经历；技术资料本体不属于个人生活经历。它们与旧 heldout、历史真实材料没有数据依赖。`freeze_manifest.json` 绑定各份文件的完整字节 hash。每版 challenge 只为当时的代码做一次独立验证；出结果后若用于后续问题诊断，就属于已见材料，不能再证明后续代码的独立质量。

完整 request/proposal 包含合法 UUID、原文摘要、跨度摘要、subject、speech、scope 与完整 semantic body。它们是固定夹具，不代表模型生成式提取质量。来源完整性使用生产 `ProposalValidator` 验证。

## 执行

从仓库根目录执行，使用现有 `.venv`。每个命令的 `--output` 必须指定尚不存在的新目录。

```sh
PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split dev --variant baseline --prepare-only \
  --output benchmarks/jev_memory_iteration/reports/baseline-prepared
```

prepare-only 不联网，不读取任何凭据，也不使用 collector 合法假答案计分。它输出公开 `judge_observation` 实际生成的待发 state/questions 和代码来源 hash。旧版 8 条 dev 共 55 题；逐候选比较版为 6 个基础问题加各事件候选问题，具体数量记录在各次报告中，单次请求最多 56 题。

真实调用要求当前进程已有 `BEATAPI_API_KEY`，并显式添加发送开关。脚本不加载 dotenv、配置文件、钥匙串或其他凭据。不要把密钥写入命令、报告或提交。

```sh
PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split dev --variant baseline --dev-round 0 --allow-synthetic-send \
  --output benchmarks/jev_memory_iteration/reports/baseline-live

PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split dev --variant current --dev-round 1 --allow-synthetic-send \
  --output benchmarks/jev_memory_iteration/reports/current-dev-1
```

最多允许两轮 dev 修改，第二轮使用 `--dev-round 2`。每轮只发一个同步请求，不自动循环。完成 dev 后只运行一次 challenge：

```sh
PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split challenge --variant current --allow-synthetic-send \
  --output benchmarks/jev_memory_iteration/reports/current-challenge
```

本地 `live_run_ledger.json` 记录已保留的尝试，强制至少 60 秒间隔、相同 dev variant/round 一次、challenge 一次。失败也占用尝试，防止隐式重试。它只能约束本脚本，同账号其他工具的调用间隔仍须由调用者协调。不要删除或重置账本以追加调用。模型固定 `jev-1.14-free`，无自动重试或付费回退。

## 离线重算

```sh
PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split dev --variant baseline \
  --replay benchmarks/jev_memory_iteration/reports/baseline-live \
  --output benchmarks/jev_memory_iteration/reports/baseline-replay

PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split dev --variant baseline --policy-override argmax \
  --replay benchmarks/jev_memory_iteration/reports/baseline-live \
  --output benchmarks/jev_memory_iteration/reports/baseline-argmax-ablation
```

replay 必须匹配同 split/variant 的完整原请求；prompt、case、状态或包装变化都会拒绝复用。argmax 消融仅允许离线 baseline，保留旧输入和题目，仅把已存响应中的选择策略改为概率最大的选项。报告标明复用原响应、零新模型调用，原 usage 为参考元数据，不能作为本次新 token 消耗。

baseline 从固定 git 对象 `87183ec31ef467afea45f24a463c14d5c61da00f:src/liveday0/judgments.py` 加载，保持旧输入和 0.8 置信度阈值。current 从当前源码加载，使用当前实际策略；当前迭代为新输入与 argmax。两者直接比较是组合变化，不能把差异只归因到 prompt。

## 解析与指标

每个 case 先通过公开 `MemoryJudgments.judge_observation` 收集本地 state/questions。collector 只回一个合法协议夹具，供生产代码走完问题构造，不是语义答案。请求把状态放入 `cases.{id}`；每题明确只查看对应局部对象，`source_span_text/source_context/observed_at/claim/candidates/request/proposal` 均相对该局部对象。ID 不包含标签，预期答案不发送给模型。

返回后先完整校验全局协议，再还原 case-local 答案，通过同一生产 `MemoryJudgments` 解析。每例保留原始 choices、probabilities、confidence 和最终判断。生产 `ObservationPipeline.capture` 的分类、事件归属、来源支持检查顺序被只读预测函数复现；它不保存、不读取数据库、不证明保存授权。

报告分开列出正向事件、工作事件、trace、outside、禁止误写、已有事件归属、新事件归属的正确数、分母、准确率与 needs_review 率。路由正确同时要求已有目标 ID 正确；禁止误写组还要求来源支持判断拒绝被篡改的提案。overall 同时要求路由、目标和声明的来源支持断言通过。组可重叠，不相加作为总分。每组没有样例时为 null。

原 model/id/usage 直接保存于 response.json；report 只在整批层计一次真实 token 总量，不给各 case 估分摊。网络或协议失败报告 `failed_no_semantic_result`，不生成语义指标，也不计成模型答错。

## 验证边界

这是有限开发/新挑战样例。多 case batch 与生产单 case 上下文存在差异；通过本评测仍需独立单 case 的实际 capture 验证。它不检验候选发现、生成式提取、数据库落库、长期召回或总体真实数据质量。

只运行以下无数据库单元测试。协议夹具只检验封装、还原、拒绝、安全开关和指标口径，不代表 Jev 的真实质量。

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover \
  -s tests -p test_jev_iteration_harness.py -v
```

不要用 pytest 跑这个文件：仓库 conftest 的自动夹具会初始化并清空数据库。

## 第二轮与 challenge_v2

第一轮 dev 原始结果为 7/8，c01 有来源支持标签过强的勘误，详见 `ERRATA.md`。第一版 challenge 已消耗，原始结果为 4/6。第二轮分析了其事件归属和日常粒度失败，因此它已转为已见诊断材料，不能证明第二轮的独立质量；其记录、冻结标签和 hash 保留，禁止重跑或改写。

当前真实调用阶段已限制为第二轮，只允许 `--variant current --dev-round 2`，split 为 dev 或 challenge_v2。前文 baseline/round 1 真实调用命令属于历史执行示例；离线 replay 与 prepare-only 不受该发送限制影响。

`challenge_v2.json` 是全新独立材料，在第二轮评测前冻结。它单独允许一次真实调用；旧版与新版各有独立的一次额度。版本更新不能清除旧版失败，也不能作为第一版的替代分数。

```sh
PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split dev --variant current --dev-round 2 --allow-synthetic-send \
  --output benchmarks/jev_memory_iteration/reports/current-dev-2

PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split challenge_v2 --variant current --dev-round 2 --prepare-only \
  --output benchmarks/jev_memory_iteration/reports/challenge-v2-prepared

PYTHONPATH=src .venv/bin/python benchmarks/jev_memory_iteration/run.py \
  --split challenge_v2 --variant current --dev-round 2 --allow-synthetic-send \
  --output benchmarks/jev_memory_iteration/reports/current-challenge-v2
```

问题收集不依赖固定题名或固定每例七题。当前模型接口按候选分别生成 `same_event/different_event/uncertain` Choice；每例基本六题加候选数，无候选时由生产代码返回 new_event。全局封装、局部还原以及生产解析均自动采用当前公开方法实际生成的问题。baseline 仍固定旧 git 对象及原协议。
