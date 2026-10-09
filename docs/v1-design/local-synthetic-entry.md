# 合成本地入口

`liveday0 local` 在同一个 Python 单体中接通本机用户身份、封闭提案、隐私门禁和现有 MemoryService。它仅识别三句完整匹配的合成生活样例，不是通用自然语言提取器，也不是可对外开放的服务器鉴权层。

## 运行

先按 README 配置一个仅用于合成数据的 PostgreSQL 数据库，设置 `LIVEDAY0_DATABASE_URL` 并执行 `uv run liveday0 migrate up`。本入口不创建数据库、不安装模型、不调用付费 API。

```bash
uv run liveday0 local --demo
uv run liveday0 local
```

`--demo` 执行固定样例：保存、精确重试、查询、纠正、再解释、删除、明确恢复、再次删除和 secret 拒绝。批准回调是合成测试夹具，输出明确标记这一限制。该模式不接收任意文字或目标参数，重复运行会产生新的合成来源。

交互模式要求真实终端，不能从管道灌入命令。它接受以下 JSON 命令，`quit` 退出。示例中的 ID 从上一条回执或 recall 卡片取得；纠正版本从卡片的当前版本取得。

```json
{"action":"capture","text":"合成生活：周末搬家，等待朋友来帮忙。"}
{"action":"query","query":"合成搬家"}
{"action":"correct","card_id":"<回执中的 card_id>","expected_version":1,"text":"合成生活：朋友已来帮忙，搬家完成。"}
{"action":"delete","kind":"card","id":"<card_id>"}
{"action":"restore","evidence_id":"<evidence_id>"}
{"action":"delete","kind":"evidence","id":"<evidence_id>"}
```

另一句可保存样例是 `合成生活：我决定每周约朋友吃饭。`。未撤销来源可用 `{"action":"interpret","evidence_id":"<evidence_id>"}` 产生独立解释。精确重试 capture 时同时复用回执的 `message_id`、`occurred_at` 和原文；重试解释时复用 `intent_id`。不会自动把相似文字认作同一次输入。

## 信任边界

- 身份来自本进程真实 OS UID、经系统账户查询确认，并按主机名限定本地范围；请求不接受 tenant、user_id 或授权布尔值。不会读取 USER/HOME 环境变量充当身份。主机更名会改变身份命名空间；不同机器相同主机名和 UID 也不提供全局唯一保证，不能据此接共享服务器或跨设备账户。
- 此原型信任同一 OS 账户内的本地代码和终端操作者。拥有该账户进程权限的程序不在隔离边界内；`isatty` 或 `/dev/tty` 也不是对恶意同用户进程的防护。原有显式 tenant 开发 CLI 仍然存在，只能由受信任开发者使用。
- 纠正、删除、敏感保存分别展示完整具体范围，再从 `/dev/tty` 读取 `SAVE`。恢复分两步确认：先准许读取指定已撤销来源，再确认这一次完整冻结的来源、epoch、intent 和输出。核心 authorizer 只读查询已批准的精确请求；确认先在宿主完成，不由模型字段触发。批准仅保存在本进程内，不形成持久凭据或泛化恢复许可。
- `correct` 和 `delete` 的目标由人类命令明确给定。提案中的 revise、forget 等声明被拒绝，不能直接触发目标更改。核心仍检查版本、租户和来源撤销状态。删除先冻结目标版本及受影响来源、卡片、派生对象、关系、任务和快照集合，释放读锁后确认；写事务内重读比较通过才删除。确认期间的纠正或新增解释会零副作用拒绝，必须重新读取和确认。此原型还保守绑定 tenant revision，因此无关生活写入也可能要求重新确认；每类最多 200 个对象、完整确认最多 64,000 字节，超限不截断。

## 提取、来源与擦除

仅复用归档 LE 提交 `300cfae0209551ebf4484237ee37e2397cae1960` 的 `extraction_contract.py`（SHA-256 `d92c46328fd9acf8a31ad2d54fd9ef5468f767a645b83ad3bc94a83b6540bfb2`）。未合并旧 pipeline、provider 或 locator。

请求最多 12,000 Unicode 字符；命令和提案序列化输出各不超过 64,000 字节。解析为封闭结构后，独立检查来源身份、版本、范围、内容摘要、重复项和卡片结构，再检查直接用户陈述、主体、隐私、临时性及动作范围。已标记 secret 的来源在调用适配器之前拒绝；never_store 和已声明的引语/假设/第三方内容不能写入；敏感内容须额外确认。模型自报 explicit_save 没有授权效力。以上是对已声明标签和引用的确定性校验，不代表能识别任意自然语言中的秘密或错误归因。

当前本地入口每个 message/version 最多接受一个 span；多项有效提案整次拒绝。持久 source locator 只有 schema、opaque message UUID、version、获准 span 偏移和有效敏感级别。证据身份由 message/version 分配，改变 span 边界不能另获证据身份绕过原来源撤销。locator 不含原文、整段或 span 摘要、producer、语义摘要。只保存获准 span 原文，不保存未获准的周边文字及其摘要。

初次 capture 的 producer 元数据不持久化；它的写入幂等摘要由既有 core 保存，且只覆盖获准 evidence 与语义载荷。后续 interpret/restore 的 producer 标识放在可擦除的 interpretation intent provenance 中，来源或相关解释撤销时清除。来源仍获准保留时，原文及 locator 可保留供一次明确恢复；删除整个 evidence 时二者清空。显式恢复只创建本次新卡，原来源持续 revoked，不恢复自动解释权限。

## 验收范围

合成测试覆盖入口身份、保存回执、查询、纠正、删除、干净重启、再解释、精确恢复和隐私门禁。它不证明真实人类鉴权、任意语言提取质量、网络丢包或断电恢复。后台 candidate discovery、真实模型 provider、自然语言目标消歧和跨设备身份尚未接通；继续这些工作应有单独范围与验收。
