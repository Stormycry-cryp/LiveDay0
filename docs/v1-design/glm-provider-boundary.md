# 国内 GLM 完整提取合同边界

`GLMCNAdapter.extract(ExtractionRequest) -> ProposalBatch` 接受现有合同内的短文本，输出 0 或 1 个完整 event、fact 或 prospective 提议。它不是固定五字段试验，也不是已验证质量的生产提取器。`production_ready=False`；默认 CLI、保存确认、核心写入和数据库 schema 不变。本阶段只验证 synthetic loopback 与 fresh 隔离 PostgreSQL，不配置真实凭据或调用付费服务。

## 协议和合同

固定 `https://open.bigmodel.cn/api/paas/v4/chat/completions`、`glm-5.3-flash`、非流式、`thinking.type=enabled`、`reasoning_effort=low`、`max_tokens=4096`。使用 `response_format={"type":"json_object"}`，完整 wire schema 随 system 指令发送。没有假设服务端 strict schema 保证。

`extraction_wire.py` 从既有 OpenAI 边界移出纯 schema、严格 JSON 解码和来源绑定；保留 OpenAI 错误类型、原因码及 `_bind` 兼容入口。未知／缺失字段、重复 key、非法枚举／类型／body、越界 Unicode span、过多 proposal 均整体拒绝，不删字段或调用模型修补。GLM 最终还经过 `ProposalBatch.from_dict`，宿主继续调用 `validate_and_gate`。

模型只能提供语义字段与相对原文 span。来源 ID、版本、摘要、绝对区间、request fingerprint、producer、canonical/revision key、valid_at 由宿主绑定。模型不能提供 tenant、数据库 ID、凭据或人的授权。只解析最终 content；不保存、记录或使用 reasoning_content 作为证据。结构和来源校验不能证明语义忠实，相关质量仍待真实验证。

## 外发许可独立于保存许可

`secret` 在调用授权、预算及 HTTP 前拒绝。所有请求（包括 loopback fixture）都需要可信 `authorize_send` 回调返回与当前 `SendApproval` **完全相等**的对象；`True`、模型的 `explicit_save` 或另一份授权不能通过。检查值包含完整 payload bytes、来源 fingerprint、用途、准确型号、目的 URL、CNY 币种与该次费用预留。payload 摘要仅方便检查；完整 bytes 参与相等判断，默认 repr 不暴露正文。不要记录检查对象的 payload。

宿主必须从独立的人类外发授权构造回调，在用户审阅相应检查值后才返回它。无条件返回参数只适合合成测试，不能当作生产授权。保存确认发生在抽取之后，不能替代外发许可；外发获准后，模型把敏感来源标为 ordinary 仍不能绕过原保存 gate。模型的 save、forget、revise 不取得人的操作权限。

`purpose` 必须显式选择 `capture_new`、`interpret_saved` 或 `restore_saved`。新输入只能是调用方刚提交的有界文本；不得把从存储读出的正文冒充新输入以绕过来源检查。默认本地宿主没有远程授权集成，本补丁不添加 CLI 开关或自动解释。

## 已保存来源与删除竞态

`interpret_saved`／`restore_saved` 必须提供可信 `source_guard(request)`。回调在每次调用中用原核心 read API 重新读取同一 tenant、evidence、状态、版本、interpretation epoch，与冻结 prepared 对象及 `saved_request(prepared)` 比较；完全一致才返回 `True`。普通解释使用 `read_evidence_interpretation`；显式恢复使用已单独获得 read 授权的 `read_explicit_reinterpretation` 和同一 intent。adapter 自身不直接访问数据库。

推荐宿主次序：

1. 核心读取并冻结 prepared，构造 `saved_request(prepared)`。
2. 创建该用途的 adapter；将独立外发授权和上述 guard 注入。授权完成后、预留及发送前检查来源。
3. HTTP 返回后再次 guard；失败则丢弃输出，费用预留保留。通过后仍执行原 `validate_and_gate`。
4. 用原 `commit_evidence_interpretation` 或 `commit_explicit_reinterpretation` 提交同一 prepared；核心再检查版本／epoch／状态。显式恢复仍须精确输出的独立人类授权。

每次 guard 只开短事务并在返回前关闭。网络等待不能持有数据库锁。删除可能发生在发送前检查刚结束或请求在途时；已经发送给第三方的正文无法由本地删除撤回。返回后的 guard 与核心 commit 检查阻止陈旧结果落库，但不消除外发竞态，也不承诺第三方删除。不能省略最终核心 commit 检查：来源可能在 adapter 返回之后才变化。

## 成本及传输边界

`GLMCallBudget(max_cny=Decimal(...))` 要求显式人民币额度，范围 `(0, 1.25]`，每实例仅一次尝试。按完整模型容量 1,048,576 输入及 131,072 输出，官方全价输入 ¥0.8／百万、输出 ¥2.8／百万，预留 **¥1.2058624**；不计缓存优惠。完整请求字节上限 80,000，原合同文本上限 12,000 code points。没有经核实的 Flash tokenizer，计数 API 调用数为零。

这是本地保守估计，不是账户或第三方账单硬上限，尤其不能证明所有计费推理 token 均受请求的 4096 参数限制。收到 usage 后验证整数、范围及 total=sum；异常时丢弃结果。成功、失败、拒绝、超时及未知结果都消耗同一预留，不退款供再次尝试。预算是进程内状态，不跨实例／进程持久化；可信宿主负责整个获批试验的唯一预算，不能重建实例重试。此预算类本身不授予真实调用许可。

不使用 SDK、环境密钥、代理、重定向、自动重试、工具或其他模型。真实凭据只接受显式参数；loopback 禁止凭据。HTTP 响应最大 64,000 bytes；连接后总 I/O deadline 同时约束慢头和慢体。OS DNS 可能超出该时限，不能把它称为从开始到结束的硬墙钟上限。异常使用静态原因码，不含来源、凭据或原始响应。

价格和参数快照日期为 2026-10-09。官方依据：[国内价格](https://docs.bigmodel.cn/cn/guide/start/pricing.md)、[GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash.md)、[对话补全](https://docs.bigmodel.cn/api-reference/模型-api/对话补全.md)。实际账户可用性、JSON mode／reasoning 参数、返回型号、完整 schema 遵循率、中文生活抽取质量、usage 中思考计费口径及延迟仍未真实验证。

## 离线验收与独立小试

`tests/test_glm_boundary.py` 复用禁止外网的 loopback fixture，验证完整三类合同、空结果、授权每个绑定字段、敏感保存分离、HTTP 失败及预算不退款、原 gate、保存来源失效、网络期间删除不被锁住，以及返回后核心提交的再次检查。必要回归包括既有 OpenAI 边界与本地入口／删除确认测试。数据库测试只允许新的私有 Unix-socket 集群，先核验目录／marker／数据库及空表，再运行会清表的 fixtures，最后停止集群。

已单独批准的固定 933-byte 五字段 runner、请求、三份哈希固定纯合同模块及人工输入入口均未修改。那个小试的授权和结果不覆盖此完整 schema，也不能充当本 adapter 的真实质量验收。真实完整合同试验需要另列内容、次数与费用授权。
