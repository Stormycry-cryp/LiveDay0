# 单一 OpenAI 传输边界：仅完成本地 HTTP 验证

本批次实现 `OpenAIResponsesAdapter`，供受信 Python 调用方显式注入现有
`LocalMemorySession(adapter=...)`。默认 CLI 仍使用原来的三句 deterministic fixture。
没有自动启用开关，没有新增 SDK、tokenizer、环境变量读取或凭据配置。
`production_ready=False`；当前本地入口的 `synthetic` 标识仍适用于合成试验。

## 已实现与未验证

- 只实现固定 `https://api.openai.com/v1/responses`、固定快照
  `gpt-4.1-mini-2025-04-14` 的请求/响应协议；其他 provider、兼容网关和模型没有接通。
  `ResponsesHTTP(fixture_port=...)` 只连 `127.0.0.1`，禁止带 credential。
- 请求使用严格 JSON schema、`store:false`、非流式输出、4096 输出 token 上限；
  没有工具、搜索、embedding、后台任务、跳转、代理环境读取、修复回合或自动重试。
- 模型只选相对原文的 Unicode span 和语义字段；来源 ID、版本、digest、时间、
  canonical key、producer 由适配器从受信请求绑定。它们不由模型声明，也不外发来源 ID。
  body 字段词表复用原封闭合同；只去掉已知可选字段的 null。未知字段即使为 null 也拒绝。
- 回包先验 HTTP、完成状态、模型、单一文本输出、usage、JSON 和封闭合同，再复用
  `ProposalValidator` 与现有隐私门禁。拒绝、工具输出、截断、非法 JSON/重复键、
  错误来源范围和任一不合法 proposal 都不能作为成功结果。空 proposals 是合法无提取。
- 已声明 secret 在适配器调用前拒绝，适配器自身也再次拒绝。远端发送另外要求
  `authorize_send(request) is True`；该受信回调应审核完整请求及既定模型/预算，
  不得只信模型的 explicit_save 或从普通命令 JSON 接收批准。它不替代敏感保存确认。
- 真实 TLS、API key、官方服务接受此 schema、真实 token 计数和提取质量尚未验证。
  本地 HTTP fixture 的 producer 明确标为 `openai-responses-fixture`，不能当作真实模型产物。
  span/digest 校验只证明引用范围和来源绑定，不证明语义推断正确。

## 预算与超时

`CallBudget` 必须由一次获准小试共享：最多12次，金额限制不超过 US$0.15。
每次发送前按 8192 输入 + 4096 输出的完整额度预留 US$0.0098304，
12次共 US$0.1179648。费用依据2026-10-09官方快照价格（输入$0.40/百万、
输出$1.60/百万），付费执行前必须重新核价。

预检回调 `token_counter(MODEL, full_payload)` 必须覆盖指令、schema、消息以及协议开销；
没有回调、失败、非整数或超过8192都在网络和预算预留前拒绝。
**当前没有提供已验证的真实计数器，因此真实执行仍缺此依赖。**
测试中的常数计数器只测试边界，不是可用于真实调用的 tokenizer。
如果 provider 返回超出预检限额或格式不合法的 usage，整轮预算停止，不能继续追加调用。

预算是进程内保守预留器，不是账户账单上限，也不能抵御错误的计数器、价格变化、
重启新建预算或其他进程的消费。未知费用、超时、拒绝、HTTP错误均占一次并保留全额；
不按少用 token 退还预留，不自动重试。usage数值仅供观察。达到边界就停止。

连接本身使用 socket timeout；连接后的整个 I/O 阶段另用总时限中断 socket，防止慢响应持续占用。
默认两阶段各15秒，均可收紧且不能超过30秒。系统 DNS 解析可能超过连接 timeout，
所以不宣称硬端到端总时限。连接完成前不发送 HTTP正文；HTTP错误正文不进入异常信息。
响应最多64000 bytes，输入序列化最多80000 bytes；这些字节限制不等于 token 计数。

## 验证与启用界限

`tests/test_openai_boundary.py` 用受限 loopback HTTP 服务验证协议、预算、超时、
关闭的schema、secret零请求，以及原入口的敏感确认、再解释与删除撤销。
测试钩子禁止外部 DNS/TCP；合成数据库独立且测试后停止。没有真实模型调用或付费。
没有扩充产品固定生活句表，没有重跑旧 heldout/v3c，没有改变服务器鉴权范围。

真实试验须另获模型/12次/US$0.15/合成数据外发授权，并配置项目凭据、核实计数器与价格。
本代码存在不代表已经获准执行这些动作。

官方协议依据：[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)；
模型/价格依据：[GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)。
