# 完整召回返回包与执行预算

本项接续既定产品计划第三优先，修复两个实际入口缺口：原版本100字符估算预算返回565字节/142估算单位却报告0；原20ms配置遇真实pg_sleep后132ms仍返回可用快照。反例使用`bc5bce4`已提交源码的临时副本和新隔离合成库记录，未改写旧证据。

## 默认上限与入口

| 项目 | 默认 | 配置与含义 |
|---|---:|---|
| 完整context | 16384 bytes | `RecallOptions.context_byte_limit`；UTF-8紧凑JSON加一个LF，包括全部metadata、layers、handles、budget报告本身 |
| 整包字符估算 | 1600 | `context_token_limit`；`ceil((紧凑JSON字符数+LF)/4)`，公开估算算法，不是实际模型tokenizer计数 |
| 单卡字符估算 | 180 | 原`card_token_limit`；只选择完整卡或带明确representation的已有语义短形 |
| 单次完整展开 | 65536 bytes | `expansion_byte_limit`；完整原卡加LF，超限不返回半卡/半出处 |
| 总执行工作预算 | 800ms | `timeout_ms`；从`MemoryService.recall/expand_snapshot`入口开始的同一monotonic截止点 |
| 规范候选 | 48 | 复用现有共享`candidate_limit`；本次当前来源ID输入也不超过此值，query不超过2000字符 |

字节配置最大1048576、timeout最大10000ms，均为正整数；字符估算与候选相关参数也在`RecallOptions`中拒绝0、布尔或过大配置。默认值保持既有候选和单卡设置，不以全仓调参选择新值。

```bash
uv run liveday0 recall "$TENANT_ID" "搬家" \
  --context-bytes 16384 --context-tokens 1600 --timeout-ms 800

uv run liveday0 expand-snapshot "$TENANT_ID" "$SNAPSHOT_ID" "$ITEM_ID" \
  --max-bytes 65536 --timeout-ms 800
```

API仍返回字典。预算按`recall_json()`的UTF-8序列化合同计算；`recall/expand-snapshot` CLI直接输出同一紧凑JSON加LF，因此没有漂亮打印后的隐性膨胀。交互式query也输出紧凑JSON；`LocalMemorySession`的`synthetic/context`外壳另有固定29字节，故默认整条query输出最多16413字节。外部调用者额外HTTP/SDK包装和传输帧须自行计入其自己的边界。

## 缩小与拒绝的可判别结果

先为每个规范语义身份准备完整的item/type展开句柄，要求最低引用信封能容纳；随后按原层序尝试完整卡/已有语义短形，超限整卡省略。每次重算**完整包**，包括报告数字的最终位数。不会裁剪JSON流、来源列表、候选中的半个claim或限定条件；省略项保留可调用的snapshot/item引用，完整原卡仍在同一pinned展开副本中。

正常返回`budget.payload_bytes`、`estimated_tokens`、`status=complete/reduced`和`omitted_card_count`。发生整卡省略时`degraded_reasons`包含`context_budget_reduced`；短形本身仍有representation。模型可自行决定是否需要展开，不规定表达或提问。连所有必要句柄都放不下，抛`RecallBudgetExceeded(complete_reference_envelope_limit)`，不创建新的召回快照。完整展开超过其字节上限则整项拒绝；可显式请求更大的有限上限，例如131072 bytes，不能把部分出处伪装成完整展开。

限额异常的CLI退出码为2，返回内容无关的`error/reason/context_available=false`小错误对象（不超过512 bytes，独立于可能只有1 byte的成功包预算）：`recall_budget_exceeded`、`recall_deadline_exceeded`、`recall_deadline_unavailable`。不混入可用context或原SQL/记忆正文。API抛对应的`RecallBudgetExceeded/RecallDeadlineExceeded/RecallDeadlineUnavailable`，交互终端显示静态异常类型和reason。配置本身不合法在I/O前拒绝。

## deadline的覆盖与真实边界

使用一个ContextVar截止点，finally复位，不把800ms重新分给每条SQL。它覆盖read-before-use追平、数据库连接、tenant门禁、各SQL、编译循环、快照发布和服务返回检查。连接沿psycopg/libpq非阻塞polling，以剩余预算和10ms检查间隔等待；每次SQL在门禁前即设置**剩余**statement_timeout，绝不设0。没有常驻线程、额外服务或超时后后台发布。

为避免同步DNS与隐式多地址重试破坏这个入口的连接预算，受限连接必须显式给出单一Unix socket目录、数字IP，或已解析单一`hostaddr`；名称地址/缺失地址/多地址会在连接前以`recall_deadline_unavailable`拒绝。普通写入、迁移和独立run-jobs仍沿原连接入口；README的127.0.0.1和隔离私有socket满足要求。测试假服务是本次私有目录的Unix socket，不监听TCP。

截止异常从维护catch-up直接传播，使当前claim/目标变更外层事务回滚；不是合成失败，不加failure_count、不耗尽重试预算。不返回已知旧上下文。截止前已经独立提交的合法catch-up保持；若最终事务收尾后才发现截止，可能已有未交给调用者的快照落库，不能声称整次服务所有已提交写入都回滚。

这是**协作式总工作deadline与实际数据库取消**，不是实时系统的严格墙钟保证。单次Python序列化/解码、惰性输入、事务收尾和OS调度不可硬中断；CLI启动/import/打印及外部传输不在服务工作预算内。测试30ms预算遇单次70ms Python步骤实际约83ms，之后明确拒绝迟到context；不能把这条证据写成30ms硬返回。连接、SQL和门禁测试的观察值也不是跨机器SLA。所有中间来源/增量/高连接图工作量和内存仍未因此获得硬上限。

## 定向证据与下一项

新标记空库、0700私有Unix socket、no TCP、受限应用登录：一个聚合case验证完整CLI字节与估算、极小信封不落新快照、41条出处和边界/不确定性完整、默认64KiB展开拒绝而显式128KiB完整展开成功、真实慢SQL及累计SQL/tenant门禁超时、私有连接停滞、Python协作检查、catch-up预算不消耗、ContextVar复位。SQL/累积/门禁40ms预算分别约40.05/40.40/40.43ms，80ms连接预算约87.25ms。单独无I/O方法验证未解析名称拒绝；受影响既有单卡预算方法只把总预算改到能容纳真实信封的350并定向执行，新增小信封拒绝验证另保留；旧cue/48与8候选单项回归通过。未运行全套、旧演示/重启/heldout/v3c或真实模型。

原反例`/private/tmp/ld0-limit-red-lkkdrpek/manifest.json`，新聚合`/private/tmp/ld0-limits-0owrgs45/manifest.json`，受影响旧cue`/private/tmp/ld0-oldcue-8sujujyp/manifest.json`，全部停止、PID不存在。接口没有增加记忆TTL、硬容量治理、自动清理或真实模型质量证明。

下一项仍在产品第三优先：先复用中文变体/重复事件/无关查询的现有证据，挑一个真实未验收的生活检索反例；高连接来源/增量等完整工作量门槛也保持开放。不要转成无限维护诊断或事后放宽门槛。

后续[选中卡片来源引用工作量](recall-source-work.md)新增默认总256行、最大4096的来源读取/装配限额及`recall_work_limit_exceeded`；先选择相关卡才加载来源。其余完整工作量与严格墙钟限制仍保持，不修改本页原字节/超时证据。
