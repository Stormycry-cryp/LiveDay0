<div align="center">

# LiveDay0

#### 让 AI 记住一个人正在怎样生活，同时给每段记忆留下证据和改正的余地

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](./pyproject.toml)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](./migrations/001_initial.up.sql)
[![Tests](https://img.shields.io/badge/Tests-22_passed-10B981?style=for-the-badge)](./docs/benchmarks/v1-public-human-recall-baseline.md)

![Architecture](https://img.shields.io/badge/Architecture-Modular_Monolith-8B5CF6?style=flat-square)
![Isolation](https://img.shields.io/badge/Isolation-PostgreSQL_RLS-2563EB?style=flat-square)
![Recall](https://img.shields.io/badge/Recall-Bounded_Context-D97706?style=flat-square)

</div>

把聊天全部存下来并不难。难的是到了下一次交谈，AI 能否找回真正相关的那部分，又不会把一句旧话当成永远不变的人物标签。

LiveDay0 关注一个人的生活怎样跨过多次对话继续存在。普通日常、具体事件、当前处境、重要关系和还没结束的事情都可以进入记忆。每段记忆仍保留来源、不确定性和版本，后来出现纠正或删除时，旧结论必须跟着失效。

这份仓库是面向多租户服务器的 v1 参考实现。它已经跑通证据写入、规范事件与事实、局部维护、有界召回、纠正和删除。最终回复说什么、用什么语气、要不要主动提起一段往事，仍由上层 Agent 决定。

---

## 为什么要把这些东西分开

长期记忆系统常见的做法有两种。一种保存所有消息，久而久之得到一份越来越吵的搜索档案。另一种把人压缩成几条稳定标签，读取很方便，变化、情境和反例却会慢慢消失。

LiveDay0 在 v1 中保留了几组容易被混在一起的东西。

- 来源和解释分开。模型可以修改解释，不能静默改写原始证据。
- 事件承担生活经验的主体，明确事实和前瞻事项可以直接保留，不必硬编成事件。
- 当前状态、生活线索和关系视图引用同一批规范内容，不再复制一份平行历史。
- 召回按规范身份去重，完整语义卡片会整体进入上下文。空间不够时省略整张卡片，并留下可继续展开的引用。
- 纠正和删除优先于保住一份完整旧历史。系统宁可少返回一些，也不能继续提供已经知道是错的内容。
- 记忆只给 Agent 提供理解材料，不替 Agent 决定措辞、追问和交流方式。

完整设计见 [`implementation_design.md`](./docs/v1-design/implementation_design.md)。七个可观察的验收场景见 [`acceptance_scenarios.md`](./docs/v1-design/acceptance_scenarios.md)。

---

## 一段记忆怎样走过系统

v1 采用 Python 模块化单体，所有权威状态都放在同一个 PostgreSQL 数据库里。在线请求保存证据，立刻处理纠正和删除，再完成一次范围受限的召回。需要追平事件或重写派生视图时，系统把目标交给可重试的维护任务。

```mermaid
flowchart LR
    A["文字 / 图片引用 / 纠正 / 删除"] --> B["租户守卫 + 证据接入"]
    B --> C["证据与普通生活痕迹"]
    C --> D["规范内容\n事件 / 明确事实 / 前瞻事项"]
    D --> E["当前状态 / 生活线索 / 关系视图"]
    C -. "标记待维护" .-> F["确定性维护任务"]
    F -. "局部重写 + 原子替换" .-> D
    F -. "局部重写 + 原子替换" .-> E
    D --> G["固定快照 + 有界候选"]
    E --> G
    G --> H["完整分层记忆上下文"]
```

代码按这些职责分开。

| 模块 | 工作 |
|---|---|
| [`core.py`](./src/liveday0/core.py) | 接收证据，维护规范卡片和事件增量，传播纠正与删除 |
| [`maintenance.py`](./src/liveday0/maintenance.py) | 标记待维护目标，合并任务，校验版本，处理重试和原子替换 |
| [`recall.py`](./src/liveday0/recall.py) | 固定快照，发现候选，展开类型化关系，按规范身份去重并控制上下文大小 |
| [`migrations.py`](./src/liveday0/migrations.py) | 执行成对 SQL 迁移并查询状态 |
| [`cli.py`](./src/liveday0/cli.py) | 提供本地开发和验收入口 |

### PostgreSQL 保存全部权威状态

证据、普通痕迹、规范卡片与版本、事件增量、关系、派生视图、维护任务、召回快照和删除标记都在 PostgreSQL 中。

全文索引用于寻找候选，带类型的关系表负责邻接查询。数据库装有 `vector` 扩展时，迁移会创建 8 维 HNSW 候选索引。没有扩展时，系统会明确降级，也不会把全文搜索结果假装成向量结果。

v1 没有引入 SQLite、独立向量数据库或图数据库，向量 payload 也不会成为第二份事实来源。

### 租户隔离落在数据库里

迁移会创建没有登录权限的 `liveday0_app` 运行角色，并为所有租户数据表启用和强制执行 Row-Level Security。每个事务先切换运行角色，再用 `SET LOCAL app.tenant_id` 固定服务端租户范围，读取和写入都受同一组策略限制。

CLI 接受 `tenant_id` 只是为了本地开发。接入真实服务器后，租户身份必须来自已经认证的服务端上下文，不能相信客户端随手提交的任意 ID。

---

## 在本机跑起来

需要准备这些环境。

- Python 3.13
- [`uv`](https://docs.astral.sh/uv/)
- Colima 和 Docker CLI，或一个可以连接的 PostgreSQL 17 实例

仓库里的 Compose 配置只供本地开发。它使用公开 ECR 的 PostgreSQL 17 Alpine 镜像，不依赖 Docker Desktop。

```bash
git clone https://github.com/Stormycry-cryp/LiveDay0.git
cd LiveDay0

colima start
export LIVEDAY0_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export LIVEDAY0_DATABASE_URL="postgresql://postgres:${LIVEDAY0_POSTGRES_PASSWORD}@127.0.0.1:55432/liveday0"

docker compose up -d --wait
uv sync --python 3.13
uv run liveday0 migrate up
uv run liveday0 migrate status
```

也可以复制 [`.env.example`](./.env.example)，再设置本地环境变量。不要提交 `.env`。

### 写入证据并召回

先建立一个本地测试租户。

```bash
export TENANT_ID=11111111-1111-4111-8111-111111111111
uv run liveday0 tenant "$TENANT_ID"
```

下面的例子写入一条明确事实，再用饮食相关的问题把它找回来。`observe` 可以直接接收 JSON，也可以接收 `@文件路径`。

```bash
uv run liveday0 observe "$TENANT_ID" '{
  "evidence": {
    "modality": "text",
    "source_kind": "user_message",
    "content": "我对花生严重过敏，哪怕一点也不行。",
    "idempotency_key": "example-allergy"
  },
  "semantics": [{
    "card_type": "fact",
    "canonical_key": "fact:peanut-allergy",
    "body": {
      "proposition": "用户对花生严重过敏，任何量都不接受",
      "scope": "用户本人；花生暴露"
    }
  }]
}'

uv run liveday0 recall "$TENANT_ID" "花生 饮食"
```

v1 没有内置模型 provider。调用方负责提出范围明确的语义内容或派生视图替代主体，内核负责检查来源、租户、版本、生命周期和依赖。模型因此不会拿到没有边界的数据库写权限。

### 纠正、删除和维护

```bash
uv run liveday0 correct "$TENANT_ID" "$CARD_ID" @correction.json
uv run liveday0 delete "$TENANT_ID" evidence "$EVIDENCE_ID"
uv run liveday0 run-jobs "$TENANT_ID" --limit 10
```

纠正会追加证据并检查期望版本。删除会传播到来源、卡片版本、依赖视图、缓存快照和排队任务，最后只留下不含原内容的防复活标记。

---

## 迁移和回退

```bash
uv run liveday0 migrate status
uv run liveday0 migrate down --steps 1
uv run liveday0 migrate up
```

`migrate down` 会删除 v1 模式，只能用于空库或已经明确授权的本地回退验收。

停止数据库并保留本地卷时运行下面的命令。

```bash
docker compose down
```

只有确认要删除本项目的本地测试数据库时，才执行带 `-v` 的版本。

```bash
docker compose down -v
```

如果要实测向量候选，请从新的空测试卷开始，并在第一次启动前选择带 `vector` 扩展的 PostgreSQL 17 镜像。

```bash
export LIVEDAY0_POSTGRES_IMAGE=pgvector/pgvector:pg17
docker compose up -d --wait
uv run liveday0 migrate up
```

---

## 已验证到什么程度

数据库启动并完成迁移后，可以运行测试和本地基准。

```bash
uv run pytest -q
uv run python benchmarks/benchmark_recall.py
uv run python benchmarks/public_human_recall_benchmark.py --check-sources
```

2026 年 8 月 7 日的验收使用全新的 PostgreSQL 17.10 空库。迁移 `up / down / up` 通过，测试结果为 `19 passed` 和 `0 skipped`，验收场景 S1 到 S7 全部通过。

额外测试覆盖了 RLS 租户隔离、幂等、真实并发与重试、期望版本冲突、纠正和删除传播、后台失败、向量候选超时降级、固定快照和完整卡片预算。

默认参数采用单卡 180 tokens、整包 1600 tokens，候选、关系和最终数量分别为 48、24 和 12。本机合成样本完成 30 次召回，中位延迟为 28.62 ms，P95 为 32.19 ms，最大值为 34.91 ms。

环境、样本和限制记录在 [`v1-local-baseline.md`](./docs/benchmarks/v1-local-baseline.md)。这些数字只说明当前本地合成样本的起跑位置，不能当作生产延迟承诺。

2026 年 8 月 10 日新增公开真人语料基线：38 条去标识最小证据来自 13 个 Stack Exchange 公开线程，覆盖稳定上下文、变化中的现在、未完成连续性、纠正、删除、时间、噪声和跨人隔离。最终 8/8 类通过，必要项召回包 15/15，噪声、旧解释、删除复活和跨人泄漏均为 0；其中 11 项直接入上下文，4 项保留为可展开引用。来源、许可、隐私边界、原始失败和修复证据见 [`v1-public-human-recall-baseline.md`](./docs/benchmarks/v1-public-human-recall-baseline.md)。

---

## 现在已经有的部分

- PostgreSQL 17 单一权威存储和强制 RLS 租户隔离
- 证据、普通痕迹、规范事件、明确事实和前瞻事项
- 事件快照、待吸收增量、完整有效事件卡和读前追平
- 当前状态、基础生活线索和关系视图
- 纠正、明确删除、身份候选和依赖感知局部失效
- 固定快照、有界关系展开、完整卡片预算和失败降级
- 可重试维护任务、版本冲突保护和成对 SQL 迁移
- 公开真人最小证据数据集、source-safe split、来源/许可/PII 审计和八类确定性召回 benchmark

## 还缺证据的部分

- pgvector 候选质量和性能，当前基准镜像没有安装该扩展
- 更大规模、多语言和长期关系样本上的相关率、记忆自然度与直接上下文覆盖率
- 长期图规模、维护积压和生产负载下的容量结论
- 常驻 worker、生产级调度、监控和告警
- HTTP 服务、认证授权、生产部署和多设备拓扑
- 成熟的慢速人物假设、人生章节和召回反馈学习

当前 `run-jobs` 是需要显式执行的本地维护入口。它还没有经过常驻后台 worker 验证。

---

## 接下来可以验证什么

1. 在带 pgvector 的隔离环境复测候选质量、延迟和降级过程。
2. 在同样的许可和去标识边界下扩充多语言、长时间跨度的公开真人样本，单独评估直接上下文覆盖率。
3. 验证长期数据量下的索引、关系展开和维护积压，再决定是否需要物理拆分。
4. 增加生产服务边界、认证租户建立、worker 调度和可观测性。
5. 等前面的纵向流程稳定后，再扩展慢速人物与关系理解，以及可以修正的人生章节。

这些仍是后续验证方向，当前仓库没有把它们写成已经完成的功能。

---

## 仓库结构

```text
LiveDay0/
├── README.md
├── compose.yaml
├── pyproject.toml
├── uv.lock
├── migrations/
│   ├── 001_initial.up.sql
│   └── 001_initial.down.sql
├── src/liveday0/
│   ├── core.py
│   ├── maintenance.py
│   ├── recall.py
│   ├── migrations.py
│   └── cli.py
├── tests/
├── benchmarks/
│   ├── public_human_recall/
│   ├── public_human_recall_benchmark.py
│   └── results/
└── docs/
    ├── v1-design/
    └── benchmarks/
```

仓库不包含真实凭据、真实用户数据、生产配置或付费服务调用。
