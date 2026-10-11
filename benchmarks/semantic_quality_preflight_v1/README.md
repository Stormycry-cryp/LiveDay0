# 自然中文语义质量：独立评测准备 v1

后续收口：评测专用的[人工捕获入口与全局账本](README-CAPTURE.md)已补齐，只经本地假服务验证。原manifest/样本/prompt/评分工具仍保持冻结；下文“捕获入口待交付”是本准备版本的历史边界，当前应按新入口说明与其独立capture manifest执行门判断。真实请求仍需新明确授权，当前未执行，旧一次授权仍不可复用。

本目录是**准备好的评测包，不是质量通过结果**。12条新编虚构生活材料，6条公开审阅集、6条由另一作者独立拟定的留出集；六维度各一条/每split：普通片段、时间变化、人物角色、条件承诺、后来反证、不确定性。材料没有真实个人数据，也不把旧公园/订票/复诊演示换名重跑。留出语义未向实现者展开，由非作者独立预检；不将oracle或baseline发给模型。

`contract.json`固定期望口径、禁止推断、四个评测轴、停止条件和未来执行边界；两个jsonl保留每例可接受变体、必需/禁止语义点、来源时间与后续探针。允许同义表达，不用字符串完全相等或现有实现输出反向定义正确。oracle是源材料约束，不是可供提取器照抄的响应。没有人工编写golden模型响应。

## 冻结与独立性

`manifest.json`绑定全部样本、合同、离线工具，以及实际复用的纯合同/adapter/gate源码。代码、prompt、模型配置、数据、oracle或阈值变化即停止，不在当前结果后追改；新版本须重新前置审阅。留出若用于改prompt/实现就退役，不再称独立留出。冻结前独立预检修订了空输出适用范围、次要上下文可省略边界及提案/实际host操作混淆；那是执行前oracle修订，尚无任何真实响应。

普通片段/猜测允许空候选时，只有明确标为returned_content的必需项可标not_applicable，禁止推断仍适用。空提案只说明这一候选处理符合约束，**不证明普通trace已经保存、召回有用或Agent会拒答**。现有wire最多一个候选、没有trace类型，是接口覆盖限制，不能据此修改生活意义标准。整段复杂多事件、图像、长期人物/关系等均不在这一小样本宣称范围。

## 可直接运行的无秘密命令

仓库根目录，标准库Python即可；`-I -S`隔离site初始化，工具不加载产品包初始化/数据库，不读环境变量、dotenv、密钥或旧试验状态。网络入口在工具中关闭，输出文件以exclusive方式创建，不覆盖失败证据。

```sh
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py check
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py export --out /tmp/quality-v1-development-requests.jsonl
```

导出的每行仅含case ID、payload SHA、source fingerprint与现有GLM请求格式。模型只看到既有instructions/schema、中文content、occurred_at、来源类型/权限/敏感度；看不到split、维度、oracle、探针或baseline。导出是0次发送，不能当执行授权。

未来经新授权的操作者提供captures.jsonl，每行严格为：

```json
{"case_id":"d01","request_sha256":"<export中的64位SHA>","response":{"model":"<已冻结模型>","choices":[],"usage":{}}}
```

上例仅示意字段，空choices不是合法响应。实际response沿既有GLM外层JSON/单choice/stop/usage和内层闭合ProposalBatch合同；不得放headers、密钥或其他真实个人材料。收集顺序固定，每case最多一次；提前停止时只交已尝试的连续前缀，剩余项记not_run。未知/失败也保留原尝试标记，不能换一份好响应充当首次。

```sh
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py ingest --captures /tmp/quality-v1-development-captures.jsonl --out /tmp/quality-v1-development-structure.json
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py template --report /tmp/quality-v1-development-structure.json --out /tmp/quality-v1-development-review.jsonl
# 人工核对来源、响应与每项语义，填写review；不是让模型自评分。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py score --report /tmp/quality-v1-development-structure.json --review /tmp/quality-v1-development-review.jsonl --out /tmp/quality-v1-development-score.json
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py export --split heldout --development-score /tmp/quality-v1-development-score.json --out /tmp/quality-v1-heldout-requests.jsonl
```

留出使用相同ingest/template/score命令加`--split heldout`与新文件名。公开集6项人工完整通过才能发布留出请求；这只是可信操作者的科研流程检查，不是不可伪造授权票据。评分仅汇总人的证据判断，不自动验证语义蕴含。结构失败、未人工判定或来源真实性未核实不能通过；未判定/未执行四轴保持not_run，不生成真实质量成绩。

## 四个轴各自的证据

| 轴 | 实际评测对象 | 不可替代的证据 |
|---|---|---|
| 提取 | 模型从原文生成的候选与来源/gate | 真实新捕获的完整响应；人工逐项required/forbidden及证据说明。结构PASS、正确脚本响应和gate接受不等于语义PASS |
| 召回 | 审定的实际候选在新隔离合成库中的compiled context及pinned展开 | 各例自然查询、相关/空历史、来源/日期/限定的实际context artifact；不能把oracle卡代替错误模型候选再给召回PASS |
| 更正 | 明确host目标/expected_version的操作 | 实际目标更正、合法历史、来源、无关锚点不变及旧snapshot拒绝；revision提案被正确路由只计提取，不计host操作完成 |
| 拒答 | 无证据问题的理解与Agent输出，以及边界不保存 | unknown/no-save/空历史分别记录；完整Agent答复的独立artifact才能计对话拒答，记忆层不因此获得回复政策 |

后三轴的探针已固定，但本包没有数据库或Agent执行分支；缺少实际artifact就not_run。review填写pass/fail须引用独立artifact SHA；工具只检查引用格式，人负责核实artifact与冻结探针的关联。外发计划只包含提取请求，不含付费judge或额外Agent回复调用；若未来要增加Agent模型回复，须另设请求数量/费用并获新授权，不能把它藏在本次12次中。

## 请求、费用与停止估算

最多**12次外发提取尝试**（先6公开，逐条人工审阅，再至多6留出），单并发、零自动重试。不存在“失败免费不计次数”；超时、拒绝、未知结果同样消费本次机会，剩余not_run。无付费模型judge；导出/导入/人工评分和纯PG/core操作不新增模型请求。

每请求沿当前adapter `max_tokens=4096`；12个请求的max_tokens合计49,152，这不是实际账单或可保证的模型完整输出。当前代码有80,000-byte请求和64,000-byte响应界及connected I/O timeout，DNS/总墙钟没有保证；12×30秒最多约6分钟的connected等待只用于安排，不是严格总时限。工具check报告冻结payload实际字节，但不把UTF8字节当token。

现有GLM代码使用2026-10-09价目快照、最大输入/输出能力预约，算术为每次¥1.2058624、12次¥14.4703488。**它不是当前价格、批准预算或账单保证**；不能因为新建12个进程就绕过累计额度。真实运行前须重新核实价格/模型并由用户指定累计费用封顶，经审阅的新捕获入口应先持久预约每次和全局预算、禁止重试/并发/续跑旧标记。离线工具不实现该执行期账本，也不冒充执行期费用控制。

任一条件立即停止：新授权缺失/撤回、材料或prompt hash变化、来源/结构/usage/HTTP/传输/超时/未知错误、首个必需语义缺失或禁止推断、人工否决、达到尝试/累计预算上限或费用不可核实、官方周剩余≤50%或不可读。停止后不自动恢复。公开集失败不得发布留出，不按结果放宽标准；小样本全部通过也只是一道有限质量前检，不等于泛化或生产可用。

## 下一次所需的具体授权

原一次真实试验已消耗且只有synthetic_extraction_mismatch，旧入口、标记和缺字段结果不能复用或改写。本包不包含可联网的执行命令；上面的命令是可运行的离线评测命令。未来真正捕获需要另行审阅的新执行入口，不能假装旧`--execute`已可用。

下一步需要用户明确授权：**对这份manifest限定的新评测，允许至多12次单次尝试/零重试的虚构材料外发；确认模型与endpoint、累计费用上限、允许保存虚构请求/响应供人工审阅；指定由用户管理认证的独立执行环境，本Agent仍不读取/注入/迁移密钥；若同时验收后三轴，另明确允许新空标记合成PG及Agent答复的范围。** 授权后先审核新捕获入口与账本，再运行，不在准备阶段自行开放。当前有价值的准备已收口；不能靠更多合成成功测试替代这一步。
