<div align="center">

# LiveDay0

#### 一个面向 AI 朋友的、有证据边界的生活记忆内核

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)](./pyproject.toml)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](./migrations/001_initial.up.sql)
[![Tests](https://img.shields.io/badge/Tests-19_passed-10B981?style=for-the-badge)](./docs/benchmarks/v1-local-baseline.md)

![Architecture](https://img.shields.io/badge/Architecture-Modular_Monolith-8B5CF6?style=flat-square)
![Isolation](https://img.shields.io/badge/Isolation-PostgreSQL_RLS-2563EB?style=flat-square)
![Recall](https://img.shields.io/badge/Recall-Bounded_Context-D97706?style=flat-square)

</div>

LiveDay0 是一个面向多租户服务器的生活记忆 v1 参考实现。

它不是聊天记录仓库、人物画像、通用知识库或提醒工具。它尝试解决的是另一件事：让 AI 在跨会话交流中保留一个人生活的连续性——普通日常、具体事件、当前处境、重要关系和仍朝向未来的事情——同时保留来源、不确定性、纠正和删除边界。

当前仓库跑通了一条最小纵向闭环：证据与普通生活痕迹进入规范事件、明确事实或前瞻事项，形成受控派生视图，再编译成完整、紧凑、可追溯的模型侧记忆上下文。它是记忆内核，不包含最终回复策略，也不是完整的 AI 朋友产品。

---

## 它解决什么

长期记忆系统容易走向两个极端：要么把所有消息都保存成可搜索档案，要么把一个人压缩成几条稳定标签。前者噪音越来越大，后者会丢失情境、变化和反例。

LiveDay0 的 v1 选择保留这些区别：

- 原始证据与模型解释分开；解释可以修正，来源不能被静默改写。
- 事件是生活经验的中心颗粒度；少量明确事实和前瞻事项可以直接成为规范锚点，不制造假事件。
- 当前状态、生活线索和关系视图是引用规范锚点的派生投影，不复制另一份“发生过什么”。
- 召回按规范身份去重，并以完整语义卡片进入上下文；放不下时整卡省略并保留引用，不截断半条结论。
- 明确纠正和删除优先于历史完整性；缺少历史优于返回已知错误的历史。
- 召回只为 Agent 提供理解材料，不决定语气、追问、是否主动提及记忆或最终回复。

完整设计见 [`implementation_design.md`](./docs/v1-design/implementation_design.md)，七个可观察验收场景见 [`acceptance_scenarios.md`](./docs/v1-design/acceptance_scenarios.md)。

---

## 架构与数据流

v1 是一个 Python 模块化单体，所有权威状态位于同一个 PostgreSQL 数据库。在线路径负责保存证据、应用硬失效并完成一次有界召回；事件追平和派生视图重综合由可重试的维护任务处理。

```mermaid
flowchart LR
    A["文字 / 图片引用 / 纠正 / 删除"] --> B["租户守卫 + 证据接入"]
    B --> C["证据与普通生活痕迹"]
    C --> D["规范核心：事件 / 明确事实 / 前瞻事项"]
    D --> E["当前状态 / 生活线索 / 关系视图"]
    C -. "标脏" .-> F["确定性维护任务"]
    F -. "局部重综合 + 原子替换" .-> D
    F -. "局部重综合 + 原子替换" .-> E
    D --> G["固定快照 + 有界候选"]
    E --> G
    G --> H["完整分层记忆上下文"]
```

主要模块：

| 模块 | 职责 |
|---|---|
| [`core.py`](./src/liveday0/core.py) | 证据接入、规范卡片、事件增量、纠正和删除传播 |
| [`maintenance.py`](./src/liveday0/maintenance.py) | 确定性标脏、任务合并、版本校验、重试和原子替换 |
| [`recall.py`](./src/liveday0/recall.py) | 固定快照、候选发现、类型化关系展开、规范去重和上下文预算 |
| [`migrations.py`](./src/liveday0/migrations.py) | 成对 SQL 迁移与状态查询 |
| [`cli.py`](./src/liveday0/cli.py) | 本地开发和验收入口 |

### PostgreSQL 是唯一事实源

- 证据、普通痕迹、规范卡片与版本、事件增量、关系、派生投影、维护任务、召回快照和删除标记都在 PostgreSQL 中。
- PostgreSQL 全文索引用于候选发现；关系邻接由带类型的关系表承载。
- 数据库存在 `vector` 扩展时，迁移会创建 8 维 HNSW 候选索引；扩展不存在时系统明确降级，不把全文候选伪装成向量结果。
- v1 不使用 SQLite、独立向量数据库或图数据库，也不把向量 payload 当作第二事实源。

### 租户隔离由数据库强制执行

迁移创建无登录权限的 `liveday0_app` 运行角色，并对所有租户数据表启用、强制执行 Row-Level Security。每个事务先切换运行角色，再通过 `SET LOCAL app.tenant_id` 固定服务端租户作用域；策略同时约束读取和写入。

CLI 中的 `tenant_id` 只是本地开发入口。接入真实服务器时，租户必须来自已认证的服务端上下文，不能直接信任客户端提交的任意 ID。

---

## 快速开始

前置条件：

- Python 3.13
- [`uv`](https://docs.astral.sh/uv/)
- Colima 和 Docker CLI，或一个可访问的 PostgreSQL 17 实例

仓库自带的 Compose 配置只用于本地开发。它使用公开 ECR 的 PostgreSQL 17 Alpine 镜像，不依赖 Docker Desktop。

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

也可以复制 [`.env.example`](./.env.example) 后自行设置本地环境变量；不要提交 `.env`。

### 写入证据并召回

先建立一个本地测试租户：

```bash
export TENANT_ID=11111111-1111-4111-8111-111111111111
uv run liveday0 tenant "$TENANT_ID"
```

写入一条明确事实。`observe` 的 payload 可以直接传 JSON，也可以使用 `@文件路径`：

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

v1 不内置模型 provider。调用方负责生成有界语义提案或派生视图替代主体，内核负责来源、租户、版本、生命周期和依赖校验，避免模型直接获得无边界数据库写权限。

### 纠正、删除和维护

```bash
uv run liveday0 correct "$TENANT_ID" "$CARD_ID" @correction.json
uv run liveday0 delete "$TENANT_ID" evidence "$EVIDENCE_ID"
uv run liveday0 run-jobs "$TENANT_ID" --limit 10
```

纠正采用追加证据和期望版本；删除会传播到来源、卡片版本、依赖视图、缓存快照和排队任务，只保留不含被删内容的防复活标记。

---

## 迁移与回退

```bash
uv run liveday0 migrate status
uv run liveday0 migrate down --steps 1
uv run liveday0 migrate up
```

`migrate down` 会删除 v1 模式，只应用于空库或明确的本地回退验收。停止本地数据库但保留卷：

```bash
docker compose down
```

只有确认要删除本项目本地测试数据库时才执行：

```bash
docker compose down -v
```

如果要实测向量候选，请从新的空测试卷开始，并在首次启动前选择包含 `vector` 扩展的 PostgreSQL 17 镜像：

```bash
export LIVEDAY0_POSTGRES_IMAGE=pgvector/pgvector:pg17
docker compose up -d --wait
uv run liveday0 migrate up
```

---

## 测试与基准

数据库启动并迁移后运行：

```bash
uv run pytest -q
uv run python benchmarks/benchmark_recall.py
```

2026-08-07 的已验收基线：

- PostgreSQL 17.10 空库 migration `up / down / up` 通过。
- `19 passed`、`0 skipped`；S1–S7 全绿。
- 额外覆盖 RLS 租户隔离、幂等、真实并发与重试、期望版本冲突、纠正与删除传播、后台失败、向量候选超时降级、固定快照和完整卡片预算。
- 默认参数（单卡 180 tokens、整包 1600 tokens、候选/关系/最终 48/24/12）在本机合成样本 30 次召回中的中位延迟为 28.62 ms，P95 为 32.19 ms，最大 34.91 ms。

环境、样本和限制见 [`v1-local-baseline.md`](./docs/benchmarks/v1-local-baseline.md)。这些数字是本地合成样本的起跑基线，不是生产延迟承诺。

---

## 当前边界

已经实现并验收：

- PostgreSQL 17 单一权威存储与强制 RLS 租户隔离
- 证据、普通痕迹、规范事件、明确事实和前瞻事项
- 事件快照、待吸收增量、完整有效事件卡和读前追平
- 当前状态、基础生活线索和关系投影
- 纠正、显式删除、身份候选和依赖感知局部失效
- 固定快照、有界关系展开、完整卡片预算和失败降级
- 可重试维护任务、版本冲突保护与成对 SQL 迁移

尚未完成或没有足够证据：

- pgvector 候选质量与性能；当前基准镜像没有安装该扩展
- 真实生活样本上的长期相关率、记忆自然度和关系连续性评估
- 长期图规模、维护积压和生产负载下的容量结论
- 常驻 worker、生产级调度、监控和告警
- HTTP 服务、认证授权、生产部署与多设备拓扑
- 成熟的慢速人物假设、人生章节和召回反馈学习

当前 `run-jobs` 是显式驱动的本地维护入口，不应被描述为已经验证的常驻后台 worker。

---

## 路线

1. 在带 pgvector 的隔离环境复测候选质量、延迟和降级路径。
2. 建立去标识化的真实生活样本集，评估相关历史、空召回、纠正和关系连续性。
3. 验证长期数据量下的索引、关系展开和维护积压，再决定是否需要物理拆分。
4. 增加生产服务边界、认证租户建立、worker 调度和可观测性。
5. 在纵向闭环稳定后，再扩展慢速人物/关系理解和可修正人生章节。

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
└── docs/
    ├── v1-design/
    └── benchmarks/
```

仓库不包含真实凭据、真实用户数据、生产配置或付费服务调用。
